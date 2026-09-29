"""Postgres (Supabase) layer for JETLUXECO.

Set DATABASE_URL env var to your Supabase pooler connection string.
"""
import os
import hashlib
import datetime as dt
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from aircraft_photos import aircraft_image_url

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS legs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    operator        TEXT,
    operator_phone  TEXT,
    operator_email  TEXT,
    operator_home_base TEXT,
    origin          TEXT NOT NULL,
    origin_city     TEXT,
    destination     TEXT NOT NULL,
    destination_city TEXT,
    depart_date     DATE NOT NULL,
    depart_time     TEXT,
    aircraft        TEXT,
    tail_number     TEXT,
    image_url       TEXT,
    category        TEXT,
    pax             INTEGER,
    price_usd       DOUBLE PRECISION,
    currency        TEXT DEFAULT 'USD',
    url             TEXT,
    raw             TEXT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    active          BOOLEAN DEFAULT TRUE
);

-- Migrate older deployments
ALTER TABLE legs ADD COLUMN IF NOT EXISTS operator_phone TEXT;
ALTER TABLE legs ADD COLUMN IF NOT EXISTS operator_email TEXT;
ALTER TABLE legs ADD COLUMN IF NOT EXISTS operator_home_base TEXT;
ALTER TABLE legs ADD COLUMN IF NOT EXISTS tail_number TEXT;
ALTER TABLE legs ADD COLUMN IF NOT EXISTS image_url TEXT;

CREATE INDEX IF NOT EXISTS idx_legs_depart ON legs(depart_date);
CREATE INDEX IF NOT EXISTS idx_legs_origin ON legs(origin);
CREATE INDEX IF NOT EXISTS idx_legs_destination ON legs(destination);
CREATE INDEX IF NOT EXISTS idx_legs_source ON legs(source);

