"""
PlaneSpotters.net enrichment — resolves tail number → real aircraft photo + operator.

For any leg with a tail number (typically ADSB-LIVE rows), we can look up:
  - Real photo of THAT specific aircraft (not a generic model shot)
  - Operator/airline name (parsed from PlaneSpotters slug)
  - Aircraft variant (G650ER vs G650, CJ3+ vs CJ3, etc.)

API: https://www.planespotters.net/photo/api (free, requires contact UA)
Rate limit: gentle — cache aggressively, refresh weekly per tail.
"""
from __future__ import annotations
import os
import re
import sys
import json
import time
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests
import psycopg2
import psycopg2.extras

UA = "JETLUXECO/1.0 (+https://jetluxeco.com/contact)"
API_BASE = "https://api.planespotters.net/pub/photos/reg"
CACHE_TTL_DAYS = 30  # aircraft photos don't change often


def _clean_tail(tail: str) -> str | None:
    if not tail:
        return None
    t = tail.strip().upper().replace(" ", "").replace("-", "")
    # Basic sanity: 3-8 alphanumeric chars, starts with letter
    if not re.match(r"^[A-Z][A-Z0-9]{2,7}$", t):
        return None
    return t


def fetch_photo(tail: str) -> dict | None:
    """Look up a tail on PlaneSpotters. Returns dict or None."""
    t = _clean_tail(tail)
    if not t:
        return None
    try:
        r = requests.get(
            f"{API_BASE}/{t}",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        photos = data.get("photos") or []
        if not photos:
            return None
        p = photos[0]  # Most-liked/most-recent photo first
        # Parse operator from URL slug: "n654fx-flexjet-gulfstream-g650er-gvi"
        link = p.get("link", "")
        m = re.search(r"/photo/\d+/([a-z0-9-]+)", link)
        operator_slug = None
        aircraft_variant = None
        if m:
            parts = m.group(1).split("-")
            if len(parts) >= 3 and parts[0].lower() == t.lower():
                # After tail comes operator, then aircraft manufacturer + model
                # Common patterns:
                #  n654fx-flexjet-gulfstream-g650er-gvi  → operator=flexjet, ac=gulfstream g650er
                #  n1a-privatecharter-cessna-citation-cj3  → operator=privatecharter
                # Heuristic: operator is the tokens between tail and known manufacturer name
                known_mfrs = {"gulfstream","cessna","bombardier","dassault","embraer",
                              "beechcraft","learjet","hawker","pilatus","boeing","airbus",
                              "cirrus","piaggio","honda","hondajet","ilyushin"}
                mfr_idx = None
                for i, tok in enumerate(parts[1:], start=1):
                    if tok.lower() in known_mfrs:
                        mfr_idx = i
                        break
                if mfr_idx and mfr_idx > 1:
                    operator_slug = "-".join(parts[1:mfr_idx])
                    aircraft_variant = "-".join(parts[mfr_idx:])
                elif len(parts) >= 3:
                    operator_slug = parts[1]
                    aircraft_variant = "-".join(parts[2:])

        # Pretty-cased operator name
        operator_name = None
        if operator_slug:
            operator_name = " ".join(w.capitalize() for w in operator_slug.split("-"))

        return {
            "photo_url": (p.get("thumbnail_large") or {}).get("src"),
            "photo_thumb": (p.get("thumbnail") or {}).get("src"),
            "photo_link": link,
            "photographer": p.get("photographer"),
            "operator_name": operator_name,
            "aircraft_variant": aircraft_variant,
        }
    except Exception:
        return None


def enrich_legs_bulk(conn, tails: list[str]) -> dict:
    """Enrich a batch of tails in parallel. Returns {tail: enrichment_dict}."""
    tails = list({_clean_tail(t) for t in tails if _clean_tail(t)})
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_photo, tails))
    return {t: r for t, r in zip(tails, results) if r}


def run_enrichment(dbase_url: str):
    """Main runner — enrich every leg that has a tail but no photo yet."""
    conn = psycopg2.connect(dbase_url)
    try:
        # Find tails needing enrichment
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT tail_number, operator
                FROM legs
                WHERE active=TRUE
                  AND (image_url IS NULL OR image_url = '')
                  AND (tail_number IS NOT NULL OR operator ~ '^N[0-9]')
                LIMIT 60
            """)
            rows = cur.fetchall()

        tails = []
        for r in rows:
            t = r.get("tail_number") or r.get("operator")
            if _clean_tail(t):
                tails.append(_clean_tail(t))

        print(f"Enriching {len(tails)} tails...", flush=True)
        results = enrich_legs_bulk(conn, tails)
        print(f"Got photos for {len(results)}/{len(tails)}", flush=True)

        # Write back — match by tail_number OR operator (for ADSB rows where operator=tail)
        with conn.cursor() as cur:
            for tail, enr in results.items():
                cur.execute("""
                    UPDATE legs SET
                      image_url = COALESCE(%s, image_url),
                      operator = CASE
                        WHEN operator ~ '^N[0-9]' AND %s IS NOT NULL THEN %s
                        WHEN operator IS NULL AND %s IS NOT NULL THEN %s
                        ELSE operator
                      END,
                      tail_number = COALESCE(tail_number, %s),
                      aircraft = COALESCE(aircraft, %s)
                    WHERE (tail_number = %s OR operator = %s) AND active = TRUE
                """, (
                    enr.get("photo_url"),
                    enr.get("operator_name"), enr.get("operator_name"),
                    enr.get("operator_name"), enr.get("operator_name"),
                    tail,
                    enr.get("aircraft_variant"),
                    tail, tail,
                ))
            conn.commit()

        # Report sample
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT tail_number, operator, aircraft, image_url
                FROM legs
                WHERE tail_number = ANY(%s) OR operator = ANY(%s)
                LIMIT 5
            """, (list(results.keys()), list(results.keys())))
            for r in cur.fetchall():
                print(f"  {r['tail_number'] or r['operator']}: op={r['operator']} ac={r['aircraft']} photo={'YES' if r['image_url'] else 'no'}", flush=True)

        return {"enriched": len(results), "attempted": len(tails)}
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].upper().startswith("N"):
        # Single-tail smoke test
        result = fetch_photo(sys.argv[1])
        print(json.dumps(result, indent=2))
    else:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("Set DATABASE_URL env var", file=sys.stderr)
            sys.exit(1)
        result = run_enrichment(db_url)
        print(f"\nDone: {result}")
