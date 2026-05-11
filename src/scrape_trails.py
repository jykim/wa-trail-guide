"""Scrape WTA hike search per region. Output: data/trails.json.

Pulls min_rating=4 results sorted by rating, keeps trails with >= MIN_VOTES votes
to filter out obscure single-vote trails. Takes PER_REGION_KEEP top trails per region.
Then fetches each trail page to extract geocoordinates from JSON-LD.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup

from common import DATA, REGIONS, fetch

SEARCH_URL = "https://www.wta.org/go-outside/hikes"
PER_REGION_FETCH = 50         # how many to pull from WTA per region
PER_REGION_KEEP = 5           # how many to keep per region after vote filter
MIN_VOTES = 20                # popularity floor
MIN_RATING = 4                # WTA's own filter

VOTES_RE = re.compile(r"\(\s*(\d+)\s+votes?\s*\)", re.I)
MILES_RE = re.compile(r"([\d.]+)\s*miles?", re.I)
ELEV_RE = re.compile(r"[\d,]+")


@dataclass
class Trail:
    slug: str
    name: str
    url: str
    region: str
    subregion: str
    rating: float
    votes: int
    length_miles: float | None
    elev_gain_ft: int | None
    highpoint_ft: int | None
    lat: float | None = None
    lng: float | None = None


def _parse_int(s: str | None) -> int | None:
    if not s:
        return None
    m = ELEV_RE.search(s)
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def _parse_stat(item, css_class: str) -> str | None:
    el = item.find(class_=css_class)
    if not el:
        return None
    dd = el.find("dd")
    return dd.get_text(" ", strip=True) if dd else None


def parse_region(region_uuid: str, region_name: str) -> list[Trail]:
    url = f"{SEARCH_URL}?region={region_uuid}&sort_on=rating&min_rating={MIN_RATING}&b_size={PER_REGION_FETCH}"
    resp = fetch(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.find_all(class_="search-result-item")
    if not items:
        raise RuntimeError(f"No results for region {region_name} — selector drift?")

    trails: list[Trail] = []
    for it in items:
        a = it.find(class_="listitem-title").find("a") if it.find(class_="listitem-title") else None
        if not a or not a.get("href"):
            continue
        href = a["href"]
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        name = a.get_text(strip=True)

        region_div = it.find(class_="region")
        region_text = region_div.get_text(" ", strip=True) if region_div else region_name
        if ">" in region_text:
            _, subregion = region_text.split(">", 1)
            subregion = subregion.strip()
        else:
            subregion = ""

        rating_el = it.find(class_="current-rating")
        rating = float(rating_el.get_text(strip=True)) if rating_el else 0.0

        votes_el = it.find(class_="rating-count")
        votes = 0
        if votes_el:
            m = VOTES_RE.search(votes_el.get_text())
            if m:
                votes = int(m.group(1))

        length_text = _parse_stat(it, "hike-length")
        length_miles = None
        if length_text:
            m = MILES_RE.search(length_text)
            if m:
                length_miles = float(m.group(1))

        elev_gain = _parse_int(_parse_stat(it, "hike-gain"))
        highpoint = _parse_int(_parse_stat(it, "hike-highpoint"))

        trails.append(Trail(
            slug=slug,
            name=name,
            url=href,
            region=region_name,
            subregion=subregion,
            rating=rating,
            votes=votes,
            length_miles=length_miles,
            elev_gain_ft=elev_gain,
            highpoint_ft=highpoint,
        ))

    popular = [t for t in trails if t.votes >= MIN_VOTES]
    popular.sort(key=lambda t: (t.rating, t.votes), reverse=True)
    return popular[:PER_REGION_KEEP]


_KNOWN_REGIONS = {name for _, name in REGIONS}


def _parse_trail_page(slug: str, url: str) -> Trail | None:
    """Build a Trail from the trail page itself (used for extras_trails.json)."""
    resp = fetch(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else slug.replace("-", " ").title()

    # Region: find a wta-icon-headline__text that starts with a known region name.
    region = ""
    subregion = ""
    for el in soup.find_all(class_="wta-icon-headline__text"):
        txt = el.get_text(" ", strip=True)
        for known in _KNOWN_REGIONS:
            if txt.startswith(known):
                region = known
                if ">" in txt:
                    subregion = txt.split(">", 1)[1].strip()
                break
        if region:
            break

    # JSON-LD: rating + votes + coords
    rating = 0.0
    votes = 0
    lat: float | None = None
    lng: float | None = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            blob = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(blob, dict):
            continue
        if blob.get("@type") == "LocalBusiness":
            ag = blob.get("aggregateRating") or {}
            try:
                rating = float(ag.get("ratingValue") or 0)
            except (TypeError, ValueError):
                pass
            try:
                votes = int(ag.get("ratingCount") or 0)
            except (TypeError, ValueError):
                pass
            geo = blob.get("geo") or {}
            try:
                lat = float(geo["latitude"]); lng = float(geo["longitude"])
            except (KeyError, TypeError, ValueError):
                pass
            break

    # Stats: length, elev gain, highpoint (from .hike-stats grid)
    length_miles: float | None = None
    elev_gain: int | None = None
    highpoint: int | None = None
    for el in soup.find_all(class_="hike-length"):
        m = MILES_RE.search(el.get_text(" ", strip=True))
        if m:
            length_miles = float(m.group(1))
        break
    for el in soup.find_all(class_="hike-gain"):
        elev_gain = _parse_int(el.get_text(" ", strip=True))
        break
    for el in soup.find_all(class_="hike-highpoint"):
        highpoint = _parse_int(el.get_text(" ", strip=True))
        break

    if not name:
        return None

    return Trail(
        slug=slug,
        name=name,
        url=url,
        region=region or "Other",
        subregion=subregion,
        rating=rating,
        votes=votes,
        length_miles=length_miles,
        elev_gain_ft=elev_gain,
        highpoint_ft=highpoint,
        lat=lat,
        lng=lng,
    )


def fetch_coords(trail_url: str) -> tuple[float | None, float | None]:
    """Extract (lat, lng) from a trail page's JSON-LD GeoCoordinates block."""
    resp = fetch(trail_url)
    soup = BeautifulSoup(resp.text, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            blob = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        geo = blob.get("geo") if isinstance(blob, dict) else None
        if isinstance(geo, dict) and "latitude" in geo and "longitude" in geo:
            try:
                return float(geo["latitude"]), float(geo["longitude"])
            except (TypeError, ValueError):
                continue
    return None, None


def load_extras() -> list[dict]:
    """Load curated extra trail slugs from data/extra_trails.json (if present)."""
    path = DATA / "extra_trails.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  ! extra_trails.json: {e}", file=sys.stderr)
        return []


