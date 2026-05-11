"""Render dist/index.html from trails.json + status.json."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import DATA, DIST, TEMPLATES

STALE_DAYS = 30
FRESH_DAYS = 7


def _days_since(iso: str) -> int | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return (date.today() - d).days


def main() -> int:
    trails = json.loads((DATA / "trails.json").read_text())
    status = json.loads((DATA / "status.json").read_text())

    drive_path = DATA / "drive_cache.json"
    drive_cache = json.loads(drive_path.read_text()) if drive_path.exists() else {}

    merged = []
    for t in trails:
        s = status.get(t["slug"], {})
        last = s.get("last_report_date") or ""
        days = _days_since(last)
        is_stale = days is None or days > STALE_DAYS
        is_fresh = days is not None and days <= FRESH_DAYS

        drive: dict = {}
        if t.get("lat") is not None and t.get("lng") is not None:
            key = f"{t['lat']:.5f},{t['lng']:.5f}"
            drive = drive_cache.get(key, {})

        merged.append({
            **t,
            "drive_seattle_min":  drive.get("seattle_min"),
            "drive_seattle_mi":   drive.get("seattle_mi"),
            "drive_bellevue_min": drive.get("bellevue_min"),
            "drive_bellevue_mi":  drive.get("bellevue_mi"),
            "status": {
                "accessibility": s.get("accessibility") or "unknown",
                "accessibility_reason": s.get("accessibility_reason") or "",
                "snow_line_ft": s.get("snow_line_ft"),
                "road_status": s.get("road_status") or "unknown",
                "bugs": s.get("bugs") or "unknown",
                "wildflowers": s.get("wildflowers") or "unknown",
                "summary": s.get("summary") or "",
                "last_report_date": last,
                "days_since_report": days,
                "is_stale": is_stale,
                "is_fresh": is_fresh,
            },
        })

    regions = sorted({t["region"] for t in trails})

    data_blob = {"trails": merged, "regions": regions}
    now = datetime.now(timezone.utc).astimezone()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("dashboard.html.j2")
    html = tpl.render(
        week_of=now.strftime("%Y-%m-%d"),
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        trail_count=len(merged),
        data_json=json.dumps(data_blob, ensure_ascii=False),
    )

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "index.html"
    out.write_text(html)
    print(f"[render] wrote {out} ({len(html)/1024:.1f} KB, {len(merged)} trails)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
