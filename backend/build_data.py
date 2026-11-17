#!/usr/bin/env python3
"""
Denver Group Rides - data builder
=================================
Builds the four JSON files the frontend reads (same formats as boulderrides.cc):

    data/club_rides.json        upcoming group rides with routes
    data/races.json             upcoming races & events
    data/weather.json           daily forecast for downtown Denver
    data/detailed_weather.json  forecast along the route for rides with a known pace

Sources
    Ride with GPS   public club/organization event pages (no credentials needed)
    Meetup          public group calendars (iCal) + the event page for the meeting point
    Event calendars WordPress "The Events Calendar" sites (e.g. Bicycle Colorado)
    Strava          club group events (needs STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET /
                    STRAVA_REFRESH_TOKEN in the environment; skipped when absent)
    Boulder feed    the public boulderrides.cc data branch, merged in as-is
    BikeReg         public event search API
    NWS             api.weather.gov forecasts
    OpenStreetMap   Overpass API, paved/unpaved tagging for routes without surface data

Python 3.9+, standard library only.

    python3 backend/build_data.py                 # build everything into ./data
    python3 backend/build_data.py --only rides    # rides | races | weather | detailed
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TZ = ZoneInfo("America/Denver")
USER_AGENT = os.environ.get(
    "RIDES_USER_AGENT", "denver-group-rides/1.0 (community ride map; github.com/100-debug/Denver-CC)"
)
MI_PER_M = 1 / 1609.344
FT_PER_M = 3.28084
MAX_ROUTE_POINTS = 2000
REQUEST_PAUSE_S = 0.35  # be polite to the upstream services

_log_lines = []


def log(msg):
    print(msg, flush=True)
    _log_lines.append(msg)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, url, status, body=""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.body = body


def http_get(url, headers=None, data=None, timeout=45, retries=3, raw=False):
    """GET (or POST when data is given). Returns parsed JSON, or bytes when raw=True."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = res.read()
            time.sleep(REQUEST_PAUSE_S)
            return body if raw else json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx (other than 429) will not get better with a retry
            if 400 <= e.code < 500 and e.code != 429:
                raise HttpError(url, e.code, e.read()[:300].decode("utf-8", "replace"))
            last = HttpError(url, e.code)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise last if isinstance(last, Exception) else RuntimeError(f"request failed: {url}")


# --------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def miles_between(a, b):
    return haversine_m(a[0], a[1], b[0], b[1]) * MI_PER_M


def cumulative_m(points):
    """Cumulative distance (m) along [[lat, lng, ...], ...]."""
    out, total = [0.0], 0.0
    for i in range(1, len(points)):
        total += haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        out.append(total)
    return out


def thin_route(points, limit=MAX_ROUTE_POINTS):
    """Evenly thins a long route, always keeping the ends and every surface change."""
    n = len(points)
    if n <= limit:
        return points
    step = math.ceil(n / limit)
    keep = []
    for i, p in enumerate(points):
        change = i > 0 and p[3] != points[i - 1][3]
        if i % step == 0 or i == n - 1 or change:
            keep.append(p)
    return keep


def decode_polyline(s, precision=5):
    """Google encoded polyline -> [[lat, lng], ...]."""
    coords, idx, lat, lng, factor = [], 0, 0, 0, 10 ** precision
    while idx < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        coords.append([lat / factor, lng / factor])
    return coords


# --------------------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------------------
def now_local():
    return dt.datetime.now(TZ)


def date_window(days):
    """(first_date, last_date) inclusive, local time: today .. today + days - 1."""
    today = now_local().date()
    return today, today + dt.timedelta(days=days - 1)


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_ride_date(d):
    return d.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------------------
# Paved / unpaved
# --------------------------------------------------------------------------------------
# Ride with GPS tags every track point with a surface id "S". The ids are not publicly
# documented; the ranges below are inferred and then VERIFIED per route against the
# route's own published `unpaved_pct` (see rwgps_surfaces). If they disagree, the route
# is left untagged (null) and handed to the OpenStreetMap classifier instead.
def _rwgps_surface_tag(s):
    if s is None or s in (0, 95, 99):
        return None  # unknown
    if 1 <= s <= 49:
        return 1  # paved family
    if 50 <= s <= 94:
        return 0  # unpaved family
    return None


def rwgps_surfaces(track_points, unpaved_pct):
    """Returns a per-point list of 1 (paved) / 0 (unpaved), or None if not trustworthy."""
    tags = [_rwgps_surface_tag(p.get("S")) for p in track_points]
    known = [t for t in tags if t is not None]
    if not known or len(known) < 0.5 * len(tags):
        return None
    # fill unknowns from the previous known point (then the next, for a leading gap)
    last = None
    for i, t in enumerate(tags):
        if t is None:
            tags[i] = last
        else:
            last = t
    nxt = None
    for i in range(len(tags) - 1, -1, -1):
        if tags[i] is None:
            tags[i] = nxt if nxt is not None else 1
        else:
            nxt = tags[i]
    if unpaved_pct is not None:
        # distance-weighted share of unpaved points vs. what Ride with GPS reports
        d = [p.get("d") or 0.0 for p in track_points]
        total = (d[-1] - d[0]) or 1.0
        unpaved = sum((d[i] - d[i - 1]) for i in range(1, len(d)) if tags[i] == 0)
        if abs(100.0 * unpaved / total - float(unpaved_pct)) > 15:
            return None
    return tags


