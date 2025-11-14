#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LaborWatch — 노동·법령·금융·ESG 알림봇 (Cloudtype + Telegram)

기능 요약:
1) 전송 대상은 '알림 기준일의 전날(00:00~23:59, KST)'에 새로 올라온 항목만.
2) 법령(입법예고/시행/행정예고)은 '노동관계 법령'만 필터링(법명 + 백업 키워드).
3) ESG 뉴스는 Top3, 한국ESG기준원 관련 뉴스도 Top3(다매체 중복 근사)만 전송.
4) 오전 8시 자동 알림(Cloudtype 스케줄 UI가 없어도, 내부 타이머로 실행).
5) 전날 창구 수집 요약(카테고리별 건수 + ERR 건수)도 함께 전송.

ENV:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  RUN_MODE=DAILY|POLL (권장: DAILY)
  POLL_INTERVAL_S=900
"""

import os, re, time, hashlib, sqlite3, textwrap, html
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
import requests
import xml.etree.ElementTree as ET

# 선택 의존성(HTML 파싱 안정성 ↑)
try:
    from bs4 import BeautifulSoup  # type: ignore
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

from feeds_config import FEEDS

# ===== 기본 설정 =====
KST = timezone(timedelta(hours=9))
DB_PATH = os.getenv("DB_PATH", "laborwatch.sqlite3")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_ID", ""))
RUN_MODE         = os.getenv("RUN_MODE", "DAILY").upper()
POLL_INTERVAL_S  = int(os.getenv("POLL_INTERVAL_S", "900"))
HEADERS = {"User-Agent": "LaborWatchBot/1.2 (+Cloudtype)"}

# ===== 키워드 세트(뉴스 필터용) =====
KW = {
    "노동뉴스": r"(노동|근로|근로기준법|산업안전보건|최저임금|주\s?52시간|모성보호|육아|남녀고용평등|노사관계|통상임금|연차|포괄임금|근로시간단축|타임오프)",
    "금융위뉴스": r"(금융위원회|금융위|증선위|FIU|정책금융)",
    "금감원뉴스": r"(금융감독원|금감원|DART|전자공시)",
    "ESG뉴스": r"\b(ESG|지속가능경영|지배구조|ESG공시|KCGS|한국ESG기준원)\b",
}

# ===== 노동관계 법령 화이트리스트 + 백업 키워드 =====
LABOR_LAWS = [
    "근로기준법", "산업안전보건법", "최저임금법", "남녀고용평등", "고용보험법",
    "근로자퇴직급여", "기간제 및 단시간근로자 보호", "파견근로자보호",
    "노동조합 및 노동관계조정법", "근로복지기본법", "고용정책 기본법",
    "직업안정법", "산재보험", "모성보호", "육아기 근로시간 단축", "남녀고용평등법",
]
LABOR_LAW_PAT    = re.compile("|".join(LABOR_LAWS), re.I)
LABOR_BACKUP_PAT = re.compile(r"(근로|노동|모성보호|육아|남녀고용평등|최저임금|산업안전|퇴직급여)", re.I)

KCGS_PAT = re.compile(r"(한국ESG기준원|KCGS)", re.I)

# ===== 텔레그램 =====
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception:
        pass

# ===== 저장소 =====
def ensure_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS seen (
              id TEXT PRIMARY KEY,
              title TEXT, link TEXT, pubdate TEXT, feed TEXT, cat TEXT, first_seen_ts TEXT
            )
        """)

