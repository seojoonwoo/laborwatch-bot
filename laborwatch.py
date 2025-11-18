#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cloudtype App 항상 실행 + APScheduler로 매일 08:00에만 뉴스 → Telegram 알림봇

운영 로직
---------
- Cloudtype: python@3.11 App 으로 24시간 항상 실행
- Start command:  python laborwatch.py
- 이 스크립트:
    → APScheduler BlockingScheduler 가 백그라운드에서 대기
    → 매일 08:00(Asia/Seoul) 에 job() 한 번 실행
    → 그 외 시간에는 그냥 대기(프로세스는 살아있음)

뉴스 범위
---------
- job()이 실행되는 시각 기준 24시간 전 ~ 1분 전 기사만 포함
  (예: 오늘 08:00 실행 → 전날 08:00 ~ 오늘 07:59 기사)

카테고리
---------
  1) 인사노무 일반 뉴스 TOP 10
     - Google News (노동·근로·인사노무·육아·채용·장애인·가족돌봄 등, OR 기반)
  2) 노동관계 법령 개정 관련 뉴스 TOP 10
     - Google News (근로기준법·남녀고용평등법·산안법 등 + 개정·입법예고 등)
  3) 고용노동부 보도자료 및 정책알림 TOP 5
     - korea.kr 고용노동부 RSS: https://www.korea.kr/rss/dept_moel.xml
  4) 금융위원회 보도자료 및 정책알림 TOP 5
     - 금융위 보도자료 RSS: http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111
  5) KCGS 관련 뉴스 TOP 1
     - Google News (KCGS / 한국ESG기준원 언급 기사, kcgs.or.kr 발행은 제외)

환경변수
---------
  TELEGRAM_TOKEN   : 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID : 보내줄 채팅 ID
"""

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests
from dateutil import parser as dateparser
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

############################
# 기본 설정
############################

# Google News RSS (카테고리 1,2,5용)
BASE_RSS_GOOGLE = (
    "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
)

# 부처/기관 RSS (카테고리 3,4용)
MOEL_RSS = "https://www.korea.kr/rss/dept_moel.xml"  # 고용노동부
FSC_RSS = "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111"  # 금융위 보도자료

UA = "LaborNewsBot/1.2 (+https://github.com)"
KST = timezone(timedelta(hours=9))


def get_time_window_utc():
    """
    현재 시각(now UTC) 기준:
    - 시작: 24시간 전
    - 종료: 1분 전

    예) 08:00 KST에 실행되면,
        → 전날 08:00 ~ 오늘 07:59 KST 사이 기사만 포함
    """
    now_utc = datetime.now(timezone.utc)
    end_utc = now_utc - timedelta(minutes=1)
    start_utc = now_utc - timedelta(hours=24)
    return start_utc, end_utc


############################
# 텔레그램
############################

def tg(msg: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception:
        # 텔레그램 실패는 조용히 무시
        pass


############################
# 뉴스 수집 유틸
############################

def looks_korean(text: str) -> bool:
    """제목이 거의 영문이면(ESG 영문 기사 등) 버리기 위한 필터."""
    return bool(re.search(r"[가-힣]", text or ""))


def make_google_url(query: str) -> str:
    return BASE_RSS_GOOGLE.format(query=quote(query))


def fetch_feed(url: str, label: str = ""):
    """
    RSS/Atom 주소에서 피드 가져오기 + 디버그 알림.

    - 요청 실패: [뉴스봇 오류] ... 형태로 텔레그램 알림
    - 응답 성공 + entries 0개: [뉴스봇] ... entries가 0개입니다.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)

        if not feed.entries and label:
            tg(
                f"[뉴스봇] {label} RSS 응답은 성공했지만 기사 entries가 0개입니다.\n"
                f"URL={url}"
            )

        return feed
    except Exception as e:
        if label:
            tg(
                f"[뉴스봇 오류] {label} RSS 요청 실패: "
                f"{type(e).__name__}: {e}"
            )
        return feedparser.parse("")