UNPAVED_OSM = (
    "unpaved|gravel|fine_gravel|compacted|dirt|earth|ground|grass|sand|mud|pebblestone|"
    "rock|woodchips|soil"
)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def osm_surfaces(latlngs, snap_m=14.0):
    """
    Tags each [lat, lng] as 1 (paved) or 0 (unpaved) from OpenStreetMap: a point is
    unpaved when it lies within snap_m of a way whose surface (or tracktype) is unpaved.
    Returns None when the Overpass API cannot be reached.
    """
    if not latlngs:
        return None
    lats = [p[0] for p in latlngs]
    lngs = [p[1] for p in latlngs]
    pad = 0.002
    bbox = f"{min(lats) - pad},{min(lngs) - pad},{max(lats) + pad},{max(lngs) + pad}"
    query = (
        f"[out:json][timeout:90];("
        f'way["highway"]["surface"~"^({UNPAVED_OSM})$"]({bbox});'
        f'way["highway"="track"][!"surface"]({bbox});'
        f");out geom;"
    )
    data = None
    for base in OVERPASS_URLS:
        try:
            data = http_get(
                base, data=urllib.parse.urlencode({"data": query}).encode(), timeout=120, retries=2
            )
            break
        except Exception as e:  # noqa: BLE001 - any failure -> try the next mirror
            log(f"    overpass {base.split('/')[2]} failed: {e}")
    if data is None:
        return None

    lat0 = (min(lats) + max(lats)) / 2
    m_lat, m_lng = 111320.0, 111320.0 * math.cos(math.radians(lat0))
    cell = snap_m

    def key(lat, lng):
        return (int(lat * m_lat // cell), int(lng * m_lng // cell))

    # spatial hash of unpaved ways, densified so consecutive samples are < cell apart
    grid = {}
    for el in data.get("elements", []):
        geom = el.get("geometry") or []
        for a, b in zip(geom, geom[1:]):
            seg = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            steps = max(1, int(seg // (cell / 2)))
            for k in range(steps + 1):
                t = k / steps
                la, lo = a["lat"] + (b["lat"] - a["lat"]) * t, a["lon"] + (b["lon"] - a["lon"]) * t
                grid.setdefault(key(la, lo), []).append((la, lo))

    out = []
    for la, lo in latlngs:
        kx, ky = key(la, lo)
        hit = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for qa, qo in grid.get((kx + dx, ky + dy), ()):
                    if haversine_m(la, lo, qa, qo) <= snap_m:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        out.append(0 if hit else 1)
    return out


# --------------------------------------------------------------------------------------
# Ride assembly shared by all sources
# --------------------------------------------------------------------------------------
PACE_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:-|–|to)\s*(\d{1,2}(?:\.\d)?)\s*mph|(\d{1,2}(?:\.\d)?)\s*mph", re.I)


def pace_from_text(text):
    """Average speed (mph) when the organizer states exactly one pace, else None."""
    found = set()
    for lo, hi, single in PACE_RE.findall(text or ""):
        v = (float(lo) + float(hi)) / 2 if lo else float(single)
        if 6 <= v <= 30:
            found.add(round(v, 1))
    return found.pop() if len(found) == 1 else None


COLORADO_BBOX = (36.99, -109.06, 41.01, -102.04)  # south, west, north, east


def region_for(start, cfg):
    """
    Area a ride belongs to, decided by where it STARTS (not by where its club is based):
    the nearest city region within that region's radius, otherwise "mountains" for
    anything else in Colorado west of the Front Range foothills. None = outside coverage.
    """
    lat, lng = start[0], start[1]
    s, w, n, e = COLORADO_BBOX
    if not (s <= lat <= n and w <= lng <= e):
        return None
    best, best_mi = None, None
    for key, reg in cfg["regions"].items():
        if not reg.get("center"):
            continue
        mi = miles_between(reg["center"], start)
        if mi <= reg.get("radius_mi", 30) and (best_mi is None or mi < best_mi):
            best, best_mi = key, mi
    if best and lng > cfg.get("mountain_line_lng", -105.6):
        return best
    if lng <= cfg.get("mountain_edge_lng", -105.2):
        return "mountains"
    return best


def in_range(club, start, cfg):
    """A ride is kept when it starts inside the coverage area."""
    return region_for(start, cfg) is not None


PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RWGPS_ROUTE_LINK = re.compile(r"ridewithgps\.com/routes/(\d+)")


def clean_text(text, limit=600):
    """Plain text from an event description: no HTML/markdown links, no phone numbers or emails."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = re.sub(r"\[([^\]]*)\]\((https?://[^)]+)\)", r"\1", t)      # [label](url) -> label
    t = re.sub(r"https?://\S+", "", t)
    t = PHONE_RE.sub("[phone on event page]", t)
    t = EMAIL_RE.sub("[email on event page]", t)
    t = re.sub(r"[*_#>\\]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def ride_type_for(title, desc, unpaved_pct):
    """
    road / gravel / mtb, only on firm evidence: the event TITLE says so, or the route's
    measured surface does. Descriptions are too loose ("road conditions", "some gravel").
    """
    t = (title or "").lower()
    if re.search(r"\bmtb\b|mountain bik|singletrack|single track|fat ?bike|enduro", t):
        return "mtb"
    if "gravel" in t or (unpaved_pct is not None and unpaved_pct >= 20):
        return "gravel"
    if re.search(r"\broad\b", t) or (unpaved_pct is not None and unpaved_pct < 5):
        return "road"
    return None


def route_metrics(route):
    """(distance_m, unpaved_pct) measured from the route geometry itself."""
    if not route or len(route) < 2:
        return None, None
    cum = cumulative_m(route)
    total = cum[-1]
    tagged = [p[3] for p in route if len(p) > 3 and p[3] is not None]
    pct = None
    if total > 0 and len(tagged) >= 0.8 * len(route):
        unpaved = sum(cum[i] - cum[i - 1] for i in range(1, len(route)) if route[i][3] == 0)
        pct = int(round(100 * unpaved / total))
    return total, pct


def ride_id(url, route_id, date):
    return hashlib.sha1(f"{url}|{route_id}|{date}".encode()).hexdigest()[:10]


def make_ride(source, club, title, start_dt, url, start, route_id, dist_m, gain_m, route, speed,
              desc="", place=None, address=None):
    return {
        "id": ride_id(url, route_id, fmt_ride_date(start_dt)),
        "source": source,
        "club_id": club.get("id") if isinstance(club.get("id"), int) else None,
        "club_name": club["name"],
        "region": club.get("region"),  # replaced by the geographic region in finish_ride()
        "description": clean_text(desc),
        "start_name": (place or "").strip() or None,
        "start_address": (address or "").strip() or None,
        "title": title.strip(),
        "date": fmt_ride_date(start_dt),
        "url": url,
        "starting_location": [round(start[0], 5), round(start[1], 5)],
        "women_only": bool(club.get("women_only", False)),
        "route_id": route_id,
        "dist_mi": round(dist_m * MI_PER_M, 1) if dist_m is not None else None,
        "elev_gain_ft": int(round(gain_m * FT_PER_M)) if gain_m is not None else None,
        "route": route,
        "_speed_mph": speed,  # internal: stripped before writing club_rides.json
    }


# --------------------------------------------------------------------------------------
# Ride with GPS (public JSON, no credentials)
# --------------------------------------------------------------------------------------
RWGPS = "https://ridewithgps.com"


def rwgps_route(route_id, cache):
    if route_id in cache:
        return cache[route_id]
    try:
        r = http_get(f"{RWGPS}/routes/{route_id}.json")
    except HttpError as e:
        log(f"    route {route_id}: {e} (private or removed) - skipped")
        cache[route_id] = None
        return None
    r = r.get("route", r)
    tps = [p for p in r.get("track_points", []) if "x" in p and "y" in p]
    if len(tps) < 2:
        cache[route_id] = None
        return None
    surf = rwgps_surfaces(tps, r.get("unpaved_pct"))
    pts = [
        [round(p["y"], 5), round(p["x"], 5), int(round(p["e"])) if p.get("e") is not None else None,
         surf[i] if surf else None]
        for i, p in enumerate(tps)
    ]
    out = {
        "name": r.get("name") or "",
        "distance": r.get("distance"),
        "gain": r.get("elevation_gain"),
        "points": pts,
        "needs_osm": surf is None,
    }
    cache[route_id] = out
    return out


def fetch_rwgps_club(club, cfg, first, last, route_cache):
    rides = []
    param = "club_id" if club.get("rwgps_kind") == "club" else "organization_id"
    listing = http_get(f"{RWGPS}/events.json?{param}={club['id']}")
    items = []
    for g in listing.get("groups", []):
        if g.get("name") in ("Current", "Upcoming"):
            items += g.get("items", [])
    for it in items:
        if not it.get("starts_at"):
            continue
        start_dt = parse_iso(it["starts_at"]).astimezone(TZ)
        if not (first <= start_dt.date() <= last):
            continue
        ev_id = int(str(it["slug"]).split("-")[0])
        ev = http_get(f"{RWGPS}/events/{ev_id}.json").get("event", {})
        routes = ev.get("routes") or []
        if not routes:
            log(f"    {it['name']} ({start_dt:%a %m/%d}): no route attached - skipped")
            continue
        speed = pace_from_text(ev.get("desc")) or club.get("avg_speed_mph")
        for rt in routes:
            r = rwgps_route(rt["id"], route_cache)
            if not r:
                continue
            start = r["points"][0][:2]
            if ev.get("computed_lat") is not None and ev.get("computed_lng") is not None:
                start = [ev["computed_lat"], ev["computed_lng"]]
            if not in_range(club, start, cfg):
                log(f"    {it['name']}: starts outside the coverage area - skipped")
                continue
            title = it["name"] if len(routes) == 1 else f"{it['name']} | {r['name']}"
            rides.append(make_ride(
                "Ride with GPS", club, title, start_dt, f"{RWGPS}/events/{ev_id}", start,
                rt["id"], r["distance"], r["gain"], r["points"], speed,
                desc=ev.get("desc"), address=ev.get("location"),
            ))
    return rides


# --------------------------------------------------------------------------------------
# Strava (needs API credentials)
# --------------------------------------------------------------------------------------
STRAVA_API = "https://www.strava.com/api/v3"
CYCLING_TYPES = {"Ride", "GravelRide", "MountainBikeRide", "EBikeRide", "EMountainBikeRide", "VirtualRide", "Handcycle", "Velomobile"}


def strava_token():
    cid, sec, ref = (os.environ.get(k) for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"))
    if not (cid and sec and ref):
        return None
    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": sec, "grant_type": "refresh_token", "refresh_token": ref,
    }).encode()
    tok = http_get("https://www.strava.com/oauth/token", data=body)
    return tok["access_token"]


def strava_route(route_id, auth, cache):
    if route_id in cache:
        return cache[route_id]
    try:
        meta = http_get(f"{STRAVA_API}/routes/{route_id}", headers=auth)
    except HttpError as e:
        log(f"    strava route {route_id}: {e} - skipped")
        cache[route_id] = None
        return None
    pts = []
    try:  # the GPX export carries elevation; the polyline does not
        gpx = http_get(f"{STRAVA_API}/routes/{route_id}/export_gpx", headers=auth, raw=True)
        for el in ET.fromstring(gpx).iter():
            if el.tag.endswith("trkpt") or el.tag.endswith("rtept"):
                ele = next((c.text for c in el if c.tag.endswith("ele")), None)
                pts.append([round(float(el.get("lat")), 5), round(float(el.get("lon")), 5),
                            int(round(float(ele))) if ele else None, None])
    except Exception as e:  # noqa: BLE001
        log(f"    strava route {route_id}: GPX export failed ({e}); using polyline without elevation")
    if len(pts) < 2:
        line = (meta.get("map") or {}).get("polyline") or (meta.get("map") or {}).get("summary_polyline")
        pts = [[round(a, 5), round(b, 5), None, None] for a, b in decode_polyline(line)] if line else []
    if len(pts) < 2:
        cache[route_id] = None
        return None
    out = {"name": meta.get("name") or "", "distance": meta.get("distance"),
           "gain": meta.get("elevation_gain"), "points": pts, "needs_osm": True}
    cache[route_id] = out
    return out


def fetch_strava_club(club, cfg, first, last, auth, route_cache):
    rides = []
    events = http_get(f"{STRAVA_API}/clubs/{club['id']}/group_events?upcoming=true&per_page=200", headers=auth)
    for ev in events:
        if ev.get("activity_type") and ev["activity_type"] not in CYCLING_TYPES:
            continue
        route_id = (ev.get("route") or {}).get("id_str") or ev.get("route_id")
        if not route_id:
            continue  # same rule as the Boulder site: no route, no listing
        for occ in ev.get("upcoming_occurrences") or []:
            start_dt = parse_iso(occ).astimezone(TZ)
            if not (first <= start_dt.date() <= last):
                continue
            r = strava_route(str(route_id), auth, route_cache)
            if not r:
                continue
            start = ev.get("start_latlng") or r["points"][0][:2]
            if not in_range(club, start, cfg):
                continue
            ride = make_ride(
                "Strava", club, ev.get("title") or "Group ride", start_dt,
                f"https://www.strava.com/clubs/{club['id']}/group_events/{ev.get('id_str') or ev['id']}",
                start, int(route_id), r["distance"], r["gain"], r["points"],
                pace_from_text(ev.get("description")) or club.get("avg_speed_mph"),
                desc=ev.get("description"), address=ev.get("address"),
            )
            ride["women_only"] = bool(ev.get("women_only")) or ride["women_only"]
            rides.append(ride)
    return rides


# --------------------------------------------------------------------------------------
# Shared by the calendar-style sources (Meetup, event calendars): these events carry a
# meeting point but usually no route. A Ride with GPS route link in the description is
# followed; otherwise the ride is listed as a start pin without a route.
# --------------------------------------------------------------------------------------
NOT_A_RIDE = re.compile(
    r"\b(meeting|happy hour|banquet|party|potluck|picnic|dinner|luncheon|volunteer|trail ?work|"
    r"clinic|class|workshop|webinar|fundraiser|gala|swap|movie|hike|run|ski|snowshoe)\b", re.I)
IS_A_RIDE = re.compile(r"\b(ride|rides|riding|mtb|gravel|cycling|bike|spin|climb|loop|century|paceline)\b", re.I)


def looks_like_ride(title, desc):
    if IS_A_RIDE.search(title or ""):
        return True
    if NOT_A_RIDE.search(title or ""):
        return False
    return bool(IS_A_RIDE.search(desc or ""))


def calendar_ride(source, club, title, start_dt, url, start, desc, place, address, route_cache):
    """Builds a ride from a calendar event; attaches a route when the description links one."""
    m = RWGPS_ROUTE_LINK.search(desc or "")
    r = rwgps_route(int(m.group(1)), route_cache) if m else None
    speed = pace_from_text(desc) or club.get("avg_speed_mph")
    if r:
        # keep the organizer's meeting point; the linked route only supplies the line
        return make_ride(source, club, title, start_dt, url, start, int(m.group(1)),
                         r["distance"], r["gain"], r["points"], speed, desc, place, address)
    return make_ride(source, club, title, start_dt, url, start, None, None, None, [], speed,
                     desc, place, address)


# ---- Meetup -----------------------------------------------------------------------------
def parse_ical(text):
    """Minimal iCalendar reader -> list of {SUMMARY, DTSTART, DESCRIPTION, URL, STATUS, LOCATION, GEO}."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        ev = {}
        for line in block.split("END:VEVENT")[0].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name = key.split(";")[0].upper()
            value = (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
                     .replace("\\;", ";").replace("\\\\", "\\"))
            ev[name] = value
            if name == "DTSTART":
                tz = re.search(r"TZID=([^;:]+)", key)
                ev["_TZID"] = tz.group(1) if tz else None
                ev["_DATE_ONLY"] = "VALUE=DATE" in key.upper() and "T" not in value
        events.append(ev)
    return events


def ical_start(ev):
    """Timezone-aware start, or None for all-day / unparseable entries."""
    raw = ev.get("DTSTART", "")
    if ev.get("_DATE_ONLY") or "T" not in raw:
        return None
    try:
        if raw.endswith("Z"):
            return dt.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).astimezone(TZ)
        naive = dt.datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
        return naive.replace(tzinfo=ZoneInfo(ev.get("_TZID") or "America/Denver")).astimezone(TZ)
    except (ValueError, KeyError):
        return None


def _find_venue(node):
    """First dict in Meetup's page data that looks like a venue (has lat + lng + a name)."""
    if isinstance(node, dict):
        lat, lng = node.get("lat"), node.get("lng", node.get("lon"))
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and (lat or lng) and node.get("name"):
            return node
        for v in node.values():
            hit = _find_venue(v)
            if hit:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _find_venue(v)
            if hit:
                return hit
    return None


def meetup_venue(url):
    page = http_get(url, headers={"Accept": "text/html"}, raw=True).decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    return _find_venue(json.loads(m.group(1)))


def fetch_meetup_group(club, cfg, first, last, route_cache):
    rides = []
    text = http_get(f"https://www.meetup.com/{club['id']}/events/ical/",
                    headers={"Accept": "text/calendar"}, raw=True).decode("utf-8", "replace")
    seen = set()
    for ev in parse_ical(text):
        start_dt = ical_start(ev)
        url = ev.get("URL")
        if not start_dt or not url or not (first <= start_dt.date() <= last):
            continue
        if (ev.get("STATUS") or "").upper() == "CANCELLED" or url in seen:
            continue
        seen.add(url)
        title = (ev.get("SUMMARY") or "").strip()
        desc = ev.get("DESCRIPTION") or ""
        if desc.startswith(club["name"]):          # Meetup prefixes the group name
            desc = desc[len(club["name"]):]
        if not title or re.search(r"\bcancel", title, re.I) or not looks_like_ride(title, desc):
            continue
        venue = meetup_venue(url)
        if not venue:
            log(f"    {title} ({start_dt:%a %m/%d}): no meeting point published - skipped")
            continue
        start = [float(venue["lat"]), float(venue.get("lng", venue.get("lon")))]
        if not in_range(club, start, cfg):
            continue
        address = ", ".join(x for x in (venue.get("address"), venue.get("city"), venue.get("state")) if x)
        rides.append(calendar_ride("Meetup", club, title, start_dt, url, start, desc,
                                   venue.get("name"), address, route_cache))
    return rides


# ---- WordPress "The Events Calendar" sites (Bicycle Colorado, ...) ---------------------
_geocode_cache = {}


def geocode(query):
    """Address -> [lat, lng] via OpenStreetMap Nominatim (1 request/second, cached). None if not found."""
    key = re.sub(r"\s+", " ", query.strip().lower())
    if key in _geocode_cache:
        return _geocode_cache[key]
    hit = None
    try:
        time.sleep(1.0)
        res = http_get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"format": "jsonv2", "limit": 1, "countrycodes": "us", "q": query}))
        # only accept a specific place or address, never a whole town or county
        if res and res[0].get("addresstype") not in ("city", "town", "village", "county", "state", "municipality"):
            hit = [float(res[0]["lat"]), float(res[0]["lon"])]
    except Exception as e:  # noqa: BLE001
        log(f"    geocoder failed for {query!r}: {e}")
        return None
    _geocode_cache[key] = hit
    return hit


