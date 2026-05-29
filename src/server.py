"""Local HTTP server: serves dist/ and exposes a small WTA-fetch API.

Routes:
  GET  /                 — static files from dist/
  GET  /api/search?q=…   — search WTA hike index, return up to 20 matching trails
  POST /api/add          — body {"slug": str, "url": str}: fetch the trail page,
                           scrape 3 latest reports, run OpenAI summarization, update
                           data/*.json and dist/index.html, append to extra_trails.json
  POST /api/subscribe    — body {"email": str}: append to data/subscribers.json (open)
  GET  /api/subscribers  — list registered emails (password-gated; admin page)

Designed as a drop-in replacement for `python -m http.server 8765 --directory dist/`.
"""
from __future__ import annotations

import hmac
import importlib
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from dataclasses import asdict
from pathlib import Path
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
_jobs: dict[str, dict] = {}     # async refresh jobs: {job_id: {status, started_at, ...}}
_jobs_lock = threading.Lock()
_JOB_KEEP_SECS = 30 * 60         # GC completed jobs after 30 minutes

load_dotenv(dotenv_path=DATA.parent / ".env")
# ~/dotEnv is the canonical source for OPENAI_API_KEY; loads after .env so it wins on overlap.
load_dotenv(dotenv_path=Path.home() / "dotEnv", override=True)


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

        from summarize import _cache_key, _enforce_snow_evidence, summarize_trail

        status_path = DATA / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        is_new_slug = slug not in status
        rs_dicts = reports[slug]
        sig = _cache_key(rs_dicts)
        try:
            st = summarize_trail(OpenAI(), asdict(trail), rs_dicts)
            _enforce_snow_evidence(st)
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
        if is_new_slug:
            from datetime import date as _date
            st["change_reason"] = "new"
            st["changed_at"] = _date.today().isoformat()
        status[slug] = st
        status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))

        extras_path = DATA / "extra_trails.json"
        extras = json.loads(extras_path.read_text()) if extras_path.exists() else []
        if not any(e.get("slug") == slug for e in extras):
            extras.append({"slug": slug, "url": url})
            extras_path.write_text(json.dumps(extras, indent=2, ensure_ascii=False))

        import render
        # Reload from disk so edits made after the server started take effect
        # (e.g., new keys added to the dashboard data blob).
        importlib.reload(render)
        render.main()

        return {
            "status": "added",
            "slug": slug,
            "name": trail.name,
            "region": trail.region,
            "accessibility": st.get("accessibility"),
        }


def refresh_status(only_non_open: bool = False) -> dict:
    """Rescrape trip reports for all known trails, resummarize what's new, re-render.

    Skips the regional top-N scrape (slow, slow-moving). Typical wall time ~90-120s.
    only_non_open: only re-summarize trails that aren't currently "open" (cheaper/faster).
    """
    with _data_lock:
        import scrape_reports
        import summarize
        import render

        # Reload from disk in case these modules were edited after the
        # long-running server first imported them.
        importlib.reload(scrape_reports)
        importlib.reload(summarize)
        importlib.reload(render)

        from datetime import date

        status_path = DATA / "status.json"
        before = json.loads(status_path.read_text()) if status_path.exists() else {}

        scrape_reports.main()
        summarize.main(only_non_open=only_non_open)
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


def _gc_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        for jid in list(_jobs.keys()):
            j = _jobs[jid]
            if j.get("finished_at") and now - j["finished_at"] > _JOB_KEEP_SECS:
                _jobs.pop(jid, None)


def start_refresh_job(only_non_open: bool = False) -> str:
    """Kick off refresh_status() on a background thread. Returns job_id."""
    _gc_jobs()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "started_at": time.time()}

    def run():
        try:
            res = refresh_status(only_non_open=only_non_open)
            with _jobs_lock:
                _jobs[job_id].update(status="completed", result=res, finished_at=time.time())
        except Exception as e:
            traceback.print_exc()
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(e), finished_at=time.time())

    threading.Thread(target=run, daemon=True, name=f"refresh-{job_id}").start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


SAFE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_subs_lock = threading.Lock()


def subscribe_email(email: str) -> dict:
    """Append an email to data/subscribers.json (deduped, case-insensitive)."""
    from datetime import datetime, timezone

    with _subs_lock:
        path = DATA / "subscribers.json"
        subs = json.loads(path.read_text()) if path.exists() else []
        key = email.lower()
        if any((s.get("email") or "").lower() == key for s in subs):
            return {"status": "exists", "email": email}
        subs.append({"email": email, "added": datetime.now(timezone.utc).isoformat()})
        path.write_text(json.dumps(subs, indent=2, ensure_ascii=False))
        return {"status": "added", "email": email, "count": len(subs)}


def list_subscribers() -> list[dict]:
    path = DATA / "subscribers.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,120}$", re.I)
SAFE_URL_RE = re.compile(r"^https://www\.wta\.org/go-hiking/hikes/[A-Za-z0-9.\-/]+$")


def _dashboard_password() -> str:
    """Read fresh on each call so .env edits don't require a restart."""
    return (os.getenv("DASHBOARD_PASSWORD") or "").strip()


def check_auth(headers) -> bool:
    """True if no password is configured, or the request supplied a matching one."""
    expected = _dashboard_password()
    if not expected:
        return True  # auth disabled when env var unset
    sent = headers.get("X-Dashboard-Password") or ""
    return hmac.compare_digest(sent, expected)


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
        if parsed.path == "/api/refresh/status":
            qs = urllib.parse.parse_qs(parsed.query)
            jid = (qs.get("id", [""])[0] or "").strip()
            j = get_job(jid) if jid else None
            if not j:
                return self._send_json(404, {"error": "unknown job"})
            return self._send_json(200, j)
        if parsed.path == "/api/subscribers":
            if not check_auth(self.headers):
                return self._send_json(401, {"error": "password required"})
            return self._send_json(200, {"subscribers": list_subscribers()})
        # Clean URL for the (unlinked) admin page: /admin and /admin/ -> admin.html
        if parsed.path in ("/admin", "/admin/"):
            self.path = "/admin.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/refresh":
            if not check_auth(self.headers):
                return self._send_json(401, {"error": "password required"})
            qs = urllib.parse.parse_qs(parsed.query)
            only_non_open = (qs.get("non_open", ["0"])[0] or "").lower() in ("1", "true", "yes")
            try:
                job_id = start_refresh_job(only_non_open=only_non_open)
                return self._send_json(202, {"job_id": job_id, "status": "running"})
            except Exception as e:
                traceback.print_exc()
                return self._send_json(500, {"error": str(e)})

        if parsed.path == "/api/subscribe":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "invalid json"})
            email = (body.get("email") or "").strip()
            if not SAFE_EMAIL_RE.match(email) or len(email) > 254:
                return self._send_json(400, {"error": "invalid email"})
            try:
                return self._send_json(200, subscribe_email(email))
            except Exception as e:
                traceback.print_exc()
                return self._send_json(500, {"error": str(e)})

        if parsed.path != "/api/add":
            return self._send_json(404, {"error": "not found"})

        # Adding a trail is open (no password) — slug/url validation below limits
        # this to real wta.org hike pages. Refresh stays password-gated above.
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
