"""
ADS-B live flight enrichment.

For each tail in our legs table with source='adsb-live':
  1. Fetch the past-24h flight trace from adsb.lol (Tar1090-style globe endpoint).
  2. Find the LAST ground→airborne transition — that's the takeoff of the
     current flight. Reverse-geocode to nearest airport.
  3. Read the LAST trace point — current position, altitude, speed, descent rate.
  4. Derive flight_phase: DEPARTING / CRUISE / APPROACH / LANDED.
  5. Predict destination by:
       - if currently below 10k ft + descending → nearest big airport along heading
       - else → project current heading + ground speed against fuel range
  6. Compute ETA = distance_to_predicted_dest / ground_speed.

Writes enriched fields back to legs table:
  origin, origin_city, destination, destination_city,
  flight_phase, predicted_eta_utc, takeoff_time_utc, current_alt_ft,
  current_gs_kt, descent_rate_fpm

Designed to be called from a one-shot script (cron or scheduled task).
Pure stdlib + requests — no scipy/numpy needed.
"""
from __future__ import annotations

import os
import sys
import csv
import gzip
import json
import math
import time
import datetime as dt
from typing import Optional
from io import BytesIO

import requests

# ---------- airport database (loaded once, cached on disk) ----------

AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORTS_CACHE_PATH = "/tmp/airports.csv"
AIRPORTS_CACHE_TTL_DAYS = 30


def _ensure_airports_csv() -> str:
    """Download OurAirports CSV if missing or stale. Return local path."""
    need_download = True
    if os.path.exists(AIRPORTS_CACHE_PATH):
        age_days = (time.time() - os.path.getmtime(AIRPORTS_CACHE_PATH)) / 86400
        if age_days < AIRPORTS_CACHE_TTL_DAYS:
            need_download = False
    if need_download:
        r = requests.get(AIRPORTS_CSV_URL, timeout=60)
        r.raise_for_status()
        with open(AIRPORTS_CACHE_PATH, "wb") as f:
            f.write(r.content)
    return AIRPORTS_CACHE_PATH


_AIRPORTS_CACHE: list[dict] | None = None


def _airports() -> list[dict]:
    """Load all usable 4-letter ICAO airports, lazy-cached in memory."""
    global _AIRPORTS_CACHE
    if _AIRPORTS_CACHE is not None:
        return _AIRPORTS_CACHE
    path = _ensure_airports_csv()
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
                continue
            ident = (row.get("ident") or "").strip()
            if len(ident) != 4 or not ident.isalpha() or not ident.isupper():
                continue
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            out.append({
                "icao": ident,
                "iata": (row.get("iata_code") or "").strip() or None,
                "name": (row.get("name") or "").strip(),
                "city": (row.get("municipality") or "").strip(),
                "country": (row.get("iso_country") or "").strip(),
                "region": (row.get("iso_region") or "").strip(),
                "type": row["type"],
                "lat": lat,
                "lon": lon,
            })
    _AIRPORTS_CACHE = out
    return out


# ---------- math helpers ----------

EARTH_RADIUS_NM = 3440.065  # nautical miles


def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    lat1r, lon1r = math.radians(lat1), math.radians(lon1)
    lat2r, lon2r = math.radians(lat2), math.radians(lon2)
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def nearest_airport(lat, lon, max_nm=25.0, prefer_large=True):
    """Find nearest ICAO airport. None if no airport within max_nm."""
    best = None
    best_d = max_nm
    for ap in _airports():
        d = haversine_nm(lat, lon, ap["lat"], ap["lon"])
        if d > best_d:
            continue
        # When two airports are close, prefer larger one (KFLL over KFXE for downtown)
        if prefer_large and best is not None:
            order = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
            if order[ap["type"]] < order[best["type"]] and d < max_nm * 0.6:
                best = ap
                best_d = d
                continue
        best = ap
        best_d = d
    return (best, best_d) if best else (None, None)


# ---------- trace fetcher ----------

