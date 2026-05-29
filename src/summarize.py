"""Use OpenAI to summarize each trail's recent reports into a structured status.

Output: data/status.json keyed by slug.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from common import DATA

load_dotenv(dotenv_path=DATA.parent / ".env")
# ~/dotEnv is the canonical source for OPENAI_API_KEY; loads after .env so it wins on overlap.
load_dotenv(dotenv_path=Path.home() / "dotEnv", override=True)

MODEL = "gpt-4o-mini"
MAX_TOKENS = 600
PROMPT_VERSION = "v6"  # bump to invalidate the summary cache when the system prompt changes

SYSTEM_PROMPT = """You classify hiking-trail accessibility from recent WTA trip reports.

For each trail you receive: name, region, length, elevation gain/highpoint, and the last few trip reports (date, author, beware-of issues, feature flags, body text).

Return the structured fields via the function call.

**Recency is authoritative.** Report 1 is the most recent. When reports describe different conditions, the most recent report describes current conditions; older reports describe past conditions that may have changed. If Report 1 explicitly says a hazard has been repaired/cleared/melted out, trust that over older reports that mentioned the hazard. Only fall back to older reports for facts the latest report does not contradict.

When evidence is weak or absent, use the "unknown" sentinel for road/bugs/wildflowers and pick the best-supported accessibility class.

Accessibility rules. Classify by what a typical hiker encounters on the portion of the route they actually walk — not by whether snow is mentioned anywhere.
- "open": hikeable by a fit hiker in regular boots with no special gear for the part of the route a typical hiker completes. Minor blowdowns, mud, bugs, or small / optional / easily-avoided snow patches do not disqualify. Snow lying only above the normal turnaround or destination (e.g. a summit scramble most hikers skip) still counts as open.
- "snow_gear": choose this ONLY when reports describe snow or ice actually on the route that a typical hiker would need traction (microspikes, crampons, snowshoes, ice axe) to cross safely, OR significant route-finding across continuous snow, OR a snowmelt-swollen creek crossing impassable without a workaround. The hazard must be snow or ice specifically. Require evidence that traction was needed for snow/ice on the actual hike — not merely that snow was visible, patchy, on surrounding peaks, or above where most people turn around. A lone mention of a patch that hikers simply walked around stays "open".
- "closed": the trail or its access is impassable for a typical hiker — road washed out and no walkable workaround, trailhead permit not yet open with no alternative, severe trail damage, active fire closure, etc.

snow_gear is strictly about snow or ice ON THE TRAIL. Traction or trekking poles recommended for loose rock, scree, talus, rock fields, scrambling, steep dirt, mud, slippery boardwalks, or general stability are NOT snow_gear. Neither are blowdowns, bugs, or sun exposure. If the reason someone wants traction/poles is anything other than snow or ice on the trail, classify "open" (or "closed" if genuinely impassable) — never snow_gear.

Classify the named trail to its standard destination only. Ignore snow, scrambling, or route-finding that a report describes for an off-route extension or a higher summit beyond that destination (e.g. continuing to a peak past the lake the trail is named for).

When the trail is substantially clear of snow — snow-free, or only small/patchy snow hikers walked around — classify "open" even if traction is mentioned. BUT if reports describe the route as mostly, largely, or continuously snow-covered (a snowfield, snow most of the way, snow from the parking lot/trailhead, etc.), classify "snow_gear" even when a strong or experienced hiker reports not strictly needing traction — a typical hiker will want it. "Mostly snow-covered" is snow_gear, not open.

snow_line_ft: integer elevation in feet if a report explicitly mentions where snow starts (e.g., "snow above 4500ft"). Otherwise null.

summary: one tight sentence (max ~20 words) anchored to a specific fact from Report 1 (the latest). Hard rules:
- Do NOT start with "You should go", "This weekend", "Expect", "The trail is in great shape", or any other boilerplate opener.
- Do NOT claim anything (wildflowers, bugs, snow, road) unless the latest report's body OR feature flags OR issues say it. If unmentioned, omit.
- Lead with the single most decision-relevant concrete fact: e.g. "Parking lot full by 8am" / "Snow patches above 4500ft, microspikes recommended" / "Landslide repaired; route requires light scrambling" / "Road washed out, no access".
- Use specifics that appear in the report text (mileage, elevation, conditions, hazards), not generic phrases.

