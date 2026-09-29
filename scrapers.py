"""
Empty leg scrapers. One function per source.

Each scraper returns a list of dicts with keys:
  source, operator, origin, origin_city, destination, destination_city,
  depart_date (YYYY-MM-DD), depart_time, aircraft, category, pax, price_usd, url

If a source breaks (404, layout change, anti-bot), that scraper logs the error
and returns []. Other scrapers continue.

How to add a new source:
  1. Write a function `scrape_yoursource() -> list[dict]`
  2. Add it to the SCRAPERS list at the bottom
  3. Push and redeploy

Most sites are heavily client-rendered. Strategies that work:
  - Hit the underlying JSON API (find it in browser DevTools → Network)
  - Find an RSS or sitemap feed (often surfaces structured data)
  - Sign up for the operator's email list and parse the emails (Phase 3)
  - For static HTML pages, use BeautifulSoup as shown below
"""
import re
import json
import datetime as dt
import requests
from bs4 import BeautifulSoup

import db

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 15


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _norm_date(s):
    """Try several date formats. Return YYYY-MM-DD or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d %b %Y", "%b %d, %Y",
                "%B %d, %Y", "%d-%b-%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_price(s):
    if not s:
        return None
    digits = re.sub(r"[^\d.]", "", str(s))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _categorize(aircraft):
    """Best-effort aircraft → category mapping. Handles both model names and category labels."""
    if not aircraft:
        return None
    a = aircraft.lower().strip()
    # Direct category-label matches (e.g. "Heavy Jet" from Evojets)
    if "turboprop" in a:
        return "turboprop"
    if "very light" in a:
        return "very_light"
    if "ultra" in a and "long" in a:
        return "ultra_long"
    if "super" in a and ("mid" in a or "midsize" in a):
        return "super_midsize"
    if "heavy" in a:
        return "heavy"
    if "midsize" in a or "mid-size" in a or "mid size" in a:
        return "midsize"
    if a == "light jet" or a.endswith(" light jet") or "light jet" in a:
        return "light"
    # Model-name heuristics
    if any(x in a for x in ["citation m2", "phenom 100", "citation cj", "cj1", "cj2", "cj3", "cj4",
                            "c25a", "c25b", "c25c", "c525", "honda", "vision jet", "sf50",
                            "c510", "citation mustang"]):
        return "light"
    if any(x in a for x in ["phenom 300", "citation xls", "learjet 75", "hawker 400",
                            "citation excel", "c560", "c56x", "c550"]):
        return "midsize"
    if any(x in a for x in ["challenger 350", "challenger 300", "citation longitude",
                            "citation latitude", "praetor 500", "praetor 600",
                            "citation sovereign", "c680", "c700", "c68a",
                            "hawker 800", "hawker 900", "e545", "e550",
                            "legacy 450", "legacy 500"]):
        return "super_midsize"
    if any(x in a for x in ["challenger 605", "challenger 650", "falcon 2000",
                            "gulfstream g280", "legacy 600", "legacy 650"]):
        return "heavy"
    if any(x in a for x in ["g650", "g700", "g500", "g600", "global 7500", "global 6000",
                            "global 5000", "falcon 7x", "falcon 8x", "falcon 900"]):
        return "ultra_long"
    return None


# --------------------------------------------------------------------------
# Scrapers — public empty leg pages
# --------------------------------------------------------------------------

def scrape_magellan():
    """Magellan Jets — public empty legs page."""
    source = "magellan"
    url = "https://www.magellanjets.com/empty-legs/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        legs = []
        # Find JSON-LD structured data first
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                # Look for offers or flights
                _harvest_json_ld(data, legs, source, "Magellan Jets", url)
            except Exception:
                continue
        # Fall back to scraping flight cards
        for card in soup.select("[class*=empty-leg], [class*=flight-card], article, .deal-card"):
            text = card.get_text(" ", strip=True)
            icaos = re.findall(r"\b[KE][A-Z]{3}\b", text)
            if len(icaos) >= 2:
                date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
                price_match = re.search(r"\$[\d,]+", text)
                if date_match and _norm_date(date_match.group(1)):
                    legs.append({
                        "source": source,
                        "operator": "Magellan Jets",
                        "origin": icaos[0],
                        "destination": icaos[1],
                        "depart_date": _norm_date(date_match.group(1)),
                        "price_usd": _norm_price(price_match.group(0)) if price_match else None,
                        "url": url,
                        "raw": text[:500],
                    })
        return legs
    except Exception:
        return []


def scrape_privatefly():
    """PrivateFly — public empty legs page."""
    source = "privatefly"
    url = "https://www.privatefly.com/private-jet-empty-legs.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        legs = []
        for row in soup.select("tr, .empty-leg-row, [class*=leg-item]"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "div"]) if c.get_text(strip=True)]
            if len(cells) < 3:
                continue
            text = " ".join(cells)
            icaos = re.findall(r"\b[KE][A-Z]{3}\b", text)
            if len(icaos) < 2:
                continue
            date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
            if not (date_match and _norm_date(date_match.group(1))):
                continue
            legs.append({
                "source": source,
                "operator": "PrivateFly (broker)",
                "origin": icaos[0],
                "destination": icaos[1],
                "depart_date": _norm_date(date_match.group(1)),
                "url": url,
                "raw": text[:500],
            })
        return legs
    except Exception:
        return []


def scrape_jettly():
    """Jettly marketplace — try public listings + their __NEXT_DATA__ JSON."""
    source = "jettly"
    candidates = [
        "https://jettly.com/empty-legs",
        "https://jettly.com/private-jet-empty-legs",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            # Many React/Next sites stash data in __NEXT_DATA__
            m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.+?\})</script>', r.text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    legs = []
                    _harvest_next_data(data, legs, source, "Jettly", url)
                    if legs:
                        return legs
                except Exception:
                    pass
            return []
        except Exception:
            continue
    return []


def scrape_stratos():
    """Stratos Jet Charters — try public deals page."""
    source = "stratos"
    candidates = [
        "https://www.stratosjets.com/empty-leg-flights/",
        "https://www.stratosjets.com/empty-legs/",
        "https://www.stratosjets.com/blog/empty-leg-flights",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            legs = []
            for row in soup.select("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue
                text = " ".join(cells)
                icaos = re.findall(r"\b[KE][A-Z]{3}\b", text)
                date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
                if len(icaos) >= 2 and date_match and _norm_date(date_match.group(1)):
                    legs.append({
                        "source": source,
                        "operator": "Stratos Jet Charters",
                        "origin": icaos[0],
                        "destination": icaos[1],
                        "depart_date": _norm_date(date_match.group(1)),
                        "url": url,
                        "raw": text[:500],
                    })
            if legs:
                return legs
        except Exception:
            continue
    return []


def scrape_aslgroup():
    """
    ASL Group (aslgroup.eu) — Belgian/Dutch operator with public empty-legs page.
    Server-rendered HTML, ~20 legs at a time, all European.
    Includes: specific aircraft model, ICAO codes for both endpoints, date, time,
    pax, aircraft photo URL.
    """
    source = "aslgroup"
    url = "https://www.aslgroup.eu/en/empty-legs"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    articles = re.findall(r'<article class="plane">(.*?)</article>', html, re.DOTALL)
    legs = []
    for art in articles:
        name_m = re.search(r'<span class="plane-name">([^<]+)</span>', art)
        img_m = re.search(r'<img src="(/images/fleet_fleet/[^"]+)"', art)
        route_m = re.search(
            r'<div class="leading-headline plane-headline">\s*([^<]+?)\s*<span[^>]*></span>\s*([^<]+?)\s*</div>',
            art,
        )
        specs_raw = re.findall(r'plane-spec-item-icon"\s*/>\s*([^<]+?)\s*<', art)
        link_m = re.search(r'href="(https://www\.aslgroup\.eu/en/empty-legs/request/\d+/[^"]+)"', art)
        if not (name_m and route_m):
            continue
        aircraft = name_m.group(1).strip()
        img_url = ("https://www.aslgroup.eu" + img_m.group(1)) if img_m else None
        o_m = re.match(r'(.+?)\(([A-Z]{4})\)', route_m.group(1).strip())
        d_m = re.match(r'(.+?)\(([A-Z]{4})\)', route_m.group(2).strip())
        if not (o_m and d_m):
            continue
        date_raw = specs_raw[0] if len(specs_raw) > 0 else None
        time_raw = specs_raw[1] if len(specs_raw) > 1 else None
        pax_raw = specs_raw[2] if len(specs_raw) > 2 else None
        date_iso = None
        if date_raw:
            dm = re.match(r'(\d{2})-(\d{2})-(\d{4})', date_raw)
            if dm:
                date_iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        pax = None
        if pax_raw:
            pm = re.search(r'\d+', pax_raw)
            if pm:
                pax = int(pm.group())
        if not date_iso:
            continue
        legs.append({
            "source": source,
            "operator": "ASL Group",
            "origin": o_m.group(2),
            "origin_city": o_m.group(1).strip(),
            "destination": d_m.group(2),
            "destination_city": d_m.group(1).strip(),
            "depart_date": date_iso,
            "depart_time": time_raw,
            "aircraft": aircraft,
            "category": _categorize(aircraft),
            "pax": pax,
            "price_usd": None,  # ASL doesn't publish prices on the public page
            "image_url": img_url,
            "url": link_m.group(1) if link_m else url,
            "raw": json.dumps({"src": "aslgroup", "request_url": link_m.group(1) if link_m else None}),
        })
    return legs


def scrape_evojets():
    """
    Evojets — public JSON API at /api/empty-legs.
    Returns aircraft CATEGORY (Heavy Jet, Light Jet, Midsize Jet, Super Midsize Jet,
    Turboprop) plus seats, destination city photo, expiration date.
    """
    source = "evojets"
    url = "https://www.evojets.com/api/empty-legs?limit=1000&sortBy=date&sortOrder=asc"
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        payload = r.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not items:
            return []
    except Exception:
        return []

    # Evojets aircraft labels → our canonical category slug
    CAT_MAP = {
        "turboprop": "turboprop",
        "very light jet": "very_light",
        "light jet": "light",
        "midsize jet": "midsize",
        "mid-size jet": "midsize",
        "super midsize jet": "super_midsize",
        "super-midsize jet": "super_midsize",
        "heavy jet": "heavy",
        "ultra long range jet": "ultra_long",
        "ultra-long range jet": "ultra_long",
    }

    def _icao(field):
        # "Oakland (KOAK)" -> "KOAK"
        m = re.search(r"\(([A-Z0-9]{3,4})\)", field or "")
        return m.group(1) if m else None

    def _city(field):
        # "Oakland (KOAK)" -> "Oakland"
        if not field:
            return None
        return re.sub(r"\s*\([^)]+\)\s*$", "", field).strip()

    legs = []
    for it in items:
        origin = _icao(it.get("depart"))
        dest = _icao(it.get("arrive"))
        dep_date = _norm_date(it.get("date"))
        if not origin or not dest or not dep_date:
            continue
        aircraft_label = (it.get("aircraft") or "").strip()
        category = CAT_MAP.get(aircraft_label.lower())
        image_path = it.get("featuredImage") or ""
        image_url = (
            ("https://www.evojets.com" + image_path)
            if image_path.startswith("/") else (image_path or None)
        )
        legs.append({
            "source": source,
            "operator": "Evojets",
            "origin": origin,
            "origin_city": _city(it.get("depart")),
            "destination": dest,
            "destination_city": _city(it.get("arrive")),
            "depart_date": dep_date,
            "aircraft": aircraft_label or None,
            "category": category,
            "pax": it.get("seats"),
            "price_usd": _norm_price(it.get("price")),
            "image_url": image_url,
            "url": "https://www.evojets.com/empty-leg-flights/",
            "raw": json.dumps({
                "id": it.get("id"),
                "expDate": it.get("expDate"),
                "ccPrice": it.get("ccPrice"),
            }),
        })
    return legs


def scrape_emptyjet_sitemap(max_flights=500):
    """
    EmptyJet aggregator — pulls from their public sitemap-flights.xml
    (~10,500 flight URLs) and extracts JSON-LD schema.org Flight data
    from each page. Rate-limited to max_flights per run.

    Focuses on US flights (most valuable for our broker context).
    """
    source = "emptyjet"
    sitemap_url = "https://emptyjet.com/sitemap-flights.xml"
    try:
        r = requests.get(sitemap_url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        # Parse sitemap XML — get URLs sorted by lastmod desc (freshest first)
        url_pattern = re.compile(
            r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>",
            re.DOTALL,
        )
        entries = url_pattern.findall(r.text)
        # Sort by lastmod DESC (newest first)
        entries.sort(key=lambda x: x[1], reverse=True)
        urls = [e[0] for e in entries[:max_flights]]
    except Exception as e:
        print(f"  emptyjet sitemap: {e}")
        return []

    legs = []
    # IATA→ICAO helper — try common US metros first
    def _iata_to_icao(iata):
        # US 3-letter → K prefix (heuristic)
        if not iata:
            return None
        iata = iata.strip().upper()
        if len(iata) == 4:
            return iata
        if len(iata) == 3:
            # Special cases where K-prefix doesn't apply
            hi_ak = {"HNL": "PHNL", "OGG": "PHOG", "KOA": "PHKO", "LIH": "PHLI",
                     "ITO": "PHTO", "ANC": "PANC"}
            if iata in hi_ak:
                return hi_ak[iata]
            return "K" + iata
        return iata

    # Fetch each flight page and extract JSON-LD Flight object
    for url in urls:
        try:
            fr = requests.get(url, headers=HEADERS, timeout=8)
            if fr.status_code != 200:
                continue
            # Find the JSON-LD block — could be an array of schema objects
            ld_match = re.search(
                r'<script type="application/ld\+json">(.+?)</script>',
                fr.text, re.DOTALL,
            )
            if not ld_match:
                continue
            try:
                ld = json.loads(ld_match.group(1))
            except json.JSONDecodeError:
                continue
            # ld is usually a list; find the Flight object
            items = ld if isinstance(ld, list) else [ld]
            flight = next((x for x in items if x.get("@type") == "Flight"), None)
            if not flight:
                continue

            dep_ap = flight.get("departureAirport") or {}
            arr_ap = flight.get("arrivalAirport") or {}
            aircraft = flight.get("aircraft") or {}
            offer = flight.get("offers") or {}

            dep_iata = dep_ap.get("iataCode")
            arr_iata = arr_ap.get("iataCode")
            if not (dep_iata and arr_iata):
                continue

            dep_icao = _iata_to_icao(dep_iata)
            arr_icao = _iata_to_icao(arr_iata)

            # Parse departure time — format "2026-10-04T05:00:00+00:00"
            dep_time_raw = flight.get("departureTime", "")
            dep_date = dep_time_raw[:10] if len(dep_time_raw) >= 10 else None
            dep_time = dep_time_raw[11:16] if len(dep_time_raw) >= 16 else None
            if not dep_date:
                continue

            price = None
            if offer.get("price"):
                try:
                    price = float(offer["price"])
                except (TypeError, ValueError):
                    pass

            aircraft_name = aircraft.get("name")

            legs.append({
                "source": source,
                "operator": "EmptyJet Marketplace",
                "origin": dep_icao,
                "origin_city": dep_ap.get("name", "").replace(" Airport", "").strip() or None,
                "destination": arr_icao,
                "destination_city": arr_ap.get("name", "").replace(" Airport", "").strip() or None,
                "depart_date": dep_date,
                "depart_time": dep_time,
                "aircraft": aircraft_name,
                "category": _categorize(aircraft_name),
                "price_usd": price,
                "url": url,
                "raw": json.dumps({
                    "src": "emptyjet",
                    "ej_id": url.rsplit("/", 1)[-1],
                    "modified": flight.get("dateModified"),
                }),
            })
        except Exception:
            continue
    return legs


def scrape_adsb_live():
    """
    Real-time airborne biz jet capture from adsb.lol.
    Pulls all aircraft within 250 NM of Miami and filters to biz-jet ICAO types.
    Each captured aircraft gets enriched immediately (origin/destination/phase/ETA).
    """
    source = "adsb-live"
    # Bounding box centered on Miami area for fast adsb.lol search
    # /v2/lat/{lat}/lon/{lon}/dist/{nm} returns everything within nm
    queries = [
        # (label, lat, lon, dist_nm)
        ("FL", 25.78, -80.29, 250),    # South Florida + Bahamas
        ("NE", 40.78, -73.97, 250),    # NYC metro + Northeast
        ("CA", 34.05, -118.24, 250),   # LA basin + SoCal
        ("TX", 32.86, -96.85, 250),    # DFW + Texas
    ]
    # Biz-jet aircraft type codes we want — matches our category map
    BIZ_JET_TYPES = {
        # Light + very light
        "SF50", "C25A", "C25B", "C25C", "C525", "C510", "E50P", "E55P",
        "HDJT", "HA4T", "LJ31", "LJ40", "LJ45", "BE40",
        # Midsize
        "C560", "C56X", "C550", "LJ55", "LJ60", "LJ70", "LJ75",
        "H25B", "H25C", "C750", "E135", "E145",
        # Super-midsize
        "CL30", "CL35", "C700", "C68A", "C680", "E545", "E550",
        # Heavy
        "CL60", "CL64", "F2TH", "G280", "G200", "G150",
        # Ultra-long
        "GLF6", "GLF5", "GLF4", "GA5C", "GA6C", "G550", "G650", "G700", "G450",
        "GLEX", "GL5T", "GL7T", "FA7X", "FA8X", "F900",
        # Turboprop
        "PC12", "PC24", "TBM7", "TBM8", "TBM9", "BE20", "BE9L",
    }
    legs = []
    now = dt.datetime.utcnow()
    today = now.date().isoformat()
    time_str = now.strftime("%H:%M")
    seen_hex = set()
    for label, lat, lon, dist in queries:
        try:
            r = requests.get(
                f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}",
                timeout=15,
                headers={"User-Agent": "JETLUXECO/1.0"},
            )
            if r.status_code != 200:
                continue
            ac_list = (r.json() or {}).get("ac") or []
        except Exception as e:
            print(f"  adsb-live {label}: {e}")
            continue
        for ac in ac_list:
            type_code = (ac.get("t") or "").upper()
            if type_code not in BIZ_JET_TYPES:
                continue
            hex_code = (ac.get("hex") or "").lower()
            if not hex_code or hex_code in seen_hex:
                continue
            seen_hex.add(hex_code)
            tail = (ac.get("r") or "").strip()
            # Only AIRBORNE entries — skip ones at gate
            alt = ac.get("alt_baro")
            if alt == "ground" or (isinstance(alt, (int, float)) and alt < 100):
                continue
            legs.append({
                "source": source,
                "operator": tail or hex_code,
                # Placeholder origin/destination — enrichment will fill in real ones
                "origin": "AIRBORNE",
                "origin_city": tail,
                "destination": "EN ROUTE",
                "destination_city": (
                    f"{int(alt) if isinstance(alt,(int,float)) else 0} ft, "
                    f"{int(ac.get('gs',0))} kt, {type_code}"
                ),
                "depart_date": today,
                "depart_time": time_str,
                "aircraft": type_code,
                "tail_number": tail or None,
                "icao24": hex_code,
                "url": f"https://globe.adsb.lol/?icao={hex_code}",
                "raw": json.dumps({
                    "hex": hex_code, "r": tail, "t": type_code,
                    "flight": ac.get("flight", "").strip(),
                    "alt_baro": alt, "gs": ac.get("gs"),
                    "track": ac.get("track"), "lat": ac.get("lat"), "lon": ac.get("lon"),
                }),
            })
    return legs


def scrape_paramount():
    """Paramount Business Jets — try public listings."""
    source = "paramount"
    candidates = [
        "https://www.paramountbusinessjets.com/empty-legs.html",
        "https://www.paramountbusinessjets.com/empty-legs/",
        "https://www.paramountbusinessjets.com/private-jet-empty-leg-flights.html",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            legs = []
            for row in soup.select("tr, .empty-leg, [class*=leg]"):
                text = row.get_text(" ", strip=True)
                icaos = re.findall(r"\b[KE][A-Z]{3}\b", text)
                date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
                if len(icaos) >= 2 and date_match and _norm_date(date_match.group(1)):
                    legs.append({
                        "source": source,
                        "operator": "Paramount Business Jets",
                        "origin": icaos[0],
                        "destination": icaos[1],
                        "depart_date": _norm_date(date_match.group(1)),
                        "url": url,
                        "raw": text[:500],
                    })
            if legs:
                return legs
        except Exception:
            continue
    return []


# --------------------------------------------------------------------------
# Real-time live aviation data — OpenSky Network
# --------------------------------------------------------------------------

# Continental US + Caribbean — captures real Florida-relevant traffic
LIVE_BBOX = {
    "lamin": 15.0,   # south (covers Caribbean)
    "lomin": -130.0, # west (covers all CONUS)
    "lamax": 50.0,   # north
    "lomax": -65.0,  # east
}

# Known commercial / airline callsign prefixes — we EXCLUDE these to find biz-jets
AIRLINE_CALLSIGN_PREFIXES = (
    "AAL", "DAL", "UAL", "SWA", "JBU", "ASA", "SKW", "RPA", "ENY", "FFT",
    "NKS", "SCX", "FDX", "UPS", "GTI", "ATN", "ABX", "GEC", "BAW", "AFR",
    "DLH", "KLM", "AFL", "ACA", "CES", "CSN", "ANA", "JAL", "QFA", "EZY",
    "RYR", "TVF", "VIR", "EIN", "IBE", "AZA", "SAS", "FIN", "AUA",
    "JST", "MAS", "SVA", "ETD", "UAE", "QTR", "SIA", "CPA", "EVA", "CAL",
    "KAL", "OZ", "AAR", "VOI", "AM", "CMP", "AVA", "TAM", "PAA",
)

def scrape_opensky_live():
    """Pulls live aircraft positions from OpenSky free API.

    Returns flights currently airborne over the US + Caribbean that look like
    business jets (small, fast, not airline callsigns). These represent real
    private-aviation traffic in motion — a sourcing signal for empty legs.

    Filtering logic:
      - Altitude > 3,000m (above weather, real cruise)
      - Speed > 300 kt (cruise speed of a biz-jet)
      - Not on ground
      - Callsign NOT in airline prefix list
      - Callsign looks like N-reg or a private op prefix

    Outputs a "live snapshot" row per aircraft — searchable in your dashboard.
    """
    source = "opensky-live"
    bbox = LIVE_BBOX
    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={bbox['lamin']}&lomin={bbox['lomin']}"
        f"&lamax={bbox['lamax']}&lomax={bbox['lomax']}"
    )
    try:
        # Use a more browser-like UA to avoid OpenSky blocking cloud IPs
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            # Raise so the error is captured in scrape_log
            raise Exception(f"OpenSky HTTP {r.status_code}: {r.text[:200]}")
        data = r.json() or {}
        states = data.get("states") or []
        if not states:
            raise Exception(f"OpenSky returned 0 states (response: {len(r.text)} bytes)")
        legs = []
        today = dt.date.today().isoformat()
        for s in states:
            # state vector: 0=icao24, 1=callsign, 2=country, 5=lon, 6=lat,
            # 7=baro_alt(m), 8=on_ground, 9=velocity(m/s), 10=heading
            icao24 = (s[0] or "").strip()
            callsign = (s[1] or "").strip()
            country = s[2] or ""
            on_ground = s[8]
            altitude_m = s[7]
            velocity_ms = s[9]
            heading = s[10]

            if on_ground:
                continue
            if not callsign or altitude_m is None or velocity_ms is None:
                continue
            if altitude_m < 3000:        # below 9,800 ft = mostly GA / commuter
                continue
            velocity_kt = velocity_ms * 1.94384
            if velocity_kt < 300:        # under 300 kt = unlikely biz-jet cruise
                continue

            # Skip airline callsigns
            cs = callsign.upper()
            if cs[:3] in AIRLINE_CALLSIGN_PREFIXES:
                continue
            # Skip if callsign starts with numbers only (flight numbers)
            if cs[:2].isdigit():
                continue

            # Build a snapshot row
            legs.append({
                "source": source,
                "operator": cs,
                "origin": "AIRBORNE",
                "origin_city": country,
                "destination": "EN ROUTE",
                "destination_city": (
                    f"alt {int(altitude_m * 3.28084)} ft, "
                    f"{int(velocity_kt)} kt, hdg {int(heading or 0)}°"
                ),
                "depart_date": today,
                "depart_time": dt.datetime.utcnow().strftime("%H:%M"),
                "aircraft": cs,
                "category": None,
                "url": f"https://opensky-network.org/aircraft-profile?icao24={icao24}",
                "raw": json.dumps({"callsign": cs, "icao24": icao24, "alt_m": altitude_m, "kt": int(velocity_kt)}),
            })
        # Sort by altitude desc (higher = more likely real biz-jet)
        legs.sort(key=lambda l: -int(json.loads(l["raw"]).get("alt_m") or 0))
        return legs[:40]  # cap so DB doesn't flood
    except Exception as e:
        # Re-raise so the orchestrator stores the error in scrape_log
        raise


# --------------------------------------------------------------------------
# JSON-LD harvesting helper
# --------------------------------------------------------------------------

def _harvest_json_ld(node, legs, source, operator, url):
    """Recursively find schema.org Flight/Offer entries."""
    if isinstance(node, dict):
        node_type = node.get("@type", "")
        if isinstance(node_type, list):
            node_type = node_type[0] if node_type else ""
        if node_type in ("Flight", "Trip", "Offer"):
            origin = (node.get("departureAirport") or {}).get("iataCode") or (node.get("departureAirport") or {}).get("name")
            destination = (node.get("arrivalAirport") or {}).get("iataCode") or (node.get("arrivalAirport") or {}).get("name")
            depart = node.get("departureTime") or node.get("startDate")
            date = None
            if depart:
                date = _norm_date(depart[:10]) or _norm_date(depart)
            if origin and destination and date:
                legs.append({
                    "source": source,
                    "operator": operator,
                    "origin": origin.upper()[:4],
                    "destination": destination.upper()[:4],
                    "depart_date": date,
                    "url": url,
                    "raw": json.dumps(node)[:500],
                })
        for v in node.values():
            _harvest_json_ld(v, legs, source, operator, url)
    elif isinstance(node, list):
        for item in node:
            _harvest_json_ld(item, legs, source, operator, url)


def _harvest_next_data(node, legs, source, operator, url):
    """Walk Next.js __NEXT_DATA__ looking for empty-leg-shaped objects."""
    if isinstance(node, dict):
        # Heuristic: an object with origin/destination/departureDate is a flight
        keys = set(node.keys())
        if ({"origin", "destination", "departureDate"} <= keys or
            {"from", "to", "date"} <= keys):
            origin = node.get("origin") or node.get("from")
            destination = node.get("destination") or node.get("to")
            depart = node.get("departureDate") or node.get("date")
            if origin and destination and depart:
                legs.append({
                    "source": source,
                    "operator": operator,
                    "origin": str(origin).upper()[:4],
                    "destination": str(destination).upper()[:4],
                    "depart_date": _norm_date(str(depart)[:10]),
                    "aircraft": node.get("aircraft") or node.get("aircraftType"),
                    "price_usd": _norm_price(node.get("price") or node.get("amount")),
                    "url": url,
                    "raw": json.dumps(node)[:500],
                })
        for v in node.values():
            _harvest_next_data(v, legs, source, operator, url)
    elif isinstance(node, list):
        for item in node:
            _harvest_next_data(item, legs, source, operator, url)


# --------------------------------------------------------------------------
# Manual entry — paste empty legs from operator emails
# --------------------------------------------------------------------------

def add_manual_leg(payload: dict) -> dict:
    """Insert a manually-entered empty leg. Validates and normalizes the payload.

    Required fields: origin, destination, depart_date
    Optional: operator, aircraft, depart_time, pax, price_usd, url, source

    Returns the leg dict that was inserted (with 'status' added).
    """
    required = ["origin", "destination", "depart_date"]
    for f in required:
        if not payload.get(f):
            raise ValueError(f"Missing required field: {f}")

    leg = {
        "source": payload.get("source") or "manual",
        "operator": payload.get("operator"),
        "origin": str(payload["origin"]).upper(),
        "origin_city": payload.get("origin_city"),
        "destination": str(payload["destination"]).upper(),
        "destination_city": payload.get("destination_city"),
        "depart_date": _norm_date(payload["depart_date"]) or payload["depart_date"],
        "depart_time": payload.get("depart_time"),
        "aircraft": payload.get("aircraft"),
        "category": payload.get("category") or _categorize(payload.get("aircraft")),
        "pax": int(payload["pax"]) if payload.get("pax") else None,
        "price_usd": _norm_price(payload.get("price_usd")),
        "url": payload.get("url"),
        "raw": json.dumps(payload)[:500],
    }
    status = db.upsert_leg(leg)
    leg["status"] = status
    return leg


# --------------------------------------------------------------------------
# Sample data — seeds the DB on first run so the dashboard demonstrates
# --------------------------------------------------------------------------

SAMPLE_LEGS = [
    {"source": "sample", "operator": "Solairus Aviation", "origin": "KOPF",
     "origin_city": "Miami-Opa Locka, FL", "destination": "KTEB",
     "destination_city": "Teterboro, NJ", "depart_date": "2026-05-22",
     "depart_time": "10:30", "aircraft": "Challenger 350", "category": "super-midsize",
     "pax": 8, "price_usd": 18500.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Clay Lacy Aviation", "origin": "KFLL",
     "origin_city": "Fort Lauderdale, FL", "destination": "KASE",
     "destination_city": "Aspen, CO", "depart_date": "2026-05-24",
     "depart_time": "07:00", "aircraft": "Gulfstream G650", "category": "ultra-long-range",
     "pax": 14, "price_usd": 42000.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Banyan Air Service", "origin": "KFXE",
     "origin_city": "Fort Lauderdale Exec, FL", "destination": "KPBI",
     "destination_city": "West Palm Beach, FL", "depart_date": "2026-05-20",
     "depart_time": "16:15", "aircraft": "Citation CJ3", "category": "light",
     "pax": 7, "price_usd": 4200.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Jet Linx", "origin": "KMIA",
     "origin_city": "Miami, FL", "destination": "KDAL",
     "destination_city": "Dallas Love, TX", "depart_date": "2026-05-26",
     "depart_time": "09:00", "aircraft": "Phenom 300", "category": "midsize",
     "pax": 8, "price_usd": 14500.0, "url": "https://example.com"},
    {"source": "sample", "operator": "XO", "origin": "KTEB",
     "origin_city": "Teterboro, NJ", "destination": "KOPF",
     "destination_city": "Miami-Opa Locka, FL", "depart_date": "2026-05-21",
     "depart_time": "13:00", "aircraft": "Challenger 605", "category": "heavy",
     "pax": 9, "price_usd": 22500.0, "url": "https://example.com"},
    {"source": "sample", "operator": "VistaJet", "origin": "EGGW",
     "origin_city": "London Luton, UK", "destination": "LFPB",
     "destination_city": "Paris-Le Bourget, FR", "depart_date": "2026-05-23",
     "depart_time": "11:30", "aircraft": "Global 7500", "category": "ultra-long-range",
     "pax": 14, "price_usd": 28000.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Solairus Aviation", "origin": "KOPF",
     "origin_city": "Miami-Opa Locka, FL", "destination": "MUVR",
     "destination_city": "Varadero, Cuba", "depart_date": "2026-05-25",
     "depart_time": "14:00", "aircraft": "Citation XLS+", "category": "midsize",
     "pax": 8, "price_usd": 9800.0, "url": "https://example.com"},
    {"source": "sample", "operator": "JetEdge", "origin": "KLAS",
     "origin_city": "Las Vegas, NV", "destination": "KFLL",
     "destination_city": "Fort Lauderdale, FL", "depart_date": "2026-05-27",
     "depart_time": "08:30", "aircraft": "Falcon 7X", "category": "ultra-long-range",
     "pax": 12, "price_usd": 31500.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Wheels Up", "origin": "KFLL",
     "origin_city": "Fort Lauderdale, FL", "destination": "MYNN",
     "destination_city": "Nassau, Bahamas", "depart_date": "2026-05-19",
     "depart_time": "12:00", "aircraft": "Citation Excel", "category": "midsize",
     "pax": 7, "price_usd": 7900.0, "url": "https://example.com"},
    {"source": "sample", "operator": "Magellan Jets", "origin": "KOPF",
     "origin_city": "Miami-Opa Locka, FL", "destination": "KBOS",
     "destination_city": "Boston Logan, MA", "depart_date": "2026-05-28",
     "depart_time": "06:45", "aircraft": "Challenger 350", "category": "super-midsize",
     "pax": 9, "price_usd": 19200.0, "url": "https://example.com"},
]


def seed_sample():
    """Insert sample data so the dashboard isn't empty on first run."""
    new_count = 0
    for leg in SAMPLE_LEGS:
        if db.upsert_leg(leg) == "new":
            new_count += 1
    db.log_scrape("sample", new_count, 0)
    return new_count


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

