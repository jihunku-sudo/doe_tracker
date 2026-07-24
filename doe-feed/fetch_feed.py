"""
fetch_feed.py — DOE Clean Energy Feed Collector (Enhanced v2)
=============================================================
① energy.gov 목록 2종 파싱 (기존)
② 신규 항목마다 기사 본문을 직접 fetch → 규칙 기반 필드 추출 (신규)
   sponsor / location / stage / policy / detail / sector / amount_usd_m

외부 AI API 불필요. 결과는 state/article_cache.json 에 캐싱(중복 fetch 방지).

의존성: pip install requests beautifulsoup4
"""

import json
import os
import re
import datetime
import sys
import time

import requests
from bs4 import BeautifulSoup

# ── 수집 대상 ─────────────────────────────────────────────────────────────────
SOURCES = [
    ("projects", "https://www.energy.gov/edf/listings/projects"),
    ("foa",      "https://www.energy.gov/listings/recent-funding-opportunities-announcements"),
]

ARTICLE_FETCH_LIMIT = 30    # 1회 실행당 기사 fetch 최대 건수
ARTICLE_TEXT_LIMIT  = 6000  # 기사 텍스트 사용 최대 길이(chars)
FETCH_DELAY         = 0.6   # 요청 간 딜레이(초) — energy.gov rate limit 배려

# ── 미국 50개 주 ──────────────────────────────────────────────────────────────
US_STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
    "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
    "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
    "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
    "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
    "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
]
# 약자 매핑 (기사에서 자주 쓰이는 형태)
STATE_ABBR = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
    "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina",
    "ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania",
    "RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee",
    "TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
}

# ── 분야 키워드 매핑 ──────────────────────────────────────────────────────────
SECTOR_KW = [
    (r"nuclear|reactor|ap1000|uranium|enrichment|smr|advanced reactor|fission",  "nuclear"),
    (r"transmission|grid|reconductor|hvdc|powerline|substation|interconnect",    "grid"),
    (r"\bsaf\b|aviation fuel|biofuel|renewable diesel|ethanol|biomass",          "biofuel"),
    (r"battery|storage|energy storage|gigafactory|cell manufacturing",           "battery"),
    (r"lithium|graphite|critical mineral|critical material|rare earth|anode|cathode|cobalt|nickel", "minerals"),
    (r"solar|photovoltaic|\bpv\b|virtual power plant",                           "solar"),
    (r"\bev\b|electric vehicle|vehicle manufactur|charging infrastructure",      "ev"),
    (r"carbon capture|\bccs\b|\bdac\b|sequestration|ammonia|hydrogen",           "ccs"),
    (r"coal|natural gas|lng|petroleum|oil|methane",                              "mixed"),
]

# ── 정책·프로그램 패턴 ────────────────────────────────────────────────────────
POLICY_PATTERNS = [
    (r"title\s+17\s+(?:§|section|sec\.?)?\s*1706|energy dominance financing|\bedfp\b", "Title 17 §1706 EDFP"),
    (r"title\s+17\s+(?:§|section|sec\.?)?\s*1703",                                     "Title 17 §1703"),
    (r"title\s+17",                                                                     "Title 17"),
    (r"\batvm\b|advanced technology vehicles? manufacturing",                           "ATVM"),
    (r"eo\s*14241|executive order.*coal|reinvigorat.*coal",                             "EO 14241"),
    (r"inflation reduction act|\bira\b(?!\s+\d)",                                       "IRA"),
    (r"bipartisan infrastructure|\biija\b",                                             "IIJA"),
    (r"defense production act|\bdpa\b",                                                 "DPA"),
    (r"loan guarantee program",                                                         "Title 17 Loan Guarantee"),
]

# ── 단계 키워드 매핑 ──────────────────────────────────────────────────────────
STAGE_PATTERNS = [
    (6, r"operating|disburs|in operation|construction complete|began operations?"),
    (5, r"financial close|loan closed|closing|executed.*loan agreement"),
    (4, r"conditional commitment|conditionally committed|term sheet"),
    (3, r"due diligence|advanced review|under review|negotiat"),
    (2, r"application submitted|has applied|submitted.*application|advance to.*review"),
    (1, r"expression of interest|funding opportunity|foa|nofo|rfi|request for information"
        r"|announces?\s+\$|announces?\s+funding|solicitation|open.*application"),
]