def _load_existing_coords() -> dict[str, tuple[float, float]]:
    """Read previous trails.json (if any) and return {slug: (lat, lng)} for already-known trails."""
    path = DATA / "trails.json"
    if not path.exists():
        return {}
    try:
        prev = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for t in prev:
        if t.get("lat") is not None and t.get("lng") is not None:
            out[t["slug"]] = (t["lat"], t["lng"])
    return out


def main() -> int:
    existing_coords = _load_existing_coords()
    all_trails: list[Trail] = []
    seen: set[str] = set()
    for uuid, name in REGIONS:
        print(f"[trails] {name}...", file=sys.stderr, flush=True)
        try:
            picks = parse_region(uuid, name)
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
            continue
        for t in picks:
            print(f"  - {t.name} ({t.rating}★, {t.votes} votes)", file=sys.stderr)
            seen.add(t.slug)
        all_trails.extend(picks)

    cached = sum(1 for t in all_trails if t.slug in existing_coords)
    need = len(all_trails) - cached
    print(f"[trails] coords: {cached} cached, fetching {need}", file=sys.stderr)
    for t in all_trails:
        if t.slug in existing_coords:
            t.lat, t.lng = existing_coords[t.slug]
            continue
        try:
            t.lat, t.lng = fetch_coords(t.url)
        except Exception as e:
            print(f"  ! coords failed for {t.slug}: {e}", file=sys.stderr)
        if t.lat is None:
            print(f"  ? no coords for {t.slug}", file=sys.stderr)

    extras = load_extras()
    if extras:
        # Reuse already-known trail metadata from previous trails.json when present —
        # title/region/rating change slowly, no need to re-fetch every week.
        prev_by_slug: dict[str, dict] = {}
        prev_path = DATA / "trails.json"
        if prev_path.exists():
            try:
                for p in json.loads(prev_path.read_text()):
                    prev_by_slug[p["slug"]] = p
            except json.JSONDecodeError:
                pass

        new_extras = [e for e in extras if e["slug"] not in seen and e["slug"] not in prev_by_slug]
        cached_extras = [e for e in extras if e["slug"] in prev_by_slug and e["slug"] not in seen]

        print(
            f"[trails] extras: {len(cached_extras)} cached, fetching {len(new_extras)}",
            file=sys.stderr,
        )

        # Cached extras — rehydrate from previous trails.json.
        for entry in cached_extras:
            slug = entry["slug"]
            p = prev_by_slug[slug]
            all_trails.append(Trail(
                slug=p["slug"], name=p["name"], url=p["url"],
                region=p.get("region", "Other"), subregion=p.get("subregion", ""),
                rating=p.get("rating", 0.0), votes=p.get("votes", 0),
                length_miles=p.get("length_miles"), elev_gain_ft=p.get("elev_gain_ft"),
                highpoint_ft=p.get("highpoint_ft"),
                lat=p.get("lat"), lng=p.get("lng"),
            ))
            seen.add(slug)

        # New extras — fetch trail page.
        for entry in new_extras:
            slug = entry["slug"]
            url = entry.get("url") or f"https://www.wta.org/go-hiking/hikes/{slug}"
            try:
                t = _parse_trail_page(slug, url)
            except Exception as e:
                print(f"  ! failed {slug}: {e}", file=sys.stderr)
                continue
            if t is None:
                print(f"  ! no data {slug}", file=sys.stderr)
                continue
            print(f"  + {t.name} ({t.rating}★, {t.votes} votes, {t.region or '?'})", file=sys.stderr)
            all_trails.append(t)
            seen.add(slug)

    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / "trails.json"
    out.write_text(json.dumps([asdict(t) for t in all_trails], indent=2, ensure_ascii=False))
    print(f"[trails] wrote {len(all_trails)} trails -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