def fetch_tribe_calendar(club, cfg, first, last, route_cache):
    rides, page = [], 1
    cats = [c.lower() for c in club.get("categories", [])]
    while page <= 5:
        q = urllib.parse.urlencode({"per_page": 50, "page": page, "start_date": first.isoformat(),
                                    "end_date": (last + dt.timedelta(days=1)).isoformat()})
        data = http_get(f"{club['id'].rstrip('/')}/wp-json/tribe/events/v1/events?{q}")
        for ev in data.get("events", []):
            names = [c.get("name", "").lower() for c in ev.get("categories") or []]
            if cats and not any(c in names for c in cats):
                continue
            if ev.get("all_day"):
                continue
            title = html.unescape(ev.get("title") or "").strip()
            desc = ev.get("description") or ""
            try:
                start_dt = dt.datetime.strptime(ev["start_date"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=ZoneInfo(ev.get("timezone") or "America/Denver")).astimezone(TZ)
            except (KeyError, ValueError):
                continue
            if not (first <= start_dt.date() <= last) or not looks_like_ride(title, clean_text(desc)):
                continue
            venue = ev.get("venue") if isinstance(ev.get("venue"), dict) else {}
            place = html.unescape(venue.get("venue") or "")
            address = ", ".join(x for x in (venue.get("address"), venue.get("city"),
                                            venue.get("stateprovince") or venue.get("state")) if x)
            start = None
            if venue.get("geo_lat") and venue.get("geo_lng"):
                start = [float(venue["geo_lat"]), float(venue["geo_lng"])]
            elif venue.get("address") and venue.get("city"):
                start = geocode(f"{address}, Colorado")
            if not start and place and venue.get("city"):
                start = geocode(f"{place}, {venue['city']}, Colorado")
            if not start:
                log(f"    {title} ({start_dt:%a %m/%d}): meeting point could not be located - skipped")
                continue
            if not in_range(club, start, cfg):
                continue
            organizer = (ev.get("organizer") or [{}])[0].get("organizer")
            host = dict(club, name=html.unescape(organizer) if organizer else club["name"])
            rides.append(calendar_ride(club.get("label", "Event calendar"), host, title, start_dt,
                                       ev.get("url"), start, desc, place, address, route_cache))
        if page >= (data.get("total_pages") or 1):
            break
        page += 1
    return rides


# ---- Any public iCal feed that carries GEO coordinates or a street address --------------
def fetch_ical_feed(club, cfg, first, last, route_cache):
    rides = []
    text = http_get(club["id"], headers={"Accept": "text/calendar"}, raw=True).decode("utf-8", "replace")
    for ev in parse_ical(text):
        start_dt = ical_start(ev)
        title = (ev.get("SUMMARY") or "").strip()
        desc = ev.get("DESCRIPTION") or ""
        if not start_dt or not title or not (first <= start_dt.date() <= last):
            continue
        if (ev.get("STATUS") or "").upper() == "CANCELLED" or not looks_like_ride(title, desc):
            continue
        start = None
        if ev.get("GEO") and ";" in ev["GEO"]:
            try:
                start = [float(x) for x in ev["GEO"].split(";")[:2]]
            except ValueError:
                start = None
        loc = (ev.get("LOCATION") or "").strip()
        if not start and re.search(r"\d", loc):   # looks like a street address
            start = geocode(loc if "CO" in loc or "Colorado" in loc else f"{loc}, Colorado")
        if not start or not in_range(club, start, cfg):
            continue
        url = ev.get("URL") or club.get("url") or club["id"]
        rides.append(calendar_ride(club.get("label", "Club calendar"), club, title, start_dt, url,
                                   start, desc, None, loc, route_cache))
    return rides


# --------------------------------------------------------------------------------------
# Boulder feed (public data branch of github.com/RBergua/boulderrides, MIT)
# --------------------------------------------------------------------------------------
def fetch_boulder_feed(cfg, first, last):
    base = cfg.get("boulder_feed")
    if not base:
        return [], {}
    rides = http_get(base + "club_rides.json", timeout=90)
    keep = []
    for r in rides:
        try:
            d = dt.date.fromisoformat(r["date"].split(" ")[0])
        except (KeyError, ValueError):
            continue
        if first <= d <= last and r.get("route"):
            r["region"] = "boulder"
            r["_feed"] = True
            keep.append(r)
    detailed = {}
    try:
        for r in http_get(base + "detailed_weather.json").get("rides", []):
            if r.get("status") == "available":
                detailed[(r["ride_title"], r["date"])] = r
    except Exception as e:  # noqa: BLE001
        log(f"  boulder detailed weather unavailable: {e}")
    return keep, detailed


# --------------------------------------------------------------------------------------
# Rides
# --------------------------------------------------------------------------------------
def build_rides(cfg):
    first, last = date_window(cfg["days_ahead"])
    log(f"Rides window: {first} .. {last}")
    rides, failures = [], []
    rw_cache, st_cache = {}, {}

    token = None
    try:
        token = strava_token()
    except Exception as e:  # noqa: BLE001
        failures.append(f"Strava auth: {e}")
    auth = {"Authorization": f"Bearer {token}"} if token else None
    if not auth:
        log("Strava credentials not set - Strava clubs are skipped this run.")

    for club in cfg["clubs"]:
        if club.get("enabled") is False:
            continue
        try:
            if club["source"] == "rwgps":
                got = fetch_rwgps_club(club, cfg, first, last, rw_cache)
            elif club["source"] == "strava":
                if not auth:
                    continue
                got = fetch_strava_club(club, cfg, first, last, auth, st_cache)
            elif club["source"] == "meetup":
                got = fetch_meetup_group(club, cfg, first, last, rw_cache)
            elif club["source"] == "tribe":
                got = fetch_tribe_calendar(club, cfg, first, last, rw_cache)
            elif club["source"] == "ical":
                got = fetch_ical_feed(club, cfg, first, last, rw_cache)
            else:
                continue
            log(f"  {club['name']} [{club['source']}]: {len(got)} ride(s)")
            rides += got
        except Exception as e:  # noqa: BLE001 - one broken club must not sink the run
            failures.append(f"{club['name']}: {e}")
            log(f"  {club['name']} [{club['source']}]: FAILED - {e}")

    # paved/unpaved from OpenStreetMap for routes that came without surface data
    for cache in (rw_cache, st_cache):
        for rid, r in cache.items():
            if r and r["needs_osm"]:
                tags = osm_surfaces([p[:2] for p in r["points"]])
                if tags:
                    for p, t in zip(r["points"], tags):
                        p[3] = t
                r["needs_osm"] = False

    for r in rides:
        r["route"] = thin_route(r["route"])

    boulder, boulder_detailed = [], {}
    try:
        boulder, boulder_detailed = fetch_boulder_feed(cfg, first, last)
        seen = {(r["url"], str(r["route_id"])) for r in rides}
        boulder = [r for r in boulder if (r.get("url"), str(r.get("route_id"))) not in seen]
        log(f"  Boulder feed: {len(boulder)} ride(s)")
    except Exception as e:  # noqa: BLE001
        failures.append(f"Boulder feed: {e}")
        log(f"  Boulder feed FAILED - {e}")

    rides += boulder
    rides, warnings = finish_rides(rides, cfg)
    rides.sort(key=lambda r: (r["date"], r["club_name"], r["title"]))
    return rides, boulder_detailed, failures, warnings


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def finish_rides(rides, cfg):
    """
    Final pass over every ride, whatever its source:
      - assigns the area from the start point, drops rides outside coverage
      - re-measures distance and unpaved share from the route geometry and uses the
        measured distance when the source's own figure is missing or off by more than 10 %
      - validates dates, coordinates and elevation figures
      - removes duplicates (the same ride published on two platforms)
    """
    out, warnings = [], []
    for r in rides:
        label = f"{r.get('club_name')} / {r.get('title')} ({r.get('date')})"
        try:
            dt.datetime.strptime(r["date"], "%Y-%m-%d %H:%M")
            start = r.get("starting_location") or (r["route"][0][:2] if r.get("route") else None)
            lat, lng = float(start[0]), float(start[1])
        except (KeyError, TypeError, ValueError, IndexError):
            warnings.append(f"dropped (bad date or start point): {label}")
            continue
        region = region_for([lat, lng], cfg)
        if not region:
            warnings.append(f"dropped (starts outside coverage): {label}")
            continue
        r["region"] = region
        r["starting_location"] = [round(lat, 5), round(lng, 5)]
        r.setdefault("id", ride_id(r.get("url"), r.get("route_id"), r["date"]))
        for key in ("description", "start_name", "start_address"):
            r.setdefault(key, None)
        r["route"] = [p for p in (r.get("route") or [])
                      if len(p) >= 2 and -90 <= p[0] <= 90 and -180 <= p[1] <= 180]
        measured_m, unpaved_pct = route_metrics(r["route"])
        r["has_route"] = measured_m is not None
        r["unpaved_pct"] = unpaved_pct
        if measured_m is not None:
            measured_mi = round(measured_m * MI_PER_M, 1)
            stated = r.get("dist_mi")
            if stated is None or stated <= 0:
                r["dist_mi"] = measured_mi
            elif abs(measured_mi - stated) > max(0.5, 0.10 * stated):
                warnings.append(f"distance corrected {stated} -> {measured_mi} mi (measured from route): {label}")
                r["dist_mi"] = measured_mi
            # start pin more than 2 miles from the route: trust the route
            off = miles_between(r["starting_location"], r["route"][0][:2])
            if off > 2 and miles_between(r["starting_location"], r["route"][-1][:2]) > 2:
                warnings.append(f"start pin was {off:.1f} mi from its route, moved to route start: {label}")
                r["starting_location"] = [round(r["route"][0][0], 5), round(r["route"][0][1], 5)]
        else:
            r["dist_mi"] = r["elev_gain_ft"] = None
        gain = r.get("elev_gain_ft")
        if gain is not None and (gain < 0 or (r.get("dist_mi") and gain / r["dist_mi"] > 600)):
            warnings.append(f"elevation gain {gain} ft looks wrong and was removed: {label}")
            r["elev_gain_ft"] = None
        r["ride_type"] = ride_type_for(r["title"], r.get("description") or "", unpaved_pct)
        r["pace_mph"] = r.get("_speed_mph")
        out.append(r)

    # duplicates: same start time, same title, start points within ~300 m -> keep the richest
    best = {}
    for r in out:
        key = (r["date"], _norm_title(r["title"]), round(r["starting_location"][0], 2), round(r["starting_location"][1], 2),
               r.get("route_id") if r["has_route"] else None)
        loose = key[:4]
        prev = best.get(loose)
        if prev is None:
            best[loose] = r
        elif prev["has_route"] and r["has_route"] and prev.get("route_id") != r.get("route_id"):
            best[key] = r  # same event, different route options: keep both
        elif r["has_route"] and not prev["has_route"]:
            best[loose] = r
            warnings.append(f"duplicate removed (kept the listing with a route): {prev['club_name']} / {prev['title']}")
        else:
            warnings.append(f"duplicate removed: {r['club_name']} / {r['title']} ({r['source']})")
    return list(best.values()), warnings


def club_directory(cfg, rides):
    """The club badge list shown on the "More" page (data/clubs.json)."""
    strava_on = bool(os.environ.get("STRAVA_REFRESH_TOKEN"))
    out = []
    for c in cfg["clubs"]:
        if c.get("enabled") is False:
            continue
        url = c.get("url")
        if not url:
            if c["source"] == "strava":
                url = f"https://www.strava.com/clubs/{c['id']}"
            elif c["source"] == "meetup":
                url = f"https://www.meetup.com/{c['id']}/"
            elif c["source"] in ("tribe", "ical"):
                url = c["id"]
            else:
                kind = "clubs" if c.get("rwgps_kind") == "club" else "organizations"
                url = f"{RWGPS}/{kind}/{c['id']}"
        out.append({"name": c["name"], "url": url, "region": c.get("region", "denver"), "source": c["source"],
                    "active": c["source"] != "strava" or strava_on})
    listed = {c["name"].lower() for c in out}
    for r in rides:  # Boulder clubs that have a ride in the current feed
        if r.get("_feed") and r["club_name"].lower() not in listed:
            listed.add(r["club_name"].lower())
            if r.get("source") == "Strava" and r.get("club_id"):
                url = f"https://www.strava.com/clubs/{r['club_id']}"
            elif r.get("source") == "Ride with GPS" and r.get("club_id"):
                url = f"{RWGPS}/organizations/{r['club_id']}"
            else:
                url = "https://boulderrides.cc"
            out.append({"name": r["club_name"], "url": url, "region": "boulder", "source": "feed", "active": True})
    if cfg.get("boulder_feed"):
        out.append({"name": "All Boulder clubs: boulderrides.cc", "url": "https://boulderrides.cc",
                    "region": "boulder", "source": "feed", "active": True})
    return out


# --------------------------------------------------------------------------------------
# Races & events (BikeReg + hand-maintained extras)
# --------------------------------------------------------------------------------------
BIKEREG_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def bikereg_date(s):
    m = BIKEREG_DATE.match(s or "")
    if not m:
        return None
    off = m.group(2) or "+0000"
    tz = dt.timezone(dt.timedelta(hours=int(off[:3]), minutes=int(off[0] + off[3:])))
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz).date()