def fetch_trace(icao24_hex: str) -> Optional[dict]:
    """
    Fetch the past-24h trace for an aircraft.
    icao24_hex is the 6-char hex transponder code (e.g. 'ab2404').
    """
    if not icao24_hex or len(icao24_hex) != 6:
        return None
    last2 = icao24_hex[-2:].lower()
    url = f"https://globe.adsb.lol/data/traces/{last2}/trace_full_{icao24_hex.lower()}.json"
    try:
        r = requests.get(
            url, timeout=15,
            headers={"User-Agent": "JETLUXECO/1.0", "Accept-Encoding": "gzip"},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ---------- trace analysis ----------

def _is_ground_point(p) -> bool:
    """Trace point format: [secs, lat, lon, alt, gs, track, flags, descent_rate, ...].
    alt is either a number (feet) or the string 'ground'."""
    if not p or len(p) < 5:
        return False
    alt = p[3]
    return alt == "ground" or (isinstance(alt, (int, float)) and alt < 50)


def _safe_num(v):
    try:
        if v is None or v == "ground":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def analyze_trace(trace_doc: dict) -> dict:
    """
    Pull out the current-flight context:
      - takeoff_idx: index of first non-ground point of CURRENT flight
      - takeoff_lat/lon/time_s: position of takeoff
      - latest position + altitude + gs + descent_rate
      - flight_phase
    """
    if not trace_doc or not trace_doc.get("trace"):
        return {}
    trace = trace_doc["trace"]
    timestamp_base = trace_doc.get("timestamp", 0)

    # Find the LAST ground point — current flight starts after that
    last_ground_idx = -1
    for i in range(len(trace) - 1, -1, -1):
        if _is_ground_point(trace[i]):
            last_ground_idx = i
            break

    # Takeoff = first point after last ground
    if last_ground_idx >= 0 and last_ground_idx + 1 < len(trace):
        takeoff_idx = last_ground_idx + 1
        # But really we want the moment of departure — last ground point or first airborne
        takeoff_point = trace[last_ground_idx]
    elif last_ground_idx == -1:
        # No ground points in the trace — aircraft has been airborne the whole 24h window
        takeoff_idx = 0
        takeoff_point = trace[0]
    else:
        # Currently on the ground
        takeoff_idx = None
        takeoff_point = None

    latest = trace[-1]
    current_alt = _safe_num(latest[3])
    current_gs = _safe_num(latest[4]) if len(latest) > 4 else None
    current_track = _safe_num(latest[5]) if len(latest) > 5 else None
    current_descent = _safe_num(latest[7]) if len(latest) > 7 else None
    current_lat, current_lon = latest[1], latest[2]

    # Flight phase derivation
    on_ground_now = _is_ground_point(latest)
    if on_ground_now:
        phase = "LANDED"
    elif current_alt is not None and current_alt < 5000 and current_descent is not None and current_descent > 200:
        phase = "DEPARTING"
    elif current_alt is not None and current_alt < 10000 and current_descent is not None and current_descent < -300:
        phase = "APPROACH"
    elif current_descent is not None and current_descent < -1500:
        phase = "DESCENDING"
    elif current_alt is not None and current_alt > 18000:
        phase = "CRUISE"
    else:
        phase = "EN ROUTE"

    result = {
        "phase": phase,
        "current_lat": current_lat,
        "current_lon": current_lon,
        "current_alt_ft": current_alt,
        "current_gs_kt": current_gs,
        "current_track": current_track,
        "current_descent_fpm": current_descent,
        "timestamp_base": timestamp_base,
    }

    if takeoff_point is not None:
        # Takeoff position
        result["takeoff_lat"] = takeoff_point[1]
        result["takeoff_lon"] = takeoff_point[2]
        # Absolute takeoff timestamp = timestamp_base + seconds_offset
        result["takeoff_time_utc"] = dt.datetime.fromtimestamp(
            timestamp_base + takeoff_point[0], tz=dt.timezone.utc
        ).isoformat()
    return result


def project_destination(lat, lon, track_deg, gs_kt, current_alt_ft, descent_fpm):
    """
    Predict destination airport.
      - If on approach (< 10k ft + descending): nearest large/medium airport ahead
      - Otherwise: project heading + remaining fuel range against airport DB
    """
    if track_deg is None or gs_kt is None or gs_kt < 20:
        return None, None

    # Time to ground from current altitude assuming current descent rate (capped)
    on_approach = (
        current_alt_ft is not None and current_alt_ft < 12000
        and descent_fpm is not None and descent_fpm < -200
    )

    if on_approach:
        # Look 5–40 NM ahead in heading direction for nearest airport
        for projection_nm in (5, 10, 20, 30, 40):
            target_lat, target_lon = _project(lat, lon, track_deg, projection_nm)
            ap, d = nearest_airport(target_lat, target_lon, max_nm=20.0)
            if ap and ap["type"] in ("large_airport", "medium_airport"):
                return ap, d

    # Cruise prediction: project current track ahead.
    # At cruise altitude (>25k ft), destination is at least 60-100nm away,
    # so start the search there. Score weights distance-from-projection more
    # heavily than projection-length to find the airport ON the path.
    if current_alt_ft and current_alt_ft > 25000:
        projections = (100, 150, 200, 300, 400, 500, 600, 800)
    elif current_alt_ft and current_alt_ft > 12000:
        projections = (40, 75, 120, 180, 250, 350)
    else:
        projections = (20, 40, 75, 120, 180)

    best = None
    best_score = None
    for projection_nm in projections:
        target_lat, target_lon = _project(lat, lon, track_deg, projection_nm)
        ap, d = nearest_airport(target_lat, target_lon, max_nm=50.0, prefer_large=True)
        if ap and ap["type"] in ("large_airport", "medium_airport"):
            # Score: prioritize airports CLOSE to the projected point + within reasonable
            # range — penalize off-path airports more than path-length differences
            score = d * 10 + abs(projection_nm - 250) * 0.5
            if best_score is None or score < best_score:
                best_score = score
                best = (ap, projection_nm, d)
    if best:
        return best[0], best[1]  # return distance_to_dest = projection_nm (ground track distance)
    return None, None


def _project(lat, lon, bearing_deg, distance_nm):
    """Project a position along a bearing for distance_nm. Returns (lat, lon)."""
    R = EARTH_RADIUS_NM
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d_r = distance_nm / R
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r)
        + math.cos(lat1) * math.sin(d_r) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# ---------- N-number → ICAO24 hex lookup ----------

