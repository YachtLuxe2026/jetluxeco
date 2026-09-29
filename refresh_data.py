"""
Refresh index.html with fresh leg data from Supabase.

Runs in GitHub Actions. Reads DATABASE_URL from environment,
pulls the top 500 active legs, and bakes them into index.html.
"""
import os
import re
import json
import psycopg2
import csv
import urllib.request

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit("DATABASE_URL not set")

# Download OurAirports for coordinate lookup
print("Downloading airport data...")
urllib.request.urlretrieve(
    "https://davidmegginson.github.io/ourairports-data/airports.csv",
    "/tmp/airports.csv",
)

# Pull legs
print("Pulling legs from Supabase...")
conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("""
    SELECT origin, origin_city, destination, destination_city,
           depart_date::text, depart_time, aircraft, category, pax, price_usd,
           source, operator, operator_phone, operator_email, operator_home_base,
           url, image_url, tail_number, flight_phase,
           inferred_operator, inferred_confidence, inferred_phone, inferred_email, inferred_website
    FROM legs
    WHERE active=TRUE AND depart_date >= CURRENT_DATE
      AND source != 'adsb-live'
    ORDER BY depart_date, origin
    LIMIT 2000
""")
legs = []
for r in cur.fetchall():
    legs.append({
        "o": r[0], "oc": r[1], "d": r[2], "dc": r[3],
        "dt": r[4], "tm": r[5], "ac": r[6], "cat": r[7],
        "px": r[8], "p": float(r[9]) if r[9] else None,
        "src": r[10], "op": r[11], "ph": r[12], "em": r[13], "hb": r[14],
        "url": r[15], "img": r[16], "tail": r[17], "phase": r[18],
        "iop": r[19], "iconf": r[20], "iph": r[21], "iem": r[22], "iws": r[23],
    })
print(f"Legs pulled: {len(legs)}")

# Get airport coords
icaos = set()
for l in legs:
    if l["o"]: icaos.add(l["o"])
    if l["d"]: icaos.add(l["d"])
coords = {}
with open("/tmp/airports.csv") as f:
    for row in csv.DictReader(f):
        ident = (row.get("ident") or "").strip().upper()
        if ident in icaos:
            try:
                coords[ident] = {
                    "lat": float(row["latitude_deg"]),
                    "lon": float(row["longitude_deg"]),
                    "name": (row.get("name") or "").strip(),
                    "city": (row.get("municipality") or "").strip(),
                    "country": (row.get("iso_country") or "").strip(),
                }
            except:
                pass
print(f"Airport coords: {len(coords)}/{len(icaos)}")

# Read current index.html, replace the data
html = open("index.html").read()

def replace_after(html, prefix, new_json):
    start = html.find(prefix)
    if start < 0: return html
    end = html.find(";", start)
    return html[:start] + prefix + new_json + html[end:]

html = replace_after(html, "window.__DATA__ = ", json.dumps(legs))
html = replace_after(html, "window.__COORDS__ = ", json.dumps(coords))

open("index.html", "w").write(html)
print(f"index.html rewritten: {len(html):,} bytes")
conn.close()