def near_any_region(lat, lng, cfg, radius):
    return any(miles_between(reg["center"], [lat, lng]) <= radius for reg in cfg["regions"].values())


def build_races(cfg):
    today = now_local().date()
    horizon = today + dt.timedelta(days=cfg["races_days_ahead"])
    radius = cfg["races_radius_mi"]
    out, page, fetched = [], 0, 0
    while True:
        data = http_get(f"https://www.bikereg.com/api/search?states=CO&startpage={page}")
        events = data.get("MatchingEvents") or []
        for e in events:
            name = (e.get("EventName") or "").strip()
            start = bikereg_date(e.get("EventDate"))
            end = bikereg_date(e.get("EventEndDate")) or start
            lat, lng = e.get("Latitude"), e.get("Longitude")
            if not name or not start or lat is None or lng is None:
                continue
            if name.lower().startswith("volunteer"):
                continue
            if end < today or start > horizon or not near_any_region(lat, lng, cfg, radius):
                continue
            days = [(start + dt.timedelta(days=i)).isoformat() for i in range(min((end - start).days, 13) + 1)]
            types = list(dict.fromkeys(e.get("EventTypes") or []))
            out.append({
                "name": name,
                "url": e.get("EventWebsite") or e.get("EventUrl") or "",
                "reg_link": e.get("EventUrl") or (f"https://www.bikereg.com/{e['EventId']}" if e.get("EventId") else ""),
                "location": e.get("EventCity") or "",
                "latitude": lat,
                "longitude": lng,
                "type": types,
                "date": days[0] if len(days) == 1 else days,
            })
        page += 1
        fetched += len(events)
        if not events or fetched >= (data.get("ResultCount") or 0) or page > 30:
            break

    # Major events that are not on BikeReg: maintained by hand in backend/extra_events.json
    extra_path = os.path.join(HERE, "extra_events.json")
    if os.path.exists(extra_path):
        with open(extra_path, encoding="utf-8") as f:
            for e in json.load(f):
                dates = e["date"] if isinstance(e["date"], list) else [e["date"]]
                if dt.date.fromisoformat(dates[-1]) >= today and dt.date.fromisoformat(dates[0]) <= horizon:
                    out.append({k: v for k, v in e.items() if not k.startswith("_")})

    seen, uniq = set(), []
    for r in sorted(out, key=lambda r: (r["date"][0] if isinstance(r["date"], list) else r["date"], r["name"])):
        k = (r["name"].lower(), json.dumps(r["date"]))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    log(f"Races: {len(uniq)} within {radius} mi over the next {cfg['races_days_ahead']} days")
    return uniq


