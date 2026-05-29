"""Render dist/index.html from trails.json + status.json."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import DATA, DIST, TEMPLATES

STALE_DAYS = 30
FRESH_DAYS = 7
CHANGE_WINDOW_DAYS = 14  # how long a status change stays in the Updates section


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

        prev_acc = s.get("prev_accessibility") or ""
        prev_sum = s.get("prev_summary") or ""
        change_reason = s.get("change_reason") or ""
        changed_at = s.get("changed_at") or ""
        days_since_change = _days_since(changed_at)
        is_recently_changed = bool(
            days_since_change is not None
            and days_since_change <= CHANGE_WINDOW_DAYS
            and (prev_acc or prev_sum or change_reason in ("new", "new_report"))
        )

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
                "prev_accessibility": prev_acc,
                "prev_summary": prev_sum,
                "change_reason": change_reason,
                "changed_at": changed_at,
                "days_since_change": days_since_change,
                "is_recently_changed": is_recently_changed,
            },
        })

    regions = sorted({t["region"] for t in trails})

    camps_path = DATA / "campgrounds.json"
    campgrounds = []
    if camps_path.exists():
        try:
            campgrounds = json.loads(camps_path.read_text())
        except json.JSONDecodeError:
            campgrounds = []

    rest_path = DATA / "rest_areas.json"
    rest_areas = []
    if rest_path.exists():
        try:
            rest_areas = json.loads(rest_path.read_text())
        except json.JSONDecodeError:
            rest_areas = []

    or_trails_path = DATA / "oregon_trails.json"
    oregon_trails = []
    if or_trails_path.exists():
        try:
            oregon_trails = json.loads(or_trails_path.read_text())
        except json.JSONDecodeError:
            oregon_trails = []

    data_blob = {
        "trails": merged,
        "regions": regions,
        "campgrounds": campgrounds,
        "rest_areas": rest_areas,
        "oregon_trails": oregon_trails,
    }
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    # "Updated" reflects when the trail data was last refreshed (status.json mtime),
    # not when this page happened to be re-rendered.
    status_file = DATA / "status.json"
    updated_dt = datetime.fromtimestamp(
        status_file.stat().st_mtime, tz=timezone.utc
    ).astimezone()
    generated_at = updated_dt.strftime("%Y-%m-%d %H:%M %Z")

    tpl = env.get_template("dashboard.html.j2")
    html = tpl.render(
        generated_at=generated_at,
        trail_count=len(merged),
        data_json=json.dumps(data_blob, ensure_ascii=False),
    )

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "index.html"
    out.write_text(html)
    print(f"[render] wrote {out} ({len(html)/1024:.1f} KB, {len(merged)} trails)", file=sys.stderr)

    # Admin page: published but unlinked from the UI (reachable only by direct URL,
    # /admin or /admin.html — see the server's path rewrite). Still password-gated.
    admin_tpl = env.get_template("admin.html.j2")
    admin_html = admin_tpl.render(generated_at=generated_at, trail_count=len(merged))
    admin_out = DIST / "admin.html"
    admin_out.write_text(admin_html)
    print(f"[render] wrote {admin_out} ({len(admin_html)/1024:.1f} KB)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
