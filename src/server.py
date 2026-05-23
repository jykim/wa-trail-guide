"""Local HTTP server: serves dist/ and exposes a small WTA-fetch API.

Routes:
  GET  /                 — static files from dist/
  GET  /api/search?q=…   — search WTA hike index, return up to 20 matching trails
  POST /api/add          — body {"slug": str, "url": str}: fetch the trail page,
                           scrape 3 latest reports, run OpenAI summarization, update
                           data/*.json and dist/index.html, append to extra_trails.json

Designed as a drop-in replacement for `python -m http.server 8765 --directory dist/`.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import traceback
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from common import DATA, DIST, fetch
from compute_drive import ORIGINS, _coord_key, _route
from scrape_reports import fetch_reports
from scrape_trails import VOTES_RE, _parse_trail_page

PORT = 8765
SEARCH_URL = "https://www.wta.org/go-outside/hikes"

_data_lock = threading.Lock()  # serialize state-mutating /api/add calls

load_dotenv(dotenv_path=DATA.parent / ".env")


def search_wta(query: str, limit: int = 20) -> list[dict]:
    """Hit WTA's hike index filtered by title text; return parsed result rows."""
    params = {"title": query, "b_size": str(limit)}
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    resp = fetch(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.find_all(class_="search-result-item")
    out: list[dict] = []
    for it in items[:limit]:
        title = it.find(class_="listitem-title")
        a = title.find("a", href=True) if title else None
        if not a:
            continue
        href = a["href"]
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        name = a.get_text(strip=True)

        region_div = it.find(class_="region")
        region_text = region_div.get_text(" ", strip=True) if region_div else ""

        rating_el = it.find(class_="current-rating")
        try:
            rating = float(rating_el.get_text(strip=True)) if rating_el else 0.0
        except ValueError:
            rating = 0.0

        votes = 0
        votes_el = it.find(class_="rating-count")
        if votes_el:
            m = VOTES_RE.search(votes_el.get_text())
            if m:
                votes = int(m.group(1))

        out.append({
            "slug": slug,
            "name": name,
            "url": href,
            "region": region_text,
            "rating": rating,
            "votes": votes,
        })
    return out


def add_trail(slug: str, url: str) -> dict:
    """Incrementally add a single WTA trail to the dataset and re-render the dashboard."""
    with _data_lock:
        trails_path = DATA / "trails.json"
        trails = json.loads(trails_path.read_text())
        if any(t["slug"] == slug for t in trails):
            return {"status": "exists", "slug": slug}

        trail = _parse_trail_page(slug, url)
        if trail is None:
            raise RuntimeError(f"failed to parse trail page for {slug}")

        trails.append(asdict(trail))
        trails_path.write_text(json.dumps(trails, indent=2, ensure_ascii=False))

        if trail.lat is not None and trail.lng is not None:
            cache_path = DATA / "drive_cache.json"
            cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
            key = _coord_key(trail.lat, trail.lng)
            entry = cache.get(key, {})
            for origin_name, origin in ORIGINS.items():
                if f"{origin_name}_min" in entry:
                    continue
                try:
                    result = _route(origin, (trail.lat, trail.lng))
                except Exception as e:
                    print(f"  ! drive {origin_name}: {e}", file=sys.stderr)
                    result = None
                if result:
                    mins, miles = result
                    entry[f"{origin_name}_min"] = mins
                    entry[f"{origin_name}_mi"] = miles
            cache[key] = entry
            cache_path.write_text(json.dumps(cache, indent=2))

        reports_path = DATA / "reports.json"
        reports = json.loads(reports_path.read_text()) if reports_path.exists() else {}
        try:
            rs = fetch_reports(url)
            reports[slug] = [asdict(r) for r in rs]
        except Exception as e:
            print(f"  ! reports: {e}", file=sys.stderr)
            reports[slug] = []
        reports_path.write_text(json.dumps(reports, indent=2, ensure_ascii=False))

        # Summarize — import lazily so the server still boots if OpenAI isn't installed/configured.
        from openai import OpenAI

        from summarize import _cache_key, summarize_trail

        status_path = DATA / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        rs_dicts = reports[slug]
        sig = _cache_key(rs_dicts)
        try:
            st = summarize_trail(OpenAI(), asdict(trail), rs_dicts)
            st["_cache_sig"] = sig
        except Exception as e:
            print(f"  ! summarize: {e}", file=sys.stderr)
            st = {
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
        status[slug] = st
        status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))

        extras_path = DATA / "extra_trails.json"
        extras = json.loads(extras_path.read_text()) if extras_path.exists() else []
        if not any(e.get("slug") == slug for e in extras):
            extras.append({"slug": slug, "url": url})
            extras_path.write_text(json.dumps(extras, indent=2, ensure_ascii=False))

        import render
        render.main()

        return {
            "status": "added",
            "slug": slug,
            "name": trail.name,
            "region": trail.region,
            "accessibility": st.get("accessibility"),
        }


def refresh_status() -> dict:
    """Rescrape trip reports for all known trails, resummarize what's new, re-render.

    Skips the regional top-N scrape (slow, slow-moving). Typical wall time ~90-120s.
    """
    with _data_lock:
        import scrape_reports
        import summarize
        import render

        from datetime import date

        status_path = DATA / "status.json"
        before = json.loads(status_path.read_text()) if status_path.exists() else {}

        scrape_reports.main()
        summarize.main()
        render.main()

        after = json.loads(status_path.read_text())
        today = date.today().isoformat()
        changed_today = [
            slug for slug, s in after.items() if s.get("changed_at") == today
        ]
        new_trails = [slug for slug in after if slug not in before]

        return {
            "status": "ok",
            "trails": len(after),
            "changed_today": changed_today,
            "new_trails": new_trails,
        }


def _slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,120}$", re.I)
SAFE_URL_RE = re.compile(r"^https://www\.wta\.org/go-hiking/hikes/[A-Za-z0-9.\-/]+$")


class Handler(SimpleHTTPRequestHandler):
    # Serve files from dist/.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q", [""])[0] or "").strip()
            if not q:
                return self._send_json(400, {"error": "missing q"})
            try:
                results = search_wta(q)
                return self._send_json(200, {"query": q, "results": results})
            except Exception as e:
                traceback.print_exc()
                return self._send_json(502, {"error": str(e)})
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/refresh":
            try:
                return self._send_json(200, refresh_status())
            except Exception as e:
                traceback.print_exc()
                return self._send_json(500, {"error": str(e)})

        if parsed.path != "/api/add":
            return self._send_json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "invalid json"})

        url = (body.get("url") or "").strip()
        slug = (body.get("slug") or _slug_from_url(url)).strip()

        if not SAFE_SLUG_RE.match(slug):
            return self._send_json(400, {"error": "invalid slug"})
        if not SAFE_URL_RE.match(url):
            return self._send_json(400, {"error": "invalid wta url"})

        try:
            result = add_trail(slug, url)
            return self._send_json(200, result)
        except Exception as e:
            traceback.print_exc()
            return self._send_json(500, {"error": str(e)})


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    addr = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(addr, Handler)
    print(f"[server] serving {DIST} on http://{addr[0]}:{addr[1]} (API at /api/search, /api/add)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