def mk_id(title: str, link: str) -> str:
    base = (title or "") + "|" + (link or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

# ===== 공통 도우미 =====
def summarize(title: str, summary: str) -> str:
    t = (summary or "").strip()
    t = html.unescape(re.sub(r"<.*?>", "", t)).replace("&nbsp;", " ").strip()
    if not t:
        t = (title or "").strip()
    t = re.split(r"[。.!?]\s|[\n]", t)[0]
    return textwrap.shorten(t, width=180, placeholder="…")

def parse_rss(xml_text: str) -> List[Dict]:
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: List[Dict] = []

    # RSS 2.0
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link")  or "").strip()
        desc  = (it.findtext("description") or "").strip()
        pub   = (it.findtext("pubDate") or "").strip()
        items.append({"title": title, "link": link, "summary": desc, "pub": pub})

    # Atom
    for e in root.findall(".//atom:entry", ns):
        title = (e.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("atom:link", ns)
        link = (link_el.get("href") if link_el is not None else "").strip()
        summary = (e.findtext("atom:summary", default="", namespaces=ns) or
                   e.findtext("atom:content", default="", namespaces=ns) or "").strip()
        pub = (e.findtext("atom:updated", default="", namespaces=ns) or
               e.findtext("atom:published", default="", namespaces=ns) or "").strip()
        items.append({"title": title, "link": link, "summary": summary, "pub": pub})
    return items

def fetch_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

# ===== 날짜 파서 =====
DATE_PATTERNS = [
    "%a, %d %b %Y %H:%M:%S %z",    # Tue, 11 Nov 2025 07:00:00 +0900
    "%a, %d %b %Y %H:%M:%S %Z",    # Tue, 11 Nov 2025 07:00:00 KST
    "%Y-%m-%dT%H:%M:%S%z",         # 2025-11-11T07:00:00+09:00
    "%Y-%m-%dT%H:%M:%S.%f%z",      # 2025-11-11T07:00:00.000+09:00
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
]

def parse_dt(text: str) -> Optional[datetime]:
    t = (text or "").strip()
    if not t:
        return None

    # Z → +0000 보정
    if t.endswith("Z") and "T" in t:
        try:
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
            return dt.replace(tzinfo=timezone.utc).astimezone(KST)
        except Exception:
            pass

    for pat in DATE_PATTERNS:
        try:
            dt = datetime.strptime(t, pat)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt.astimezone(KST)
        except Exception:
            continue

    # 숫자만 강제 추출: YYYY.MM.DD 또는 YYYY-MM-DD (+ optional HH:MM)
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        try:
            hh = int(m.group(4) or 0)
            mm = int(m.group(5) or 0)
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm, tzinfo=KST)
            return dt
        except Exception:
            return None

    return None

# ===== 카테고리 판별 =====
def categorize(feed_url: str, title: str, summary: str) -> str:
    t = f"{title} {summary}".strip()

    if "fsc.go.kr" in feed_url:
        return "금융위 보도자료"
    if "moel.go.kr" in feed_url and "lawinfo" in feed_url:
        return "입법·행정예고"
    if "moel.go.kr" in feed_url:
        return "노동부 소식"
    if "moleg.go.kr" in feed_url and "ll_rss" in feed_url:
        return "최신 시행법령"
    if "moleg.go.kr" in feed_url and "li_rss" in feed_url:
        return "입법예고"
    if "news.google.com" in feed_url:
        for label, pat in KW.items():
            if re.search(pat, t, re.I):
                return label
        return "뉴스"

    for label, pat in KW.items():
        if re.search(pat, t, re.I):
            return label
    return "기타"

# ===== 특수 소스: DART 최신 공시(금감원) =====
def collect_from_dart(url: str) -> List[Dict]:
    out: List[Dict] = []
    try:
        html_text = fetch_text(url)
    except Exception:
        return out

    if not HAS_BS4:
        for m in re.finditer(r'href="(/dsaf001/main\.do\?rcpNo=\d+)[^"]*".*?>([^<]+)</a>', html_text):
            link = "https://dart.fss.or.kr" + m.group(1)
            title = html.unescape(m.group(2)).strip()
            out.append({"title": title, "link": link, "summary": "", "pub": ""})
        return out

    soup = BeautifulSoup(html_text, "html.parser")
    for a in soup.select('a[href*="/dsaf001/main.do?rcpNo="]'):
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        link = "https://dart.fss.or.kr" + href
        pub = ""
        tr = a.find_parent("tr")
        if tr:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            for token in tds[::-1]:
                if re.search(r"\d{4}-\d{2}-\d{2}", token):
                    pub = token
                    break
        out.append({"title": title, "link": link, "summary": "", "pub": pub})
    return out

# ===== 수집 =====
def collect_items() -> List[Dict]:
    items: List[Dict] = []
    for _cat, urls in FEEDS.items():
        for u in urls:
            try:
                if "dart.fss.or.kr" in u:
                    items.extend(collect_from_dart(u))
                elif u.startswith("http"):
                    txt = fetch_text(u)
                    try:
                        for it in parse_rss(txt):
                            it["feed"] = u
                            items.append(it)
                    except Exception:
                        pass
            except Exception as e:
                items.append({
                    "title": f"[ERR] {u}",
                    "link": "",
                    "summary": str(e),
                    "pub": "",
                    "feed": u,
                })

    for it in items:
        it.setdefault("feed", "unknown")
        it["cat"] = categorize(it["feed"], it.get("title",""), it.get("summary",""))
        it["_dt"] = parse_dt(it.get("pub","") or "")
    return items

# ===== 인기(조회수 근사) Top3 선택 =====
def normalize_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip().lower()
    t = re.sub(r"\[[^\]]+\]|\([^)]+\)", "", t)
    return t

def pick_top3_by_popularity(items: List[Dict]) -> List[Dict]:
    buckets: Dict[str, List[Dict]] = {}
    for it in items:
        key = normalize_title(it.get("title","")) or it.get("link","")
        buckets.setdefault(key, []).append(it)

    ranked = sorted(
        buckets.values(),
        key=lambda grp: (
            len(grp),
            max([(it.get("_dt") or datetime.min.replace(tzinfo=KST)) for it in grp])
        ),
        reverse=True
    )

    top: List[Dict] = []
    for grp in ranked:
        rep = sorted(
            grp,
            key=lambda x: x.get("_dt") or datetime.min.replace(tzinfo=KST),
            reverse=True
        )[0]
        top.append(rep)
        if len(top) == 3:
            break
    return top

# ===== 노동관계 법령 필터 =====
def is_labor_law_item(it: Dict) -> bool:
    if it.get("cat") in ("입법예고", "최신 시행법령", "입법·행정예고"):
        text = f"{it.get('title','')} {it.get('summary','')}"
        if LABOR_LAW_PAT.search(text):
            return True
        # 법명은 없지만 노동 관련일 가능성이 높은 제목/요약
        return bool(LABOR_BACKUP_PAT.search(text))
    return True

# ===== 처리 & 전송 =====
def render_msg(it: Dict, window_label: str) -> str:
    title = (it.get("title") or "").strip()
    link  = (it.get("link")  or "").strip()
    summ  = summarize(title, it.get("summary") or "")
    pub   = (it.get("pub")   or "").strip()
    feed  = it.get("feed") or ""
    cat   = it.get("cat")  or "정보"
    return (
        f"🔔 {cat} ({window_label})\n"
        f"• 제목: {title}\n"
        f"• 요약: {summ}\n"
        f"• 날짜: {pub}\n"
        f"• 출처: {feed}\n"
        f"{link}"
    )

def process_once() -> int:
    """
    기준시각(now, KST)에서 '전날 00:00~23:59'에 해당하는 항목만 전송
    """
    ensure_db()
    now = datetime.now(KST)

    y_start = (now.date() - timedelta(days=1))
    y_end   = (now.date() - timedelta(days=1))
    win_start_dt = datetime(y_start.year, y_start.month, y_start.day, 0, 0, 0, tzinfo=KST)
    win_end_dt   = datetime(y_end.year,   y_end.month,   y_end.day,   23, 59, 59, tzinfo=KST)
    window_label = f"{y_start.strftime('%Y-%m-%d')} 업데이트"

    items = collect_items()

    # 1) 날짜 윈도우(전날) 필터
    items = [it for it in items if it.get("_dt") and win_start_dt <= it["_dt"] <= win_end_dt]

    # 2) 전날 창구 수집 요약(카테고리/ERR) 먼저 전송
    summary: Dict[str, int] = {}
    for it in items:
        if it.get("title", "").startswith("[ERR]"):
            key = "ERR"
        else:
            key = it.get("cat", "기타")
        summary[key] = summary.get(key, 0) + 1

    count_line = " | ".join(f"{k}:{v}" for k, v in summary.items()) if summary else "수집 0건"
    tg_send(f"📊 {window_label} 창구 수집 요약: {count_line}")

    # 3) ESG/KCGS 뉴스 분리 후 Top3만 허용
    kcgs_items = [
        it for it in items
        if it.get("cat") in ("ESG뉴스", "뉴스")
        and KCGS_PAT.search(f"{it.get('title','')} {it.get('summary','')}")
    ]
    esg_items = [
        it for it in items
        if it.get("cat") == "ESG뉴스"
        and not KCGS_PAT.search(f"{it.get('title','')} {it.get('summary','')}")
    ]

    kcgs_top3 = pick_top3_by_popularity(kcgs_items)
    esg_top3  = pick_top3_by_popularity(esg_items)
    allowed_top_ids = {mk_id(it.get("title",""), it.get("link","")) for it in (kcgs_top3 + esg_top3)}

    sent = 0
    with sqlite3.connect(DB_PATH) as c:
        for it in items:
            # 4) 노동관계 법령 필터
            if not is_labor_law_item(it):
                continue

            # 5) ESG/한국ESG기준원 뉴스는 Top3만 허용
            if it.get("cat") in ("ESG뉴스", "뉴스"):
                uid_tmp = mk_id(it.get("title",""), it.get("link",""))
                if uid_tmp not in allowed_top_ids:
                    continue

            # 6) 중복 전송 방지
            uid = mk_id(it.get("title",""), it.get("link",""))
            if not uid:
                continue
            if c.execute("SELECT 1 FROM seen WHERE id=?", (uid,)).fetchone():
                continue

            c.execute(
                "INSERT INTO seen (id,title,link,pubdate,feed,cat,first_seen_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    uid,
                    it.get("title",""),
                    it.get("link",""),
                    it.get("pub",""),
                    it.get("feed",""),
                    it.get("cat",""),
                    now.isoformat(),
                ),
            )
            tg_send(render_msg(it, window_label))
            time.sleep(0.35)
            sent += 1

    if sent == 0:
        tg_send(f"✅ {window_label} 기준 신규 알림 없음 (전날 필터 적용)")
    return sent

def run_daily():
    process_once()

def run_poll():
    while True:
        try:
            process_once()
        except Exception as e:
            tg_send(f"[LaborWatch 오류] {e}")
        time.sleep(POLL_INTERVAL_S)

if __name__ == "__main__":
    # 스케줄 UI 없이도 매일 08:00 실행되도록 내부 타이머 제공
    if RUN_MODE == "POLL":
        run_poll()
    elif RUN_MODE == "DAILY":
        while True:
            now = datetime.now(KST)
            if now.hour == 8 and now.minute == 0:
                try:
                    process_once()
                except Exception as e:
                    tg_send(f"[오류] {e}")
                time.sleep(60)  # 1분 동안 중복 실행 방지
            else:
                time.sleep(30)
    else:
        run_daily()