SCRAPERS = [
    ("adsb-live", scrape_adsb_live),
    ("evojets", scrape_evojets),
    ("aslgroup", scrape_aslgroup),
    ("emptyjet", scrape_emptyjet_sitemap),  # ~500 legs/run from EmptyJet aggregator
    ("magellan", scrape_magellan),
    ("privatefly", scrape_privatefly),
    ("jettly", scrape_jettly),
    ("stratos", scrape_stratos),
    ("paramount", scrape_paramount),
]


def run_all():
    """Run every scraper, store results, log outcomes."""
    db.init_db()
    results = {}
    for name, fn in SCRAPERS:
        new_count = 0
        updated_count = 0
        err = None
        try:
            legs = fn() or []
            for leg in legs:
                if not leg.get("depart_date") or not leg.get("origin") or not leg.get("destination"):
                    continue
                if not leg.get("category") and leg.get("aircraft"):
                    leg["category"] = _categorize(leg["aircraft"])
                status = db.upsert_leg(leg)
                if status == "new":
                    new_count += 1
                elif status == "updated":
                    updated_count += 1
        except Exception as e:
            err = str(e)
        db.log_scrape(name, new_count, updated_count, err)
        results[name] = {"new": new_count, "updated": updated_count, "error": err}
    db.mark_stale_legs(hours=72)
    return results