# ── 주관사 추출 패턴 ──────────────────────────────────────────────────────────
SPONSOR_PATTERNS = [
    # "loan to / for CompanyName"
    r"(?:loan|commitment|guarantee|financing)\s+(?:to|for)\s+([A-Z][A-Za-z0-9\s&,\.\-']+?(?:Inc\.?|Corp\.?|LLC\.?|LP\.?|L\.P\.?|Company|Energy|Power|Mining|Technologies?|Solutions?|Industries|Group|Holdings?|Renewables?|Generation|Sciences?|Materials?|Systems?|Partners?|Ventures?))",
    # "CompanyName will receive / has been awarded / received"
    r"([A-Z][A-Za-z0-9\s&,\.\-']+?(?:Inc\.?|Corp\.?|LLC\.?|LP\.?|Company|Energy|Power|Mining|Technologies?|Solutions?|Industries|Group|Holdings?))\s+(?:will receive|has been awarded|received|has secured)",
    # "awarded / committed to CompanyName"
    r"(?:awarded|committed|announced)\s+(?:a\s+)?(?:\$[\d\.,]+\s*(?:billion|million|B|M)\s+)?(?:loan|commitment|grant|guarantee)\s+(?:to|for)\s+([A-Z][A-Za-z0-9\s&,\.\-']+?)(?:\s+to\s+|\s+for\s+|\s+in\s+|,|\.|$)",
    # "Secretary Wright / DOE announced ... [Company]"
    r"(?:announced|unveiled|committed).*?(?:to|for)\s+([A-Z][A-Za-z0-9\s&]+?(?:Inc\.?|Corp\.?|LLC|Energy|Power|Company))",
]

# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def http_get(url: str, timeout: int = 25) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": "doe-feed-bot/2.0 (Samsung E&A DOE Tracker)"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.text


def guess_sector(text: str) -> str | None:
    t = text.lower()
    for pat, key in SECTOR_KW:
        if re.search(pat, t):
            return key
    return None


def guess_amount_m(text: str) -> int | None:
    """금액을 M 단위 정수로 반환. 본문 전체에서 가장 큰(총액) 값 우선."""
    amounts = []
    for m in re.finditer(
        r"\$\s*([\d,\.]+)\s*(billion|million|trillion|\bB\b|\bM\b|\bT\b)", text, re.I
    ):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit.startswith("t"):
            amounts.append(int(n * 1_000_000))
        elif unit.startswith("b"):
            amounts.append(int(n * 1000))
        else:
            amounts.append(int(n))
    return max(amounts) if amounts else None


def guess_category(source: str, text: str) -> str:
    t = text.lower()
    if source == "projects":
        return "loan-project"
    if "request for information" in t or re.search(r"\brfi\b", t):
        return "RFI"
    if re.search(r"notice of funding|\bnofo\b|\bfoa\b|funding opportunity", t):
        return "NOFO"
    if "award" in t:
        return "Award"
    if re.search(r"solicitation|contract|procure", t):
        return "Procurement"
    return "NOFO"


def parse_date(text: str) -> str:
    m = re.search(r"([A-Z][a-z]+ \d{1,2},\s*\d{4})", text)
    if not m:
        return ""
    try:
        return datetime.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


# ── 기사 본문 fetch & 파싱 ────────────────────────────────────────────────────
def fetch_article_text(url: str) -> str:
    """기사 본문 텍스트를 추출합니다."""
    try:
        html = http_get(url)
        soup = BeautifulSoup(html, "html.parser")
        main = soup.find("main") or soup.find("article") or soup.body
        if not main:
            return ""
        for tag in main.find_all(["nav", "footer", "script", "style", "header", "aside"]):
            tag.decompose()
        return main.get_text(" ", strip=True)[:ARTICLE_TEXT_LIMIT]
    except Exception as e:
        print(f"    [warn] article fetch failed: {e}", file=sys.stderr)
        return ""


def extract_sponsor(text: str) -> str | None:
    """주관사(대출/보조금 수혜 기업)를 추출합니다."""
    # FOA/NOFO 계열은 다수 신청자 대상이므로 주관사 없음
    if re.search(r"funding opportunity|request for information|\bnofo\b|\brfi\b|open to applications", text.lower()):
        return None  # "DOE Grant Program" 처리는 호출 측에서

    for pat in SPONSOR_PATTERNS:
        m = re.search(pat, text)
        if m:
            sponsor = m.group(1).strip().rstrip(",.")
            # 너무 짧거나 일반 단어면 제외
            if len(sponsor) > 3 and not re.match(r"^(The|A|An|This|That|These|Those)$", sponsor):
                return sponsor
    return None


