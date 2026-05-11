"""Compute driving time/distance from Seattle + Bellevue to each trailhead via OSRM.

Public OSRM router has fair-use limits — we throttle to ~2 req/sec and cache
results by (lat, lng) so reruns hit cache, not network.

Output: data/drive_cache.json keyed by "{lat:.5f},{lng:.5f}" with
        {seattle_min, seattle_mi, bellevue_min, bellevue_mi}.
"""
from __future__ import annotations

import json
import sys
import time

import requests

from common import DATA

ORIGINS = {
    "seattle":  (47.6062, -122.3321),
    "bellevue": (47.6101, -122.2015),
}

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
THROTTLE_SEC = 0.5  # 2 req/sec — well within OSRM public server limits

_session = requests.Session()
_session.headers.update({
    "User-Agent": "wta-status/0.1 (personal weekly dashboard; routing via OSRM)",
    "Accept": "application/json",
})


def _coord_key(lat: float, lng: float) -> str:
    return f"{lat:.5f},{lng:.5f}"


def _route(origin: tuple[float, float], dest: tuple[float, float]) -> tuple[int, float] | None:
    olat, olng = origin
    dlat, dlng = dest
    url = f"{OSRM_BASE}/{olng},{olat};{dlng},{dlat}?overview=false"
    resp = _session.get(url, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    minutes = int(round(r["duration"] / 60))
    miles = round(r["distance"] / 1609.344, 1)
    return minutes, miles


def main() -> int:
    trails = json.loads((DATA / "trails.json").read_text())
    cache_path = DATA / "drive_cache.json"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache = {}

    fetched = 0
    skipped_cache = 0
    skipped_nocoord = 0
    for i, t in enumerate(trails, 1):
        lat, lng = t.get("lat"), t.get("lng")
        if lat is None or lng is None:
            skipped_nocoord += 1
            continue
        key = _coord_key(lat, lng)
        entry = cache.get(key, {})
        need = [o for o in ORIGINS if f"{o}_min" not in entry]
        if not need:
            skipped_cache += 1
            continue

        print(f"[drive] ({i}/{len(trails)}) {t['name']} ({', '.join(need)})", file=sys.stderr, flush=True)
        for origin_name in need:
            try:
                result = _route(ORIGINS[origin_name], (lat, lng))
            except Exception as e:
                print(f"  ! {origin_name}: {e}", file=sys.stderr)
                continue
            if result is None:
                print(f"  ? {origin_name}: no route", file=sys.stderr)
                continue
            mins, miles = result
            entry[f"{origin_name}_min"] = mins
            entry[f"{origin_name}_mi"] = miles
            time.sleep(THROTTLE_SEC)
        cache[key] = entry
        fetched += 1
        # Save incrementally so a crash mid-run doesn't lose progress
        cache_path.write_text(json.dumps(cache, indent=2))

    cache_path.write_text(json.dumps(cache, indent=2))
    print(
        f"[drive] {fetched} fetched, {skipped_cache} cached, "
        f"{skipped_nocoord} skipped (no coords) -> {cache_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