# The trace endpoint needs the 6-char ICAO24 hex. We may have N-numbers stored.
# Try multiple lookup paths.

def lookup_icao24_by_reg(registration: str) -> Optional[str]:
    """
    Look up the ICAO24 hex for an N-number.
    We try adsb.lol's live currently-broadcasting endpoint first (fast),
    then fall back to the FAA registry algorithm for US registrations.
    """
    if not registration:
        return None
    reg = registration.strip().upper().replace(" ", "")

    # Try the live-position endpoint first — only works if currently airborne
    try:
        r = requests.get(
            f"https://api.adsb.lol/v2/registration/{reg}",
            timeout=8,
            headers={"User-Agent": "JETLUXECO/1.0"},
        )
        if r.status_code == 200:
            d = r.json()
            ac = d.get("ac") or []
            if ac and ac[0].get("hex"):
                return ac[0]["hex"].lower()
    except Exception:
        pass

    # FAA registration → ICAO24 algorithm (US tail numbers N1 .. N99999 + letter variants)
    # Only handles US 'N' registrations
    if reg.startswith("N"):
        try:
            return _us_nnumber_to_icao24(reg)
        except Exception:
            return None
    return None


def _us_nnumber_to_icao24(n_number: str) -> Optional[str]:
    """FAA N-number → ICAO24 hex. Adapted from FAA's published algorithm."""
    # Strip leading "N"
    suffix = n_number[1:]
    if not suffix or not suffix[0].isdigit():
        return None

    # Parse digits + optional 1-2 letter suffix
    digits = ""
    letters = ""
    for c in suffix:
        if c.isdigit() and not letters:
            digits += c
        elif c.isalpha():
            letters += c
    if not digits or len(digits) > 5:
        return None

    # FAA: ICAO24 is decimal-based offset from US block start 0xA00000
    # See https://www.faa.gov/sites/faa.gov/files/data_research/aircraft_registration/N-Numbers.pdf
    def char_to_index(c):  # I, O excluded from FAA suffix letters
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        return chars.index(c.upper()) + 1

    n = int(digits)
    if n == 0 or n > 99999:
        return None

    bucket1 = 101711  # contribution from 1-digit prefix
    # FAA algorithm:
    offset = 0
    # First digit (1..9) → adds (first_digit - 1) * 101711
    first = int(digits[0])
    offset += (first - 1) * 101711
    rest = digits[1:]

    # Remaining digits/letters in the registration form a layered tree
    # Simplified iterative algorithm based on FAA documentation:
    suffix_letters_count = len(letters)
    if len(rest) == 0:
        sub = 0
    elif len(rest) == 1:
        sub_digit = int(rest)
        sub = sub_digit * 10111 + 1
    elif len(rest) == 2:
        sub = int(rest[0]) * 10111 + 1 + int(rest[1]) * 951 + 1
    elif len(rest) == 3:
        sub = (int(rest[0]) * 10111 + 1
               + int(rest[1]) * 951 + 1
               + int(rest[2]) * 35 + 1)
    elif len(rest) == 4:
        # 5-digit N-number with no letter suffix
        sub = (int(rest[0]) * 10111 + 1
               + int(rest[1]) * 951 + 1
               + int(rest[2]) * 35 + 1
               + int(rest[3]) + 1)
        if suffix_letters_count > 0:
            # Letters can't follow 5 digits
            return None
    else:
        return None

    if suffix_letters_count >= 1:
        sub += char_to_index(letters[0]) - 1
    if suffix_letters_count >= 2:
        sub = sub + 24 + char_to_index(letters[1]) - 1

    # Adjust ranges: subtract the digit×10111+1 when there are letters but only 1 digit pos used
    icao_dec = 0xA00001 + offset + sub
    return f"{icao_dec:06x}"


