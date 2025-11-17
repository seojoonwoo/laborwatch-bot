#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cloudtype 스케줄(daily) 전제 통합 뉴스 → Telegram 알림봇

운영 로직:
- Cloudtype에서: 매일 오전 8시(Asia/Seoul 기준)에 이 파일을 한 번 실행
- 파이썬 스크립트:
    → 실행 시점 기준 24시간 전 ~ 1분 전 사이 기사만 수집
    → 텔레그램으로 알림 한 번 보내고 종료

카테고리:
  1) 인사노무 일반 뉴스 TOP 10
  2) 노동관계 법령 개정 뉴스 TOP 10
  3) 고용노동부 보도자료 및 정책 알림 TOP 5
  4) 금융위원회 보도자료 및 정책 알림 TOP 5
  5) KCGS 관련 뉴스 TOP 1 (kcgs.or.kr 발행 제외)

환경변수:
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


############################
# 설정
############################

BASE_RSS = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
UA = "LaborNewsBot/1.0 (+https://github.com)"
KST = timezone(timedelta(hours=9))


def get_time_window_utc():
    """
    현재 시각(now UTC) 기준:
    - 시작: 24시간 전
    - 종료: 1분 전

    예) 오늘 08:00 KST에 실행되면,
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
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
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


def make_url(query: str) -> str:
    return BASE_RSS.format(query=quote(query))


def fetch_feed(url: str):
    """Google News RSS 가져오기."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        return feedparser.parse(resp.text)
    except Exception:
        return feedparser.parse("")


def to_utc(dt):
    """dateutil 이 파싱한 날짜를 UTC로 변환 (tz 없으면 KST 가정 후 UTC로)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)


def normalize_entries(feed, limit: int, block_domains=None,
                      window_start_utc=None, window_end_utc=None):
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

        published_raw = getattr(e, "published", "") or getattr(e, "updated", "")
        if not published_raw:
            continue

        try:
            dt_parsed = dateparser.parse(published_raw)
        except Exception:
            continue

        dt_utc = to_utc(dt_parsed)
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
    items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

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
    # 텔레그램 Markdown용 간단 이스케이프
    return re.sub(r"([_*\[\]()~`>#+\\-=|{}.!])", r"\\\1", text or "")


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
    lines.append("")  # 마지막에 개행
    return "\n".join(lines)


############################
# 카테고리별 수집
############################

def get_category_1(ws, we):
    """
    1) 노동, 육아, 청년, 모성보호, 출산, 채용, 파견근로자, 장애인, 가족돌봄 등 인사노무 뉴스 TOP 10
    """
    query = (
        "(노동 OR 근로 OR 인사노무 OR HR OR 인사팀) "
        "AND (육아 OR 청년 OR 모성보호 OR 출산 OR 채용 OR 모집 OR 파견근로 OR "
        "장애인 고용 OR 가족돌봄 OR 일가정양립)"
    )
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=10,
                             window_start_utc=ws, window_end_utc=we)


def get_category_2(ws, we):
    """
    2) 노동 관계 법령 개정 뉴스 (근로기준법, 모성보호, 남녀고용평등 등) TOP 10
    """
    query = (
        "(근로기준법 OR 노동관계법 OR 노동법 OR 남녀고용평등 OR 모성보호 OR 육아휴직 OR "
        "산업안전보건법 OR 파견근로자보호법 OR 기간제법 OR 고용정책기본법 OR 근로자퇴직급여보장법) "
        "AND (개정 OR 개정안 OR 개편 OR 법 개정 OR 시행령 개정 OR 시행규칙 개정 OR 입법예고)"
    )
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=10,
                             window_start_utc=ws, window_end_utc=we)


def get_category_3(ws, we):
    """
    3) 고용노동부 보도자료 및 정책알림 TOP 5
    - 도메인: moel.go.kr
    """
    query = 'site:moel.go.kr (보도자료 OR 보도 참고자료 OR 정책)'
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=5,
                             window_start_utc=ws, window_end_utc=we)


def get_category_4(ws, we):
    """
    4) 금융위원회 보도자료 및 정책알림 TOP 5
    - 도메인: fsc.go.kr
    """
    query = 'site:fsc.go.kr (보도자료 OR 보도 참고자료 OR 정책)'
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=5,
                             window_start_utc=ws, window_end_utc=we)


def get_category_5(ws, we):
    """
    5) KCGS(한국ESG기준원) 관련 뉴스 TOP 1
    - KCGS에서 직접 발행한 뉴스(kcgs.or.kr)는 제외
    """
    query = '(KCGS OR "한국ESG기준원")'
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=1,
                             block_domains=["kcgs.or.kr"],
                             window_start_utc=ws, window_end_utc=we)


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
        "ESG 일반·금감원(FSS) 관련 알림은 제외하고, 요청한 5개 카테고리만 표시합니다.\n\n"
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
        "⑤ KCGS(한국ESG기준원) 관련 뉴스 TOP 1 (kcgs 직접 발행 제외)",
        get_category_5(ws, we),
        1,
    )

    msg = header + cat1 + cat2 + cat3 + cat4 + cat5
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n(이하 생략됨)"
    return msg


def main():
    msg = build_message()
    tg(msg)


if __name__ == "__main__":
    main()
