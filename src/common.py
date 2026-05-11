"""Shared HTTP client, paths, and region list."""
from __future__ import annotations

import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIST = ROOT / "dist"
TEMPLATES = ROOT / "templates"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "wta-status/0.1 (personal weekly dashboard)"
)

REGIONS = [
    ("b4845d8a21ad6a202944425c86b6e85f", "Central Cascades"),
    ("41f702968848492db697e10b14c14060", "Central Washington"),
    ("9d321b42e903a3224fd4fef44af9bee3", "Eastern Washington"),
    ("592fcc9afd9208db3b81fdf93dada567", "Issaquah Alps"),
    ("344281caae0d5e845a5003400c0be9ef", "Mount Rainier Area"),
    ("49aff77512c523f32ae13d889f6969c9", "North Cascades"),
    ("922e688d784aa95dfb80047d2d79dcf6", "Olympic Peninsula"),
    ("0c1d82b18f8023acb08e4daf03173e94", "Puget Sound and Islands"),
    ("04d37e830680c65b61df474e7e655d64", "Snoqualmie Region"),
    ("8a977ce4bf0528f4f833743e22acae5d", "South Cascades"),
    ("2b6f1470ed0a4735a4fc9c74e25096e0", "Southwest Washington"),
]

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

_last_request = 0.0
_MIN_INTERVAL = 1.0


def fetch(url: str, **kwargs) -> requests.Response:
    """GET with 1 req/sec throttle and real UA. Raises on non-2xx."""
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    resp = _session.get(url, timeout=30, **kwargs)
    _last_request = time.monotonic()
    resp.raise_for_status()
    return resp