CREATE TABLE IF NOT EXISTS scrape_log (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    ran_at      TIMESTAMPTZ NOT NULL,
    new_count   INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS operators (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    home_base       TEXT,
    home_base_city  TEXT,
    region          TEXT,
    phone           TEXT,
    email           TEXT,
    website         TEXT,
    fleet_notes     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_operators_region ON operators(region);
CREATE INDEX IF NOT EXISTS idx_operators_home_base ON operators(home_base);
"""


@contextmanager
def conn():
    c = psycopg2.connect(DATABASE_URL)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(SCHEMA)


def _hash_leg(source, origin, destination, depart_date, aircraft):
    key = f"{source}|{origin}|{destination}|{depart_date}|{aircraft or ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _operator_lookup(c, source, operator_name):
    """Match an operator from the directory by source slug or fuzzy name."""
    keys = [source, operator_name]
    # Build candidate names with case-insensitive lookup
    candidates = []
    for k in keys:
        if not k:
            continue
        k = str(k).strip()
        candidates.append(k)
        candidates.append(k.replace("-", " "))
        candidates.append(k.title())
    with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Try exact case-insensitive match
        for cand in candidates:
            cur.execute("SELECT * FROM operators WHERE LOWER(name) = LOWER(%s) LIMIT 1", (cand,))
            row = cur.fetchone()
            if row:
                return dict(row)
        # Try substring/contains match
        for cand in candidates:
            cur.execute(
                "SELECT * FROM operators WHERE LOWER(name) LIKE LOWER(%s) ORDER BY length(name) ASC LIMIT 1",
                (f"%{cand}%",),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
    return None


def upsert_leg(leg: dict):
    now = dt.datetime.now(dt.timezone.utc)
    leg_id = _hash_leg(
        leg["source"],
        leg["origin"],
        leg["destination"],
        leg["depart_date"],
        leg.get("aircraft"),
    )

    # Enrich with operator directory + aircraft photo
    image_url = leg.get("image_url") or aircraft_image_url(leg.get("aircraft"))

    with conn() as c:
        # Look up operator contact info
        op = _operator_lookup(c, leg.get("source"), leg.get("operator"))
        op_phone = leg.get("operator_phone") or (op["phone"] if op else None)
        op_email = leg.get("operator_email") or (op["email"] if op else None)
        op_base = leg.get("operator_home_base") or (op["home_base"] if op else None)
        # If we matched an operator from the directory, prefer its canonical name
        operator_canon = leg.get("operator") or (op["name"] if op else None)

        with c.cursor() as cur:
            cur.execute("SELECT id FROM legs WHERE id = %s", (leg_id,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """UPDATE legs SET
                        last_seen = %s,
                        price_usd = COALESCE(%s, price_usd),
                        operator = COALESCE(%s, operator),
                        operator_phone = COALESCE(%s, operator_phone),
                        operator_email = COALESCE(%s, operator_email),
                        operator_home_base = COALESCE(%s, operator_home_base),
                        pax = COALESCE(%s, pax),
                        tail_number = COALESCE(%s, tail_number),
                        image_url = COALESCE(%s, image_url),
                        active = TRUE
                       WHERE id = %s""",
                    (now, leg.get("price_usd"), operator_canon, op_phone, op_email, op_base,
                     leg.get("pax"), leg.get("tail_number"), image_url, leg_id),
                )
                return "updated"
            else:
                cur.execute(
                    """INSERT INTO legs (
                        id, source, operator, operator_phone, operator_email, operator_home_base,
                        origin, origin_city, destination, destination_city,
                        depart_date, depart_time, aircraft, tail_number, image_url, category, pax,
                        price_usd, currency, url, raw, first_seen, last_seen, active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)""",
                    (
                        leg_id, leg["source"], operator_canon, op_phone, op_email, op_base,
                        leg["origin"].upper(), leg.get("origin_city"),
                        leg["destination"].upper(), leg.get("destination_city"),
                        leg["depart_date"], leg.get("depart_time"),
                        leg.get("aircraft"), leg.get("tail_number"), image_url,
                        leg.get("category"), leg.get("pax"), leg.get("price_usd"),
                        leg.get("currency", "USD"), leg.get("url"), leg.get("raw"),
                        now, now,
                    ),
                )
                return "new"


def log_scrape(source, new_count, updated_count, error=None):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO scrape_log (source, ran_at, new_count, updated_count, error) VALUES (%s, %s, %s, %s, %s)",
                (source, dt.datetime.now(dt.timezone.utc), new_count, updated_count, error),
            )


def mark_stale_legs(hours=72):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE legs SET active = FALSE WHERE last_seen < %s AND active = TRUE", (cutoff,))


def query_legs(origin=None, destination=None, date_from=None, date_to=None,
               max_price=None, source=None, aircraft_category=None, limit=500):
    sql = "SELECT * FROM legs WHERE active = TRUE AND depart_date >= CURRENT_DATE - INTERVAL '1 day'"
    args = []
    if origin:
        sql += " AND (origin = %s OR origin_city ILIKE %s)"
        args += [origin, f"%{origin}%"]
    if destination:
        sql += " AND (destination = %s OR destination_city ILIKE %s)"
        args += [destination, f"%{destination}%"]
    if date_from:
        sql += " AND depart_date >= %s"
        args += [date_from]
    if date_to:
        sql += " AND depart_date <= %s"
        args += [date_to]
    if max_price:
        sql += " AND (price_usd IS NULL OR price_usd <= %s)"
        args += [max_price]
    if source:
        sql += " AND source = %s"
        args += [source]
    if aircraft_category:
        sql += " AND category = %s"
        args += [aircraft_category]
    sql += " ORDER BY depart_date ASC LIMIT %s"
    args += [limit]

    with conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
            for r in rows:
                if r.get("depart_date"):
                    r["depart_date"] = r["depart_date"].isoformat()
                for ts_key in ("first_seen", "last_seen"):
                    if r.get(ts_key):
                        r[ts_key] = r[ts_key].isoformat()
            return [dict(r) for r in rows]


def count_legs():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM legs WHERE active = TRUE")
            return cur.fetchone()[0]


def summary_stats():
    with conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM legs WHERE active = TRUE")
            total = cur.fetchone()["n"]
            cur.execute("SELECT source, COUNT(*) AS n FROM legs WHERE active = TRUE GROUP BY source ORDER BY n DESC")
            sources = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) AS n FROM legs WHERE active = TRUE AND depart_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 day'")
            next_7 = cur.fetchone()["n"]
            cur.execute("SELECT source, ran_at, new_count, updated_count FROM scrape_log ORDER BY ran_at DESC LIMIT 10")
            last_scrape = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("ran_at"):
                    d["ran_at"] = d["ran_at"].isoformat()
                last_scrape.append(d)
            return {
                "total_active": total,
                "next_7_days": next_7,
                "by_source": sources,
                "recent_scrapes": last_scrape,
            }


# ─── Operator directory queries ──────────────────────────────────────────

def query_operators(region=None, home_base=None, search=None):
    sql = "SELECT * FROM operators WHERE 1=1"
    args = []
    if region:
        sql += " AND region = %s"
        args.append(region)
    if home_base:
        sql += " AND home_base = %s"
        args.append(home_base)
    if search:
        sql += " AND (name ILIKE %s OR home_base_city ILIKE %s)"
        args += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY region NULLS LAST, home_base NULLS LAST, name"
    with conn() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]


def operator_regions():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT region FROM operators WHERE region IS NOT NULL ORDER BY region")
            return [r[0] for r in cur.fetchall()]


def operator_home_bases():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT home_base, COUNT(*) AS n FROM operators WHERE home_base IS NOT NULL GROUP BY home_base ORDER BY n DESC, home_base"
            )
            return [{"home_base": r[0], "count": r[1]} for r in cur.fetchall()]
