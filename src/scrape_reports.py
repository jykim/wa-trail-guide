"""Scrape recent trip reports for each trail. Output: data/reports.json.

For each trail in trails.json, fetch <trail_url>/@@related_tripreport_listing
(returns ~5 most recent reports) and keep the top 3.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup

from common import DATA, fetch

REPORTS_PER_TRAIL = 3
TITLE_DATE_RE = re.compile(r"—\s*([A-Za-z]+\.?\s+\d+,\s+\d{4})")


@dataclass
class Report:
    url: str
    hike_date: str            # raw text like "May. 8, 2026"
    author: str
    issues: str               # "Beware of: ..." text or ""
    feature_flags: list[str]  # ["Wildflowers blooming", "Snow", ...]
    body: str                 # report-text body


def parse_report(row) -> Report | None:
    title = row.find(class_="listitem-title")
    if not title:
        return None
    a = title.find("a", href=True)
    if not a:
        return None
    title_text = a.get_text(" ", strip=True)
    m = TITLE_DATE_RE.search(title_text)
    hike_date = m.group(1) if m else ""

    author_el = row.find(class_="wta-icon-headline__text")
    author = author_el.get_text(strip=True) if author_el else ""

    issues_el = row.find(class_="trail-issues")
    issues = ""
    if issues_el:
        issues = issues_el.get_text(" ", strip=True)
        issues = re.sub(r"^Beware of:\s*", "", issues, flags=re.I)

    flags: list[str] = []
    stats = row.find(class_="trip-report-stats")
    if stats:
        for li in stats.find_all("li"):
            txt = li.get_text(" ", strip=True)
            if txt:
                flags.append(txt)

    body_el = row.find(class_="report-text")
    body = body_el.get_text(" ", strip=True) if body_el else ""

    return Report(
        url=a["href"],
        hike_date=hike_date,
        author=author,
        issues=issues,
        feature_flags=flags,
        body=body,
    )


def fetch_reports(trail_url: str) -> list[Report]:
    url = trail_url.rstrip("/") + "/@@related_tripreport_listing"
    resp = fetch(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    reports: list[Report] = []
    for row in soup.find_all(class_="item-row"):
        r = parse_report(row)
        if r is not None:
            reports.append(r)
        if len(reports) >= REPORTS_PER_TRAIL:
            break
    return reports


def main() -> int:
    trails_path = DATA / "trails.json"
    trails = json.loads(trails_path.read_text())
    out: dict[str, list[dict]] = {}
    for i, t in enumerate(trails, 1):
        print(f"[reports] ({i}/{len(trails)}) {t['name']}", file=sys.stderr, flush=True)
        try:
            rs = fetch_reports(t["url"])
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
            rs = []
        out[t["slug"]] = [asdict(r) for r in rs]
        print(f"  + {len(rs)} reports", file=sys.stderr)

    path = DATA / "reports.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[reports] wrote {sum(len(v) for v in out.values())} reports -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