# --------------------------------------------------------------------------------------
# Weather (National Weather Service)
# --------------------------------------------------------------------------------------
NWS = "https://api.weather.gov"
NWS_HEADERS = {"Accept": "application/geo+json"}


def weather_symbol(text, daytime=True):
    t = (text or "").lower()
    if "thunder" in t:
        return "🌩️"
    if any(w in t for w in ("snow", "flurr", "blizzard", "sleet", "wintry", "ice", "freezing")):
        return "🌨️"
    if any(w in t for w in ("rain", "shower", "drizzle")):
        return "🌧️"
    if any(w in t for w in ("fog", "haze", "smoke", "mist")):
        return "🌫️"
    if "mostly sunny" in t or "mostly clear" in t:
        return "🌤️" if daytime else "🌙"
    if "partly" in t:
        return "⛅"
    if "mostly cloudy" in t:
        return "🌥️"
    if "cloud" in t or "overcast" in t:
        return "☁️"
    if "wind" in t or "breezy" in t or "blustery" in t:
        return "💨"
    if "sunny" in t or "clear" in t or "fair" in t:
        return "☀️" if daytime else "🌙"
    return "🌡️"


_points_cache = {}


def nws_point(lat, lng):
    key = (round(lat, 2), round(lng, 2))
    if key not in _points_cache:
        p = http_get(f"{NWS}/points/{key[0]},{key[1]}", headers=NWS_HEADERS)["properties"]
        _points_cache[key] = {"hourly": p["forecastHourly"], "grid": p["forecastGridData"]}
    return _points_cache[key]