def to_utc(dt):
    """dateutil 이 파싱한 날짜를 UTC로 변환 (tz 없으면 KST 가정 후 UTC로)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)


def normalize_entries(
    feed,
    limit: int,
    block_domains=None,
    window_start_utc=None,
    window_end_utc=None,
):
    """
    - feed에서 기사 목록 추출
    - 도메인 필터 / 한글 필터 / 시간 윈도우 필터 적용
    - 최신순 정렬 후 TOP N 반환
    """
    block_domains = block_domains or []
    items = []

    for e in feed.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        if not title or not link:
            continue

        # 도메인 필터 (ex: kcgs.or.kr 제외)
        if any(dom in link for dom in block_domains):
            continue

        # 한글 없는 기사(영문 ESG 등) 제외
        if not looks_korean(title):
            continue

        # 다양한 필드에서 날짜 추출
        published_raw = (
            getattr(e, "published", "")
            or getattr(e, "updated", "")
            or getattr(e, "pubDate", "")
        )

        if not published_raw and getattr(e, "published_parsed", None):
            try:
                dt_utc = datetime(
                    *e.published_parsed[:6], tzinfo=timezone.utc
                )
            except Exception:
                dt_utc = None
        elif published_raw:
            try:
                dt_parsed = dateparser.parse(published_raw)
                dt_utc = to_utc(dt_parsed)
            except Exception:
                dt_utc = None
        else:
            dt_utc = None

        if dt_utc is None:
            continue

        if window_start_utc and window_end_utc:
            # 시간 범위 밖이면 제외 (알림 시점 기준 24시간 ~ 1분 전 사이만)
            if not (window_start_utc <= dt_utc <= window_end_utc):
                continue

        items.append(
            {
                "title": title,
                "link": link,
                "published": dt_utc,
                "source": getattr(e, "source", getattr(e, "author", "")) or "",
            }
        )

    # 최신순 정렬
    items.sort(key=lambda x: x["published"], reverse=True)

    # 제목 기준 중복 제거 + TOP N
    seen = set()
    deduped = []
    for it in items:
        key = it["title"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
        if len(deduped) >= limit:
            break
    return deduped


def escape_md(text: str) -> str:
    """
    텔레그램 Markdown용 간단 이스케이프.

    - 문자셋 맨 끝에 '-' 를 둬서 bad character range 방지.
    """
    return re.sub(r"([_*\[\]()~`>#+\\=|{}.!-])", r"\\\1", text or "")


def format_items(title: str, items, max_items: int) -> str:
    if not items:
        return f"*{title}*\n- (해당 24시간 범위 내 수집된 뉴스가 없습니다)\n\n"

    lines = [f"*{title}*"]
    for i, it in enumerate(items[:max_items], start=1):
        dt = it["published"]
        if dt:
            datestr = dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        else:
            datestr = "날짜 불명"

        t = escape_md(it["title"])
        link = it["link"]
        lines.append(f"{i}. [{t}]({link})\n   - {datestr}")
    lines.append("")  # 마지막 개행
    return "\n".join(lines)


############################
# 카테고리별 수집
############################

def get_category_1(ws, we):
    """
    1) 인사노무 일반 뉴스 TOP 10
       - 키워드 OR로 완화: 노동/근로/인사노무/육아/청년/모성보호/출산/채용/파견/장애인 고용/가족돌봄 등
    """
    query = (
        "노동 OR 근로 OR 인사노무 OR 인사팀 OR HR OR "
        "육아 OR 육아휴직 OR 육아기 단축 OR 청년 고용 OR 청년 일자리 OR "
        "모성보호 OR 출산휴가 OR 출산 OR 임신 OR "
        "채용 OR 모집 OR 공채 OR 채용공고 OR "
        "파견근로 OR 파견 노동자 OR 파견직 OR "
        "기간제 근로 OR 비정규직 OR 단시간 근로 OR 시간제 근로 OR "
        "장애인 고용 OR 장애인고용 OR "
        "가족돌봄 OR 가족돌봄휴가 OR 일가정양립 OR 워라밸"
    )
    url = make_google_url(query)
    feed = fetch_feed(url, "카테고리1 인사노무")
    return normalize_entries(
        feed, limit=10, window_start_utc=ws, window_end_utc=we
    )


def get_category_2(ws, we):
    """
    2) 노동 관계 법령 개정 뉴스 (근로기준법, 모성보호, 남녀고용평등 등) TOP 10
       - (법령명 OR ...) AND (개정/입법예고/시행/공포 등)
    """
    law_part = (
        "근로기준법 OR 노동관계법 OR 노동법 OR 남녀고용평등법 OR 남녀고용평등 OR "
        "모성보호 OR 육아휴직 OR 육아기 근로시간 단축 OR 산업안전보건법 OR 산안법 OR "
        "파견근로자보호법 OR 파견근로자 보호 등에 관한 법률 OR 기간제법 OR "
        "기간제 및 단시간근로자 보호 등에 관한 법률 OR 고용정책기본법 OR "
        "근로자퇴직급여보장법 OR 퇴직급여법 OR 퇴직연금법 OR "
        "근로시간 제도 OR 임금체계 OR 임금직무급 OR 직장 내 괴롭힘"
    )
    change_part = (
        "개정 OR 개정안 OR 전부개정 OR 일부개정 OR "
        "법률안 OR 개편 OR 제도개편 OR 법 개정 OR "
        "시행령 개정 OR 시행규칙 개정 OR 입법예고 OR 행정예고 OR "
        "공포 OR 시행"
    )
    query = f"({law_part}) AND ({change_part})"
    url = make_google_url(query)
    feed = fetch_feed(url, "카테고리2 법령개정")
    return normalize_entries(
        feed, limit=10, window_start_utc=ws, window_end_utc=we
    )


def get_category_3(ws, we):
    """
    3) 고용노동부 보도자료 및 정책알림 TOP 5
       - korea.kr 고용노동부 RSS 직접 사용
    """
    feed = fetch_feed(MOEL_RSS, "고용노동부 보도자료")
    return normalize_entries(
        feed, limit=5, window_start_utc=ws, window_end_utc=we
    )


def get_category_4(ws, we):
    """
    4) 금융위원회 보도자료 및 정책알림 TOP 5
       - 금융위원회 보도자료 RSS 직접 사용
    """
    feed = fetch_feed(FSC_RSS, "금융위원회 보도자료")
    return normalize_entries(
        feed, limit=5, window_start_utc=ws, window_end_utc=we
    )


def get_category_5(ws, we):
    """
    5) KCGS(한국ESG기준원) 관련 뉴스 TOP 1
       - KCGS를 언급하는 외부 기사 (kcgs.or.kr 자체 발행은 제외)
       - Google News 검색 사용
    """
    query = '(KCGS OR "한국ESG기준원")'
    url = make_google_url(query)
    feed = fetch_feed(url, "카테고리5 KCGS")
    return normalize_entries(
        feed,
        limit=1,
        block_domains=["cgs.or.kr", "kcgs.or.kr"],
        window_start_utc=ws,
        window_end_utc=we,
    )


############################
# 알림 메시지 생성 + 발송
############################

def build_message() -> str:
    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    ws, we = get_time_window_utc()

    header = (
        f"🔔 인사노무·노동법·정책 뉴스 요약 ({today_str})\n"
        "알림 기준: 실행 시각 기준 24시간 전 ~ 1분 전 사이에 발생한 기사만 포함합니다.\n"
        "③ 고용노동부·④ 금융위원회는 각 부처 RSS를 직접 수집하고,\n"
        "①②⑤는 Google News 기반으로 한글 기사만 필터링합니다.\n\n"
    )

    cat1 = format_items(
        "① 노동·육아·청년·모성보호·출산·채용·파견·장애인·가족돌봄 등 인사노무 뉴스 TOP 10",
        get_category_1(ws, we),
        10,
    )
    cat2 = format_items(
        "② 노동 관계 법령 개정 관련 뉴스 TOP 10",
        get_category_2(ws, we),
        10,
    )
    cat3 = format_items(
        "③ 고용노동부 보도자료·정책 알림 TOP 5",
        get_category_3(ws, we),
        5,
    )
    cat4 = format_items(
        "④ 금융위원회 보도자료·정책 알림 TOP 5",
        get_category_4(ws, we),
        5,
    )
    cat5 = format_items(
        "⑤ KCGS(한국ESG기준원) 관련 뉴스 TOP 1 (KCGS 자체 보도 제외)",
        get_category_5(ws, we),
        1,
    )

    msg = header + cat1 + cat2 + cat3 + cat4 + cat5
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n(이하 생략됨)"
    return msg


def job():
    """매일 08:00에 실행될 실제 작업."""
    try:
        msg = build_message()
        tg(msg)
    except Exception as e:
        # 전체 job 수준 에러도 한번 남겨두기
        tg(f"[뉴스봇 치명오류] job() 실행 중 예외: {type(e).__name__}: {e}")


############################
# 메인: APScheduler 로 24시간 상주
############################

def main():
    sched = BlockingScheduler(timezone=KST)

    # 매일 08:00에 job 실행
    trigger = CronTrigger(hour=8, minute=0, second=0, timezone=KST)
    sched.add_job(job, trigger, name="laborwatch_daily_8am")

    # 시작 직후 테스트 발송 원하면 아래 주석 해제
    # job()

    sched.start()


if __name__ == "__main__":
    main()
