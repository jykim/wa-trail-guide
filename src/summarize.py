"""Use OpenAI to summarize each trail's recent reports into a structured status.

Output: data/status.json keyed by slug.
"""
from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from common import DATA

load_dotenv(dotenv_path=DATA.parent / ".env")

MODEL = "gpt-4o-mini"
MAX_TOKENS = 600
PROMPT_VERSION = "v2"  # bump to invalidate the summary cache when the system prompt changes

SYSTEM_PROMPT = """You classify hiking-trail accessibility from recent WTA trip reports.

For each trail you receive: name, region, length, elevation gain/highpoint, and the last few trip reports (date, author, beware-of issues, feature flags, body text).

Return the structured fields via the function call.

**Recency is authoritative.** Report 1 is the most recent. When reports describe different conditions, the most recent report describes current conditions; older reports describe past conditions that may have changed. If Report 1 explicitly says a hazard has been repaired/cleared/melted out, trust that over older reports that mentioned the hazard. Only fall back to older reports for facts the latest report does not contradict.

When evidence is weak or absent, use the "unknown" sentinel for road/bugs/wildflowers and pick the best-supported accessibility class.

Accessibility rules:
- "open": trail is in normal hikeable condition for a fit hiker with no special gear (regular hiking boots, normal day-hike kit). Minor blowdowns, mud, or mosquitoes do not disqualify.
- "snow_gear": trail is hikeable but requires snow/ice traction (microspikes, crampons, snowshoes, ice axe), a creek-ford workaround, or significant route-finding above the snow line. Use this when reports mention snow on trail, post-holing, microspikes, crampons, snowshoes, slick snow, glacier travel, or impassable creek crossings without gear.
- "closed": the trail or its access is impassable for a typical hiker — road washed out and no walkable workaround, trailhead permit not yet open with no alternative, severe trail damage, active fire closure, etc.

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


def main() -> int:
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
    for i, t in enumerate(trails, 1):
        slug = t["slug"]
        rs = reports_by_slug.get(slug, [])
        sig = _cache_key(rs)
        cached = prev_status.get(slug)
        if cached and cached.get("_cache_sig") == sig:
            out[slug] = cached
            hits += 1
            continue

        misses += 1
        print(f"[summarize] ({i}/{len(trails)}) {t['name']} ({len(rs)} reports)", file=sys.stderr, flush=True)
        try:
            status = summarize_trail(client, t, rs)
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

    status_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(
        f"[summarize] wrote {len(out)} entries ({hits} cached, {misses} OpenAI calls) -> {status_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
