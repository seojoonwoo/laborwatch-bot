# 카테고리(출처 그룹)별 피드/페이지 목록
# - 실제 발송 라벨은 laborwatch.py 의 categorize()에서 최종 결정됩니다.

FEEDS = {
    # 법령/노동 (공식 RSS)
    "법령-입법예고": [
        "http://open.moleg.go.kr/data/xml/li_rssSH01.xml",  # 법제처 입법예고 RSS
        "https://www.moel.go.kr/rss/lawinfo.do",           # 고용노동부 입법·행정예고
    ],
    "법령-시행법령": [
        "http://open.moleg.go.kr/data/xml/ll_rssSH02.xml", # 최신 시행법령 RSS
    ],
    "노동-부처소식": [
        "https://www.moel.go.kr/rss/notice.do",            # 알려드립니다
        "https://www.moel.go.kr/rss/policy.do",            # 정책자료
    ],

    # 금융당국
    "금융위-보도자료": [
        "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111",  # 보도자료
        "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0112",  # 보도설명
        "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0114",  # 공지
    ],
    "금감원-DART": [
        # DART 최신공시(메인/목록) — HTML 파싱으로 제목/링크/일자 추출
        "https://dart.fss.or.kr/dsac001/main.do",
    ],

    # 뉴스(키워드 RSS: Google News)
    "노동-뉴스": [
        "https://news.google.com/rss/search?q=%EB%85%B8%EB%8F%99+OR+%EA%B7%BC%EB%A1%9C%EA%B8%B0%EC%A4%80%EB%B2%95+OR+%EB%AA%A8%EC%84%B1%EB%B3%B4%ED%98%B8+OR+%EC%9C%A1%EC%95%84+OR+%EB%82%A8%EB%85%80%EA%B3%A0%EC%9A%A9%ED%8F%89%EB%93%B1+OR+%EB%85%B8%EC%82%AC%EA%B4%80%EA%B3%84",
    ],
    "금융-뉴스": [
        "https://news.google.com/rss/search?q=%EA%B8%88%EC%9C%B5%EC%9C%84%EC%9B%90%ED%9A%8C+OR+%EA%B8%88%EC%9C%84+OR+%EA%B8%88%EC%9C%84%ED%95%98+OR+%EA%B8%88%EC%9C%84%20%EB%B3%B4%EB%8F%84%EC%9E%90%EB%A3%8C+OR+%EA%B8%88%EC%9C%84%20%EC%A0%95%EC%B1%85+OR+%EA%B8%88%EA%B0%90%EC%9B%90+OR+DART",
    ],
    "ESG-뉴스": [
        "https://news.google.com/rss/search?q=ESG+OR+%EC%A7%80%EC%86%8D%EA%B0%80%EB%8A%A5%EA%B2%BD%EC%98%81+OR+%EC%A7%80%EB%B0%B0%EA%B5%AC%EC%A1%B0",
        "https://news.google.com/rss/search?q=%ED%95%9C%EA%B5%ADESG%EA%B8%B0%EC%A4%80%EC%9B%90+OR+KCGS",
    ],
}
