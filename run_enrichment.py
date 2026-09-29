"""
Enrichment runner for ADSB-LIVE legs.

For each active ADSB row in the legs table:
  1. Look up icao24 (cached if we have it, otherwise resolve from tail)
  2. Fetch trace, derive takeoff airport, current phase, predicted destination, ETA
  3. Write enriched fields back to the legs row
  4. Mark legs as inactive if trace is unavailable (aircraft no longer broadcasting)

Run frequency: every scrape cycle (2 hours) — fast (~1-2s per leg).
"""
import os
import sys
import json
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
import psycopg2.extras

import adsb_enrich

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("FATAL: DATABASE_URL env var not set", file=sys.stderr)
    sys.exit(1)


def run_inline() -> dict:
    """Callable from app.py / Flask. Returns summary dict."""
    return _run()


def main():
    summary = _run()
    print(f"\n{'='*50}")
    print(f"Enriched: {summary['enriched']}")
    print(f"Failed:   {summary['failed']}")
    print(f"Deactivated (no longer broadcasting): {summary['deactivated']}")


def _run() -> dict:
    conn = psycopg2.connect(DATABASE_URL)
    enriched = 0
    failed = 0
    deactivated = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, operator, origin_city, destination_city, aircraft, icao24
                FROM legs
                WHERE source='adsb-live' AND active=TRUE
                ORDER BY last_seen DESC
            """)
            rows = cur.fetchall()

        print(f"Found {len(rows)} active ADSB legs to enrich")
        # Warm the airports cache before going parallel (so threads share it)
        adsb_enrich._airports()

        def _process(row):
            tail = (row["operator"] or row["origin_city"] or "").strip()
            try:
                if row.get("icao24"):
                    trace = adsb_enrich.fetch_trace(row["icao24"])
                    if not trace:
                        return row, None
                    analysis = adsb_enrich.analyze_trace(trace)
                    result = _build_result(trace, analysis, row["icao24"], tail)
                else:
                    result = adsb_enrich.enrich_one(tail)
                return row, result
            except Exception as e:
                print(f"  {tail}: ERR {e}", file=sys.stderr)
                return row, None

        # Process with thread pool (network IO bound)
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = [ex.submit(_process, row) for row in rows]
            results = []
            for fut in as_completed(futures):
                results.append(fut.result())

        for row, result in results:
            tail = (row["operator"] or row["origin_city"] or "").strip()
            if not result:
                # No trace → aircraft no longer broadcasting, mark inactive
                with conn.cursor() as upd:
                    upd.execute(
                        "UPDATE legs SET active=FALSE WHERE id=%s",
                        (row["id"],),
                    )
                deactivated += 1
                failed += 1
                continue

            # Write enrichment fields
            with conn.cursor() as upd:
                upd.execute("""
                    UPDATE legs SET
                        icao24 = %s,
                        flight_phase = %s,
                        current_alt_ft = %s,
                        current_gs_kt = %s,
                        current_descent_fpm = %s,
                        takeoff_time_utc = %s,
                        eta_utc = %s,
                        predicted_dest_icao = %s,
                        predicted_dest_city = %s,
                        distance_to_dest_nm = %s,
                        origin = COALESCE(%s, origin),
                        origin_city = COALESCE(%s, origin_city),
                        destination = COALESCE(%s, destination),
                        destination_city = COALESCE(%s, destination_city),
                        aircraft = COALESCE(%s, aircraft),
                        last_enriched_at = %s,
                        last_seen = %s,
                        active = TRUE
                    WHERE id = %s
                """, (
                    result.get("icao24"),
                    result.get("phase"),
                    int(result["current_alt_ft"]) if result.get("current_alt_ft") else None,
                    int(result["current_gs_kt"]) if result.get("current_gs_kt") else None,
                    int(result["current_descent_fpm"]) if result.get("current_descent_fpm") else None,
                    result.get("takeoff_time_utc"),
                    result.get("eta_utc"),
                    result.get("predicted_dest_icao"),
                    result.get("predicted_dest_city"),
                    int(result["distance_to_dest_nm"]) if result.get("distance_to_dest_nm") else None,
                    result.get("takeoff_icao"),
                    result.get("takeoff_city"),
                    result.get("predicted_dest_icao"),
                    result.get("predicted_dest_city"),
                    result.get("aircraft_type"),
                    dt.datetime.now(dt.timezone.utc),
                    dt.datetime.now(dt.timezone.utc),
                    row["id"],
                ))
            enriched += 1
            phase = result.get("phase")
            takeoff = result.get("takeoff_icao") or "?"
            dest = result.get("predicted_dest_icao") or "?"
            eta = result.get("eta_minutes") or "?"
            print(f"  {tail} ({result.get('aircraft_type')}): {takeoff} → {dest} | {phase} | ETA {eta}m")

        conn.commit()
    finally:
        conn.close()

    return {"enriched": enriched, "failed": failed, "deactivated": deactivated, "total": len(rows)}


def _build_result(trace, analysis, hex_code, tail):
    """Replicate enrich_one's result building when we already have the trace + analysis."""
    result = {
        "tail": tail,
        "icao24": hex_code,
        "aircraft_type": trace.get("t"),
        "phase": analysis.get("phase"),
        "current_alt_ft": analysis.get("current_alt_ft"),
        "current_gs_kt": analysis.get("current_gs_kt"),
        "current_descent_fpm": analysis.get("current_descent_fpm"),
    }
    if "takeoff_lat" in analysis:
        ap, d = adsb_enrich.nearest_airport(
            analysis["takeoff_lat"], analysis["takeoff_lon"], max_nm=10.0,
        )
        if ap:
            result["takeoff_icao"] = ap["icao"]
            result["takeoff_city"] = ap["city"]
            result["takeoff_time_utc"] = analysis.get("takeoff_time_utc")

    if analysis.get("phase") != "LANDED" and analysis.get("current_track") is not None:
        dest_ap, dist_nm = adsb_enrich.project_destination(
            analysis["current_lat"], analysis["current_lon"],
            analysis["current_track"], analysis["current_gs_kt"],
            analysis.get("current_alt_ft"), analysis.get("current_descent_fpm"),
        )
        if dest_ap:
            result["predicted_dest_icao"] = dest_ap["icao"]
            result["predicted_dest_city"] = dest_ap["city"]
            result["distance_to_dest_nm"] = round(dist_nm, 1) if dist_nm else None
            if analysis.get("current_gs_kt") and analysis["current_gs_kt"] > 50:
                hours = dist_nm / analysis["current_gs_kt"]
                result["eta_utc"] = (
                    dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(hours=hours)
                ).isoformat()
                result["eta_minutes"] = round(hours * 60)
    return result


if __name__ == "__main__":
    main()


# Compatibility for callers that imported _build_result
__all__ = ["main", "run_inline", "_build_result"]