def hour_label(h):
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def build_weather(cfg):
    lat, lng = cfg["weather_point"]
    periods = http_get(nws_point(lat, lng)["hourly"], headers=NWS_HEADERS)["properties"]["periods"]
    out = {}
    for p in periods:
        start = parse_iso(p["startTime"]).astimezone(TZ)
        dst = bool(start.dst())
        wanted = (9, 16) if dst else (10, 14)  # same display hours as the Boulder site
        if start.hour not in wanted:
            continue
        temp = p["temperature"]
        if p.get("temperatureUnit") == "C":
            temp = round(temp * 9 / 5 + 32)
        out.setdefault(start.date().isoformat(), []).append({
            "time": hour_label(start.hour),
            "temperature_F": temp,
            "description": p.get("shortForecast") or "",
            "symbol": weather_symbol(p.get("shortForecast"), p.get("isDaytime", True)),
        })
    log(f"Weather: {len(out)} day(s) for {lat},{lng}")
    return out


# ---- detailed (along-the-route) weather ----------------------------------------------
DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _series(layer):
    """NWS gridpoint layer -> [(start, end, value), ...]"""
    out = []
    for v in (layer or {}).get("values", []):
        start_s, dur_s = v["validTime"].split("/")
        start = parse_iso(start_s)
        d, h, m = (int(x or 0) for x in DURATION.match(dur_s).groups())
        out.append((start, start + dt.timedelta(days=d, hours=h, minutes=m), v["value"]))
    return out