# ---------- top-level enrichment ----------

def enrich_one(tail: str) -> Optional[dict]:
    """Enrich a single tail. Returns enrichment dict or None if not findable."""
    hex_code = lookup_icao24_by_reg(tail)
    if not hex_code:
        return None
    trace = fetch_trace(hex_code)
    if not trace:
        return None
    analysis = analyze_trace(trace)
    if not analysis:
        return None

    result = {
        "tail": tail,
        "icao24": hex_code,
        "aircraft_type": trace.get("t"),
        "aircraft_desc": trace.get("desc"),
        "phase": analysis["phase"],
        "current_alt_ft": analysis.get("current_alt_ft"),
        "current_gs_kt": analysis.get("current_gs_kt"),
        "current_descent_fpm": analysis.get("current_descent_fpm"),
        "current_lat": analysis.get("current_lat"),
        "current_lon": analysis.get("current_lon"),
    }

    # Takeoff airport
    if "takeoff_lat" in analysis:
        ap, d = nearest_airport(analysis["takeoff_lat"], analysis["takeoff_lon"], max_nm=10.0)
        if ap:
            result["takeoff_icao"] = ap["icao"]
            result["takeoff_city"] = ap["city"]
            result["takeoff_country"] = ap["country"]
            result["takeoff_time_utc"] = analysis.get("takeoff_time_utc")

    # Destination prediction (only if airborne)
    if analysis["phase"] != "LANDED" and analysis.get("current_track") is not None:
        dest_ap, dist_nm = project_destination(
            analysis["current_lat"], analysis["current_lon"],
            analysis["current_track"], analysis["current_gs_kt"],
            analysis.get("current_alt_ft"), analysis.get("current_descent_fpm"),
        )
        if dest_ap:
            result["predicted_dest_icao"] = dest_ap["icao"]
            result["predicted_dest_city"] = dest_ap["city"]
            result["predicted_dest_country"] = dest_ap["country"]
            result["distance_to_dest_nm"] = round(dist_nm, 1) if dist_nm else None
            # ETA
            if analysis.get("current_gs_kt") and analysis["current_gs_kt"] > 50:
                hours = dist_nm / analysis["current_gs_kt"]
                result["eta_utc"] = (
                    dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(hours=hours)
                ).isoformat()
                result["eta_minutes"] = round(hours * 60)

    return result


if __name__ == "__main__":
    # Smoke test: enrich one tail passed on CLI
    if len(sys.argv) > 1:
        result = enrich_one(sys.argv[1])
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Usage: python adsb_enrich.py <N-NUMBER>")