last_report_date: ISO YYYY-MM-DD of the most recent report's hike date.
"""

RECORD_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "record_status",
        "description": "Record the structured trail status derived from recent trip reports.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "accessibility": {
                    "type": "string",
                    "enum": ["open", "snow_gear", "closed"],
                },
                "accessibility_reason": {
                    "type": "string",
                    "description": "One short sentence justifying the accessibility classification.",
                },
                "snow_line_ft": {
                    "type": ["integer", "null"],
                    "description": "Elevation in feet where snow begins, or null if not mentioned.",
                },
                "road_status": {
                    "type": "string",
                    "enum": ["clear", "rough", "closed", "unknown"],
                },
                "bugs": {
                    "type": "string",
                    "enum": ["none", "some", "bad", "unknown"],
                },
                "wildflowers": {
                    "type": "string",
                    "enum": ["none", "starting", "peak", "past", "unknown"],
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentences: should I go this weekend, and what to expect?",
                },
                "last_report_date": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD date of the most recent report.",
                },
            },
            "required": [
                "accessibility",
                "accessibility_reason",
                "snow_line_ft",
                "road_status",
                "bugs",
                "wildflowers",
                "summary",
                "last_report_date",
            ],
        },
    },
}


def build_user_message(trail: dict, reports: list[dict]) -> str:
    lines = [
        f"Trail: {trail['name']}",
        f"Region: {trail['region']} > {trail.get('subregion','')}",
        f"Length: {trail.get('length_miles')} miles, gain {trail.get('elev_gain_ft')} ft, highpoint {trail.get('highpoint_ft')} ft",
        "",
        "Recent trip reports (most recent first):",
    ]
    if not reports:
        lines.append("(no recent reports)")
    for i, r in enumerate(reports, 1):
        lines.append(f"\n[Report {i}] hike date: {r.get('hike_date')} — author: {r.get('author')}")
        if r.get("issues"):
            lines.append(f"  Beware of: {r['issues']}")
        flags = r.get("feature_flags") or []
        if flags:
            lines.append(f"  Feature flags: {', '.join(flags)}")
        body = (r.get("body") or "").strip()
        if body:
            if len(body) > 2000:
                body = body[:2000] + "…"
            lines.append(f"  Body: {body}")
    return "\n".join(lines)


def summarize_trail(client: OpenAI, trail: dict, reports: list[dict]) -> dict:
    user_text = build_user_message(trail, reports)
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        tools=[RECORD_STATUS_TOOL],
        tool_choice={"type": "function", "function": {"name": "record_status"}},
    )
    choice = resp.choices[0]
    tool_calls = choice.message.tool_calls or []
    if not tool_calls:
        raise RuntimeError(f"No tool call returned for {trail['slug']}")
    return json.loads(tool_calls[0].function.arguments)


def _cache_key(reports: list[dict]) -> str:
    """Stable signature for the report set so we can skip re-summarization when nothing changed."""
    if not reports:
        return f"{PROMPT_VERSION}::empty"
    latest = reports[0]
    return f"{PROMPT_VERSION}::{latest.get('url','')}"


_SNOW_RE = re.compile(
    r"(snow|microspike|crampon|post-?hol|snowshoe|ice ?axe|\bice\b|\bicy\b|glissad|spikes)",
    re.I,
)


def _enforce_snow_evidence(status: dict) -> dict:
    """Safety net: gpt-4o-mini sometimes files non-snow hazards (mud, loose rock, scree,
    blowdowns, slick boardwalks) under snow_gear because it lacks an intermediate bucket.
    If it returns snow_gear but its own reason cites no snow/ice, downgrade to open."""
    if status.get("accessibility") == "snow_gear":
        if not _SNOW_RE.search(status.get("accessibility_reason") or ""):
            status["accessibility"] = "open"
    return status


def main(only_non_open: bool = False) -> int:
    """Summarize trail reports into status.json.

    only_non_open: when True, skip the OpenAI call for trails whose current status is
    "open" (reuse their cached status), even if a new report came in. Cheaper/faster
    refresh focused on snow_gear/closed/unknown trails; won't catch open->closed flips
    until a full refresh.
    """
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set (check .env or shell env)", file=sys.stderr)
        return 1

    trails = json.loads((DATA / "trails.json").read_text())
    reports_by_slug = json.loads((DATA / "reports.json").read_text())

    status_path = DATA / "status.json"
    prev_status: dict[str, dict] = {}
    if status_path.exists():
        try:
            prev_status = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            prev_status = {}

    client = OpenAI()
    out: dict[str, dict] = {}
    hits = 0
    misses = 0
    skipped_open = 0
    miss_slugs: set[str] = set()
    for i, t in enumerate(trails, 1):
        slug = t["slug"]
        rs = reports_by_slug.get(slug, [])
        sig = _cache_key(rs)
        cached = prev_status.get(slug)
        if cached and cached.get("_cache_sig") == sig:
            out[slug] = cached
            hits += 1
            continue

        if only_non_open and cached and cached.get("accessibility") == "open":
            # Non-open-only mode: leave currently-open trails on their cached status.
            out[slug] = cached
            skipped_open += 1
            continue

        misses += 1
        miss_slugs.add(slug)
        print(f"[summarize] ({i}/{len(trails)}) {t['name']} ({len(rs)} reports)", file=sys.stderr, flush=True)
        try:
            status = summarize_trail(client, t, rs)
            _enforce_snow_evidence(status)
            status["_cache_sig"] = sig
            out[slug] = status
            print(f"  -> {status['accessibility']}: {status['summary'][:90]}", file=sys.stderr)
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
            out[slug] = {
                "accessibility": "unknown",
                "accessibility_reason": f"summarization failed: {e}",
                "snow_line_ft": None,
                "road_status": "unknown",
                "bugs": "unknown",
                "wildflowers": "unknown",
                "summary": "Unable to summarize recent reports.",
                "last_report_date": "",
                "_cache_sig": sig,
            }

    # Track changes vs the previous run. Two signals, in priority order:
    #   1. accessibility flipped between buckets (open/snow_gear/closed) — most decision-relevant
    #   2. summary text changed (driven by a new latest trip report, even within the same bucket)
    # Cached entries (latest_report_url unchanged) inherit prev_* / changed_at fields from the
    # previous entry, so a change stays visible across runs until the trail changes again.
    today_iso = date.today().isoformat()
    accessibility_changes = summary_changes = new_report_changes = 0
    for slug, entry in out.items():
        prev_entry = prev_status.get(slug) or {}
        prev_acc = prev_entry.get("accessibility")
        cur_acc = entry.get("accessibility")
        prev_sum = (prev_entry.get("summary") or "").strip()
        cur_sum = (entry.get("summary") or "").strip()

        if prev_acc and cur_acc and prev_acc != cur_acc:
            entry["prev_accessibility"] = prev_acc
            entry["changed_at"] = today_iso
            entry["change_reason"] = "accessibility"
            accessibility_changes += 1
            print(f"  ~ access: {slug}: {prev_acc} -> {cur_acc}", file=sys.stderr)
        elif prev_sum and cur_sum and prev_sum != cur_sum:
            entry["prev_summary"] = prev_sum
            entry["changed_at"] = today_iso
            entry["change_reason"] = "summary"
            summary_changes += 1
            print(f"  ~ summary: {slug}", file=sys.stderr)
        elif slug in miss_slugs and prev_entry:
            # New trip-report URL came in (cache miss) but summary text happens to
            # match the prior one. Still worth surfacing as "WTA has news here".
            entry["changed_at"] = today_iso
            entry["change_reason"] = "new_report"
            new_report_changes += 1
            print(f"  ~ new report: {slug}", file=sys.stderr)

    status_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(
        f"[summarize] wrote {len(out)} entries "
        f"({hits} cached, {misses} OpenAI calls, {skipped_open} open-skipped, "
        f"{accessibility_changes} access changes, {summary_changes} summary changes, "
        f"{new_report_changes} new reports) -> {status_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only-non-open",
        action="store_true",
        help="Skip the OpenAI call for trails currently marked open (cheaper/faster).",
    )
    args = parser.parse_args()
    sys.exit(main(only_non_open=args.only_non_open))
