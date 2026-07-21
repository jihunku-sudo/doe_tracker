"""
fetch_feed.py — DOE Clean Energy Feed Collector
================================================
energy.gov 공개 목록 2종을 파싱해 docs/feed.json 으로 저장합니다.
매일 GitHub Actions (UTC 23:00 / KST 08:00) 에서 자동 실행됩니다.

의존성: pip install requests beautifulsoup4
"""

import json
import os
import re
import datetime
import sys

import requests
from bs4 import BeautifulSoup

# ── 수집 대상 ─────────────────────────────────────────────────────────────────
SOURCES = [
    ("projects", "https://www.energy.gov/edf/listings/projects"),
    ("foa",      "https://www.energy.gov/listings/recent-funding-opportunities-announcements"),
]

# ── 분야 키워드 매핑 ──────────────────────────────────────────────────────────
SECTOR_KW = [
    (r"nuclear|reactor|ap1000|uranium|enrichment",                  "nuclear"),
    (r"transmission|grid|reconductor|hvdc",                         "grid"),
    (r"\bsaf\b|aviation fuel|biofuel|renewable diesel",             "biofuel"),
    (r"battery|storage",                                            "battery"),
    (r"lithium|graphite|critical mineral|critical material|rare earth|anode|cathode", "minerals"),
    (r"solar|photovoltaic|\bpv\b|virtual power",                    "solar"),
    (r"\bev\b|electric vehicle|vehicle manufactur",                 "ev"),
    (r"ammonia|carbon capture|\bccs\b|sequestration",               "ccs"),
]


def guess_sector(text: str) -> str | None:
    t = text.lower()
    for pat, key in SECTOR_KW:
        if re.search(pat, t):
            return key
    return None


def guess_amount_m(text: str) -> int | None:
    m = re.search(r"\$\s?([\d,.]+)\s?(billion|million|b\b|m\b)", text, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2).lower()
    return round(n * 1000) if unit.startswith("b") else round(n)


def guess_category(source: str, text: str) -> str:
    t = text.lower()
    if source == "projects":
        return "loan-project"
    if "request for information" in t or re.search(r"\brfi\b", t) or "seeks hosts" in t:
        return "RFI"
    if "notice of funding" in t or re.search(r"\bnofo\b|\bfoa\b", t) or "funding opportunity" in t:
        return "NOFO"
    if "award" in t:
        return "Award"
    if "solicitation" in t or "contract" in t or "procure" in t:
        return "Procurement"
    return "NOFO"


def parse_date(text: str) -> str:
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not m:
        return ""
    try:
        return datetime.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def fetch(url: str) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": "doe-feed-bot/1.0 (Samsung E&A DOE Tracker)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse_listing(source: str, html: str, base: str = "https://www.energy.gov") -> list[dict]:
    """energy.gov 목록 페이지에서 기사 링크를 추출해 정규화된 항목 리스트를 반환합니다."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen: set[str] = set()

    # /articles/ 또는 /edf/articles/ 경로를 가진 앵커 태그를 기준으로 파싱
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
            "sector":       guess_sector(hay),
            "amount_usd_m": guess_amount_m(hay),
            "blurb":        blurb[:220],
        })

    return items


def main() -> None:
    # ── 수집 ─────────────────────────────────────────────────────────────────
    all_items: list[dict] = []
    for source, url in SOURCES:
        try:
            print(f"[fetch] {source}: {url}")
            all_items += parse_listing(source, fetch(url))
        except Exception as e:
            print(f"[warn] {source} fetch/parse failed: {e}", file=sys.stderr)

    # ── 중복 제거 & 최신순 정렬 ───────────────────────────────────────────────
    dedup: dict[str, dict] = {}
    for it in all_items:
        dedup[it["url"]] = it
    items = sorted(dedup.values(), key=lambda x: x["date"], reverse=True)

    # ── 신규 감지 (이전 실행분과 비교) ────────────────────────────────────────
    prev_path = "state/prev.json"
    prev_urls: set[str] = set()
    if os.path.exists(prev_path):
        with open(prev_path, encoding="utf-8") as f:
            prev_urls = set(json.load(f).get("urls", []))
    new_urls = [it["url"] for it in items if it["url"] not in prev_urls]
    print(f"[info] items={len(items)}  new={len(new_urls)}")

    # ── feed.json 생성 ────────────────────────────────────────────────────────
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

    # state 갱신
    with open(prev_path, "w", encoding="utf-8") as f:
        json.dump({"urls": [it["url"] for it in items]}, f, ensure_ascii=False)

    # ── 신규 없으면 정상 종료, 있으면 0 종료(워크플로가 변경분 커밋) ───────────
    if len(items) == 0:
        print("[warn] items=0 — 파싱 실패 가능성. 셀렉터 점검 필요.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
