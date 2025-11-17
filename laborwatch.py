#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
News → Telegram 통합 알림봇 (인사노무/노동법/고용노동부/금융위/KCGS 전용)

- ESG 일반, 금융감독원(FSS) 관련 알림 없음
- 카테고리:
  1) 인사노무 일반 뉴스 TOP 10
  2) 노동관계 법령 개정 뉴스 TOP 10
  3) 고용노동부 보도자료 및 정책 알림 TOP 5
  4) 금융위원회 보도자료 및 정책 알림 TOP 5
  5) KCGS 관련 뉴스 TOP 1 (kcgs.or.kr 발행 제외)

환경변수:
  TELEGRAM_TOKEN  : 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID: 보내줄 채팅 ID
"""

import os
import re
import textwrap
from datetime import datetime
from urllib.parse import quote

import feedparser
import requests
from dateutil import parser as dateparser


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

BASE_RSS = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
UA = "LaborNewsBot/1.0 (+https://github.com)"


def looks_korean(text: str) -> bool:
    """제목이 거의 영문이면(ESG 영문 기사 등) 버리기 위한 필터."""
    return bool(re.search(r"[가-힣]", text or ""))


def make_url(query: str) -> str:
    return BASE_RSS.format(query=quote(query))


def fetch_feed(url: str):
    # feedparser가 직접 가져가도 되지만 timeout 등을 위해 requests 사용
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        return feedparser.parse(resp.text)
    except Exception:
        return feedparser.parse("")  # 빈 피드


def normalize_entries(feed, limit: int, block_domains=None):
    block_domains = block_domains or []
    items = []
    for e in feed.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        if not title or not link:
            continue
        # 도메인 필터(KCGS 자체 뉴스를 제외할 때 사용)
        if any(dom in link for dom in block_domains):
            continue
        if not looks_korean(title):
            # 한글 거의 없는 기사(영문 ESG 기사 등) 제거
            continue

        # 날짜 파싱
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        try:
            dt = dateparser.parse(published)
        except Exception:
            dt = None

        items.append(
            {
                "title": title,
                "link": link,
                "published": dt,
                "source": getattr(e, "source", getattr(e, "author", "")) or "",
            }
        )

    # 최신순 정렬
    items.sort(key=lambda x: x["published"] or datetime.min, reverse=True)
    # 제목 기준 중복 제거
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


def format_items(title: str, items, max_items: int) -> str:
    if not items:
        return f"*{title}*\n- (해당 기간에 수집된 뉴스가 없습니다)\n\n"

    lines = [f"*{title}*"]
    for i, it in enumerate(items[:max_items], start=1):
        dt = it["published"]
        if dt:
            datestr = dt.strftime("%Y-%m-%d %H:%M")
        else:
            datestr = "날짜 불명"
        # 텔레그램 마크다운 V2 특수문자 간단 이스케이프
        t = escape_md(it["title"])
        link = it["link"]
        lines.append(f"{i}. [{t}]({link})\n   - {datestr}")
    lines.append("")  # 마지막에 개행
    return "\n".join(lines)


def escape_md(text: str) -> str:
    # 텔레그램 Markdown용 간단 이스케이프
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


############################
# 카테고리별 수집 로직
############################

def get_category_1():
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
    return normalize_entries(feed, limit=10)


def get_category_2():
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
    return normalize_entries(feed, limit=10)


def get_category_3():
    """
    3) 고용노동부 보도자료 및 정책알림 TOP 5
    - 도메인: moel.go.kr
    """
    query = 'site:moel.go.kr 고용노동부 보도자료 OR 정책'
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=5)


def get_category_4():
    """
    4) 금융위원회 보도자료 및 정책알림 TOP 5
    - 도메인: fsc.go.kr
    """
    query = 'site:fsc.go.kr (보도자료 OR 보도 참고자료 OR 정책)'
    url = make_url(query)
    feed = fetch_feed(url)
    return normalize_entries(feed, limit=5)


def get_category_5():
    """
    5) KCGS(한국ESG기준원) 관련 뉴스 TOP 1
    - KCGS에서 직접 발행한 뉴스(kcgs.or.kr)는 제외
    """
    query = '(KCGS OR "한국ESG기준원")'
    url = make_url(query)
    feed = fetch_feed(url)
    # kcgs.or.kr 도메인은 제외 (본인 발행이 아니라 "관련 뉴스"만 필요)
    return normalize_entries(feed, limit=1, block_domains=["kcgs.or.kr"])


############################
# 메인
############################

def build_message() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"🔔 인사노무·노동법·정책 뉴스 요약 ({today})\n"
    header += "ESG 일반·금감원(FSS) 관련 알림은 제외, 요청한 5개 카테고리만 표시합니다.\n\n"

    cat1 = format_items(
        "① 노동·육아·청년·모성보호·출산·채용·파견·장애인·가족돌봄 등 인사노무 뉴스 TOP 10",
        get_category_1(),
        10,
    )
    cat2 = format_items(
        "② 노동 관계 법령 개정 관련 뉴스 TOP 10",
        get_category_2(),
        10,
    )
    cat3 = format_items(
        "③ 고용노동부 보도자료·정책 알림 TOP 5",
        get_category_3(),
        5,
    )
    cat4 = format_items(
        "④ 금융위원회 보도자료·정책 알림 TOP 5",
        get_category_4(),
        5,
    )
    cat5 = format_items(
        "⑤ KCGS(한국ESG기준원) 관련 뉴스 TOP 1 (kcgs 직접 발행 제외)",
        get_category_5(),
        1,
    )

    msg = header + cat1 + cat2 + cat3 + cat4 + cat5
    # 텔레그램 4096자 제한에 대비해 대충 잘라두기
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n(이하 생략됨)"
    return msg


def main():
    msg = build_message()
    # 텔레그램 전송
    tg(msg)


if __name__ == "__main__":
    main()