def extract_location(text: str) -> str | None:
    """미국 주(州) 또는 도시를 추출합니다."""
    found = []
    for state in US_STATES:
        # 주 이름이 단어 경계로 등장하는지 확인
        if re.search(r'\b' + re.escape(state) + r'\b', text):
            found.append(state)
    if found:
        # 최대 3개 주만 표시
        return ", ".join(found[:3]) + ("" if len(found) <= 3 else f" 외 {len(found)-3}개 주")

    # 약자 형태 탐지 (예: "TX", "CA")
    abbr_found = []
    for m in re.finditer(r'\b([A-Z]{2})\b', text):
        if m.group(1) in STATE_ABBR:
            abbr_found.append(STATE_ABBR[m.group(1)])
    if abbr_found:
        unique = list(dict.fromkeys(abbr_found))[:3]
        return ", ".join(unique)

    # "nationwide" 표현
    if re.search(r"nationwide|across the (country|nation|united states)", text.lower()):
        return "Nationwide"
    return None


def extract_stage(text: str, source: str) -> int:
    """DOE 심사 단계(1~6)를 추출합니다."""
    t = text.lower()
    for stage, pat in STAGE_PATTERNS:
        if re.search(pat, t):
            return stage
    # source가 projects이면 최소 2단계(신청서 접수됨)
    return 2 if source == "projects" else 1


def extract_policy(text: str) -> str | None:
    """적용 법령·프로그램을 추출합니다."""
    t = text.lower()
    for pat, label in POLICY_PATTERNS:
        if re.search(pat, t, re.I):
            return label
    return None


def extract_detail(text: str, title: str) -> str:
    """기사 첫 유의미 단락을 요약으로 사용합니다."""
    # 짧은 문장 건너뛰기, 최대 300자
    sentences = re.split(r'(?<=[.!?])\s+', text)
    detail_parts = []
    char_count = 0
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 40:
            continue
        # 헤더/내비게이션 텍스트 제외
        if re.match(r"^(Skip|Share|Follow|Subscribe|Sign up|Menu|Home|Back)", sent):
            continue
        detail_parts.append(sent)
        char_count += len(sent)
        if char_count >= 280 or len(detail_parts) >= 2:
            break
    return " ".join(detail_parts)[:300] if detail_parts else title[:200]


def extract_era(text: str, date: str) -> str:
    """정책 시기를 판별합니다 (edf=트럼프 2기, lpo=바이든)."""
    t = text.lower()
    if re.search(r"energy dominance|edfp|secretary wright|trump|executive order 14", t):
        return "edf"
    # 날짜 기준: 2025-01-20 이후는 edf 가능성 높음
    if date and date >= "2025-01-20":
        return "edf"
    return "lpo"


# ── 목록 페이지 파싱 ──────────────────────────────────────────────────────────
def parse_listing(source: str, html: str, base: str = "https://www.energy.gov") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen: set[str] = set()

    for a in soup.select('a[href*="/articles/"]'):
        title = a.get_text(" ", strip=True)
        if len(title) < 12:
            continue
        href = a.get("href", "")
        url = href if href.startswith("http") else base + href
        if url in seen:
            continue
        seen.add(url)

        container = a.find_parent(["article", "li", "div"]) or a.parent
        ctext = container.get_text(" ", strip=True) if container else title
        p = container.find("p") if container else None
        blurb = p.get_text(" ", strip=True) if p else ""
        hay = f"{title} {blurb}"

        items.append({
            "id":           url,
            "date":         parse_date(ctext),
            "title":        title,
            "url":          url,
            "source":       source,
            "category":     guess_category(source, hay),
            "sector":       guess_sector(hay) or "mixed",
            "amount_usd_m": guess_amount_m(hay),
            "blurb":        blurb[:220],
            # 기사 상세 필드 (phase 2에서 채워짐)
            "enriched":     False,
            "sponsor":      None,
            "location":     None,
            "stage":        None,
            "policy":       None,
            "detail_ai":    None,
            "era":          None,
        })
    return items


