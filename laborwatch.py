#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LaborWatch — 노동·법령·금융·ESG 알림봇 (Cloudtype + Telegram)
- 소스: 법제처 RSS, 고용노동부 RSS, 금융위 RSS, DART(금감원) 최신공시(HTML 파싱), Google News RSS(키워드)
- 기능: 수집 → 카테고리 라벨 → 키워드 필터 → 중복제거(SQLite) → 텔레그램 전송
- 실행: RUN_MODE=DAILY (1회 실행) / RUN_MODE=POLL (주기 실행)
- ENV:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    RUN_MODE=DAILY|POLL (기본 DAILY)
    POLL_INTERVAL_S=900 (기본 900초)
"""

import os, re, time, hashlib, sqlite3, textwrap, html
from datetime import datetime, timedelta, timezone
from typing import List, Dict
import requests
import xml.etree.ElementTree as ET

# 선택 의존성(HTML 파싱 안정성 ↑)
try:
    from bs4 import BeautifulSoup  # type: ignore
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

from feeds_config import FEEDS   # <<< 당신이 올릴 feeds_config.py 에서 읽습니다.

# ===== 기본 설정 =====
KST = timezone(timedelta(hours=9))
DB_PATH = os.getenv("DB_PATH", "laborwatch.sqlite3")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_ID", ""))
RUN_MODE         = os.getenv("RUN_MODE", "DAILY").upper()
POLL_INTERVAL_S  = int(os.getenv("POLL_INTERVAL_S", "900"))

HEADERS = {"User-Agent": "LaborWatchBot/1.0 (+Cloudtype)"}

# ===== 키워드 세트(뉴스 필터용) =====
KW = {
    "노동뉴스": r"(노동|근로|근로기준법|산업안전보건|최저임금|주\s?52시간|모성보호|육아|남녀고용평등|노사관계|통상임금|연차|포괄임금|근로시간단축|타임오프)",
    "금융위뉴스": r"(금융위원회|금융위|증선위|FIU|정책금융)",
    "금감원뉴스": r"(금융감독원|금감원|DART|전자공시)",
    "ESG뉴스": r"\b(ESG|지속가능경영|지배구조|ESG공시|KCGS|한국ESG기준원)\b",
}

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
def summarize(text: str, summary: str) -> str:
    t = (summary or "").strip()
    t = html.unescape(re.sub(r"<.*?>", "", t)).replace("&nbsp;", " ").strip()
    if not t:
        t = (text or "").strip()
    t = re.split(r"[。.!?]\s|[\n]", t)[0]
    return textwrap.shorten(t, width=180, placeholder="…")

def parse_rss(xml_text: str) -> List[Dict]:
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []

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

# ===== 카테고리 판별 =====
def categorize(feed_url: str, title: str, summary: str) -> str:
    t = f"{title} {summary}".strip()

    # 출처 기반 우선
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
        # 키워드 우선 라벨링
        for label, pat in KW.items():
            if re.search(pat, t, re.I): return label
        return "뉴스"

    # 키워드로 백업 라벨링
    for label, pat in KW.items():
        if re.search(pat, t, re.I): return label
    return "기타"

# ===== 특수 소스: DART 최신 공시(금감원) =====
def collect_from_dart(url: str) -> List[Dict]:
    """
    금감원 DART: 공식 RSS가 아닌 경우가 있으므로 메인/목록 HTML을 파싱한다.
    - https://dart.fss.or.kr/dsac001/main.do  (최근공시 영역)
    HTML 구조가 바뀌더라도 'rcpNo' 또는 '/dsaf001/main.do?rcpNo=' 형태의 링크를 주로 찾는다.
    """
    out = []
    try:
        html_text = fetch_text(url)
    except Exception:
        return out

    if not HAS_BS4:
        # BeautifulSoup이 없으면 정규식으로 최소 정보만 파싱
        for m in re.finditer(r'href="(/dsaf001/main\.do\?rcpNo=\d+)[^"]*".*?>([^<]+)</a>', html_text):
            link = "https://dart.fss.or.kr" + m.group(1)
            title = html.unescape(m.group(2)).strip()
            out.append({"title": title, "link": link, "summary": "", "pub": ""})
        return out

    soup = BeautifulSoup(html_text, "html.parser")
    # 최근공시 테이블 영역에서 링크/제목/일자 추출 (유연한 선택자)
    for a in soup.select('a[href*="/dsaf001/main.do?rcpNo="]'):
        title = a.get_text(strip=True)
        href = a.get("href") or ""
        link = "https://dart.fss.or.kr" + href
        # 가능하면 같은 행의 날짜도 추출
        pub = ""
        tr = a.find_parent("tr")
        if tr:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            # 보통 [번호, 공시번호, 회사명, 보고서명, 접수일자, ...] 형태
            for token in tds[::-1]:
                if re.search(r"\d{4}-\d{2}-\d{2}", token):
                    pub = token
                    break
        out.append({"title": title, "link": link, "summary": "", "pub": pub})
    return out

# ===== 메인 수집 =====
def collect_items() -> List[Dict]:
    items: List[Dict] = []

    for cat, urls in FEEDS.items():
        for u in urls:
            try:
                if "dart.fss.or.kr" in u:
                    items.extend(collect_from_dart(u))
                elif u.startswith("http"):
                    # RSS/Atom 시도
                    txt = fetch_text(u)
                    # RSS 파싱 실패 시 HTML일 수도 있으니 Google News 등은 그대로 RSS로 파싱 가능
                    try:
                        items_from_rss = parse_rss(txt)
                        for it in items_from_rss:
                            it["feed"] = u
                            items.append(it)
                    except Exception:
                        # RSS가 아니면(HTML) 단순 링크 스킴으로 스킵
                        # (정책브리핑 HTML 등은 필요 시 별도 파서 추가)
                        pass
                else:
                    continue
            except Exception as e:
                items.append({"title": f"[ERR] {u}", "link": "", "summary": str(e), "pub": "", "feed": u})

    # FEEDS의 카테고리는 '출처 그룹'이고, 실제 발송용 라벨은 categorize()로 최종 결정
    for it in items:
        it.setdefault("feed", "unknown")
        it["cat"] = categorize(it["feed"], it.get("title",""), it.get("summary",""))

    return items

# ===== 처리 & 전송 =====
def render_msg(it: Dict) -> str:
    title = (it.get("title") or "").strip()
    link  = (it.get("link")  or "").strip()
    summ  = summarize(title, it.get("summary") or "")
    pub   = (it.get("pub")   or "").strip()
    feed  = it.get("feed") or ""
    cat   = it.get("cat")  or "정보"
    return (
        f"🔔 {cat}\n"
        f"• 제목: {title}\n"
        f"• 요약: {summ}\n"
        f"• 날짜: {pub}\n"
        f"• 출처: {feed}\n"
        f"{link}"
    )

def process_once() -> int:
    ensure_db()
    now = datetime.now(KST)
    items = collect_items()
    sent = 0

    with sqlite3.connect(DB_PATH) as c:
        for it in items:
            uid = mk_id(it.get("title",""), it.get("link",""))
            if not uid: 
                continue
            cur = c.execute("SELECT 1 FROM seen WHERE id=?", (uid,)).fetchone()
            if cur:
                continue
            # 필터: 뉴스 라벨은 키워드가 실제로 들어있는지만 한번 더 체크(오검출 방지)
            if it["cat"] in ("노동뉴스","금융위뉴스","금감원뉴스","ESG뉴스"):
                text_for_match = f"{it.get('title','')} {it.get('summary','')}"
                pat = KW[it["cat"]]
                if not re.search(pat, text_for_match, re.I):
                    continue

            c.execute(
                "INSERT INTO seen (id,title,link,pubdate,feed,cat,first_seen_ts) VALUES (?,?,?,?,?,?,?)",
                (uid, it.get("title",""), it.get("link",""), it.get("pub",""), it.get("feed",""), it.get("cat",""), now.isoformat())
            )
            tg_send(render_msg(it))
            time.sleep(0.4)
            sent += 1

    if sent == 0:
        tg_send(f"✅ {now.strftime('%Y-%m-%d')} 현재 신규 알림 없음")
    return sent

def run_daily():
    process_once()

def run_poll():
    # 시작 즉시 1회, 이후 주기
    while True:
        try:
            process_once()
        except Exception as e:
            tg_send(f"[LaborWatch 오류] {e}")
        time.sleep(POLL_INTERVAL_S)

if __name__ == "__main__":
    if RUN_MODE == "POLL":
        run_poll()
    else:
        run_daily()