def _at(series, when):
    for start, end, value in series:
        if start <= when < end:
            return value
    return None


_grid_cache = {}


def grid_forecast(lat, lng):
    pt = nws_point(lat, lng)
    if pt["grid"] not in _grid_cache:
        g = http_get(pt["grid"], headers=NWS_HEADERS)["properties"]
        hourly = http_get(pt["hourly"], headers=NWS_HEADERS)["properties"]["periods"]
        _grid_cache[pt["grid"]] = {
            "temp": _series(g.get("temperature")), "feels": _series(g.get("apparentTemperature")),
            "wind": _series(g.get("windSpeed")), "gust": _series(g.get("windGust")),
            "dir": _series(g.get("windDirection")), "pop": _series(g.get("probabilityOfPrecipitation")),
            "text": [(parse_iso(p["startTime"]), parse_iso(p["endTime"]), (p.get("shortForecast"), p.get("isDaytime", True))) for p in hourly],
        }
    return _grid_cache[pt["grid"]]


def c_to_f(c):
    return None if c is None else round(c * 9 / 5 + 32)


def kmh_to_mph(k):
    return None if k is None else round(k * 0.621371, 1)


def sun_times(day, lat, lng):
    """Sunrise / sunset (NOAA solar calculator), as local datetimes. None in polar day/night."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + 0.5)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl))
              - math.tan(math.radians(lat)) * math.tan(decl))
    if abs(cos_ha) > 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    base = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    rise = base + dt.timedelta(minutes=720 - 4 * (lng + ha) - eqtime)
    sset = base + dt.timedelta(minutes=720 - 4 * (lng - ha) - eqtime)
    return rise.astimezone(TZ), sset.astimezone(TZ)


def point_at(route, cum, dist_m):
    """[lat, lng] at dist_m along the route."""
    if dist_m <= 0:
        return route[0][:2]
    if dist_m >= cum[-1]:
        return route[-1][:2]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= dist_m:
            lo = mid
        else:
            hi = mid
    span = (cum[hi] - cum[lo]) or 1.0
    t = (dist_m - cum[lo]) / span
    return [route[lo][0] + (route[hi][0] - route[lo][0]) * t, route[lo][1] + (route[hi][1] - route[lo][1]) * t]


def build_detailed_weather(rides, boulder_detailed, interval_min=20):
    out = []
    for r in rides:
        key = (r["title"], r["date"])
        base = {"club_name": r["club_name"], "ride_title": r["title"], "date": r["date"],
                "dist_miles": r["dist_mi"], "average_speed_mph": r.get("_speed_mph"),
                "status": "not available", "samples": []}
        if key in boulder_detailed:  # already computed upstream for Boulder rides
            out.append(boulder_detailed[key])
            continue
        speed = r.get("_speed_mph")
        if not speed or not r.get("dist_mi") or not r.get("route") or r.get("_feed"):
            out.append(base)
            continue
        try:
            start = dt.datetime.strptime(r["date"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            cum = cumulative_m(r["route"])
            total_mi = cum[-1] * MI_PER_M
            samples, k = [], 0
            while True:
                mi = min(speed * k * interval_min / 60, total_mi)
                when = start + dt.timedelta(minutes=k * interval_min if mi < total_mi else total_mi / speed * 60)
                lat, lng = point_at(r["route"], cum, mi / MI_PER_M)
                g = grid_forecast(lat, lng)
                temp = c_to_f(_at(g["temp"], when))
                if temp is None:
                    raise ValueError("forecast does not reach the ride time yet")
                wind = kmh_to_mph(_at(g["wind"], when)) or 0.0
                gust = kmh_to_mph(_at(g["gust"], when))
                deg = _at(g["dir"], when)
                text, daytime = _at(g["text"], when) or ("", True)
                samples.append({
                    "timestamp": when.replace(microsecond=0).isoformat(),
                    "dist_miles": round(mi, 2),
                    "lat": round(lat, 5), "lon": round(lng, 5),
                    "temp_F": temp,
                    "feels_like_F": c_to_f(_at(g["feels"], when)) if _at(g["feels"], when) is not None else temp,
                    "wind_speed_mph": wind,
                    "wind_dir": COMPASS[int((deg % 360) / 22.5 + 0.5) % 16] if deg is not None else "",
                    "wind_gust_mph": gust if gust is not None else wind,
                    "rain_chance": int(_at(g["pop"], when) or 0),
                    "symbol": weather_symbol(text, daytime),
                    "description": text or "",
                })
                if mi >= total_mi or k > 60:
                    break
                k += 1
            rise, sset = sun_times(start.date(), r["route"][0][0], r["route"][0][1])
            base.update({"status": "available", "samples": samples})
            if rise and sset:
                base.update({"sunrise": rise.replace(microsecond=0).isoformat(),
                             "sunset": sset.replace(microsecond=0).isoformat()})
        except Exception as e:  # noqa: BLE001
            log(f"  detailed weather skipped for {r['title']}: {e}")
        out.append(base)
    log(f"Detailed weather: {sum(1 for r in out if r['status'] == 'available')} of {len(out)} ride(s)")
    return {"sample_interval_minutes": interval_min, "ride_count": len(out), "rides": out}


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def write_json(path, data):
    """Writes only when the content changed. Returns True when written."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "data"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--only", default="rides,races,weather,detailed",
                    help="comma list of: rides, races, weather, detailed")
    args = ap.parse_args()
    want = set(args.only.split(","))
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(args.out, exist_ok=True)
    problems = []

    # Geocoder results are kept between runs so each address is only ever looked up once
    geocache_path = os.path.join(args.out, "geocache.json")
    if os.path.exists(geocache_path):
        try:
            with open(geocache_path, encoding="utf-8") as f:
                _geocode_cache.update({k: v for k, v in json.load(f).items() if v})
        except (OSError, ValueError):
            pass

    if "rides" in want:
        rides, boulder_detailed, failures, warnings = build_rides(cfg)
        problems += failures
        warnings = list(warnings)
        if "detailed" in want:
            try:
                write_json(os.path.join(args.out, "detailed_weather.json"),
                           build_detailed_weather(rides, boulder_detailed))
            except Exception as e:  # noqa: BLE001
                problems.append(f"detailed weather: {e}")
        public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rides]
        write_json(os.path.join(args.out, "club_rides.json"), public)
        write_json(os.path.join(args.out, "clubs.json"), club_directory(cfg, public))
        by_region = {}
        for r in public:
            by_region[r.get("region", "?")] = by_region.get(r.get("region", "?"), 0) + 1
        log(f"Rides written: {len(public)} {by_region}")

    for name, fn in (("races", build_races), ("weather", build_weather)):
        if name in want:
            path = os.path.join(args.out, f"{name}.json")
            try:
                data = fn(cfg)
                if name == "weather" and os.path.exists(path):
                    # The hourly forecast starts "now": keep today's earlier slots from the last run
                    today = now_local().date().isoformat()
                    with open(path, encoding="utf-8") as f:
                        old = json.load(f).get(today) or []
                    have = {e["time"] for e in data.get(today, [])}
                    merged = [e for e in old if e["time"] not in have] + data.get(today, [])
                    if merged:
                        data = {today: merged, **{k: v for k, v in data.items() if k != today}}
                write_json(path, data)
            except Exception as e:  # noqa: BLE001 - keep the previous file on failure
                problems.append(f"{name}: {e}")

    status = {
        "generated_at": now_local().replace(microsecond=0).isoformat(),
        "strava_enabled": bool(os.environ.get("STRAVA_REFRESH_TOKEN")),
        "problems": problems,
    }
    if "rides" in want:
        status.update({
            "rides_total": len(public),
            "rides_by_region": by_region,
            "rides_by_source": {k: sum(1 for r in public if r["source"] == k) for k in sorted({r["source"] for r in public})},
            "rides_without_route": sum(1 for r in public if not r["has_route"]),
            "checks": warnings,
        })
    write_json(os.path.join(args.out, "status.json"), status)
    if _geocode_cache:
        write_json(geocache_path, {k: v for k, v in sorted(_geocode_cache.items()) if v})
    if problems:
        log("Problems:\n  " + "\n  ".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