# ── 기사 상세 보강 ────────────────────────────────────────────────────────────
def enrich_from_article(item: dict, article_text: str) -> None:
    """기사 본문에서 상세 필드를 추출해 item을 in-place 업데이트합니다."""
    full = f"{item['title']} {article_text}"

    # 금액: 기사 본문이 더 정확
    amt = guess_amount_m(full)
    if amt:
        item["amount_usd_m"] = amt

    # 분야: 기사 본문 기준 재판정
    sector = guess_sector(full)
    if sector:
        item["sector"] = sector

    # 날짜: 기사 본문에서 더 정확한 날짜 추출
    date = parse_date(article_text)
    if date and (not item["date"] or date > item["date"]):
        item["date"] = date

    # 단계
    item["stage"]    = extract_stage(article_text, item["source"])

    # 주관사
    sponsor = extract_sponsor(article_text)
    item["sponsor"]  = sponsor or ("DOE Grant Program" if item["category"] in ("NOFO", "RFI") else None)

    # 위치
    item["location"] = extract_location(article_text)

    # 정책
    item["policy"]   = extract_policy(article_text)

    # 상세 요약
    item["detail_ai"] = extract_detail(article_text, item["title"])

    # 시기
    item["era"]      = extract_era(article_text, item["date"])

    item["enriched"] = True


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    # ── Phase 1: 목록 수집 ────────────────────────────────────────────────
    all_items: list[dict] = []
    for source, url in SOURCES:
        try:
            print(f"[fetch] {source}: {url}")
            all_items += parse_listing(source, http_get(url))
        except Exception as e:
            print(f"[warn] {source} listing failed: {e}", file=sys.stderr)

    # 중복 제거 + 최신순 정렬
    dedup: dict[str, dict] = {}
    for it in all_items:
        dedup[it["url"]] = it
    items = sorted(dedup.values(), key=lambda x: x["date"], reverse=True)

    # 이전 실행분과 비교해 신규 감지
    prev_path  = "state/prev.json"
    cache_path = "state/article_cache.json"
    prev_urls: set[str] = set()
    if os.path.exists(prev_path):
        with open(prev_path, encoding="utf-8") as f:
            prev_urls = set(json.load(f).get("urls", []))

    # 기사 캐시 로드 (이미 처리한 URL 재사용)
    article_cache: dict[str, dict] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            article_cache = json.load(f)

    new_urls = [it["url"] for it in items if it["url"] not in prev_urls]
    print(f"[info] items={len(items)}  new={len(new_urls)}")

    # ── Phase 2: 신규 기사 상세 fetch & 보강 ──────────────────────────────
    fetch_count = 0
    for item in items:
        url = item["url"]

        # 캐시에 이미 있으면 재사용
        if url in article_cache:
            cached = article_cache[url]
            item.update({k: v for k, v in cached.items() if k != "url"})
            continue

        # 신규이거나 미보강 항목만 fetch (실행당 최대 ARTICLE_FETCH_LIMIT건)
        if url not in new_urls or fetch_count >= ARTICLE_FETCH_LIMIT:
            continue

        print(f"  [enrich] {item['title'][:55]}...")
        article_text = fetch_article_text(url)
        if article_text:
            enrich_from_article(item, article_text)
            # 캐시에 저장
            article_cache[url] = {
                k: item[k] for k in
                ["enriched","sponsor","location","stage","policy","detail_ai","era","sector","amount_usd_m","date"]
            }
        fetch_count += 1
        time.sleep(FETCH_DELAY)

    print(f"[info] enriched {fetch_count} new articles from full text")

    # ── Phase 3: 저장 ─────────────────────────────────────────────────────
    feed = {
        "generated_at":   datetime.datetime.now(datetime.timezone.utc)
                          .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources":        [u for _, u in SOURCES],
        "new_since_last": new_urls,
        "items":          items,
    }

    os.makedirs("docs",  exist_ok=True)
    os.makedirs("state", exist_ok=True)

    with open("docs/feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    print("[done] docs/feed.json saved")

    with open(prev_path, "w", encoding="utf-8") as f:
        json.dump({"urls": [it["url"] for it in items]}, f, ensure_ascii=False)

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(article_cache, f, ensure_ascii=False, indent=2)
    print(f"[done] article cache: {len(article_cache)} entries")

    if len(items) == 0:
        print("[warn] items=0 — 파싱 실패. 셀렉터 점검 필요.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
