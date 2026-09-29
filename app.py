"""
JETLUXECO web app.

Routes:
  GET  /                  dashboard (empty legs)
  GET  /operators         operator directory (browse / sort by home base)
  POST /login             accepts password
  GET  /logout            clears auth
  GET  /api/legs          JSON empty legs (with filters)
  POST /api/legs/manual   add a leg manually
  GET  /api/operators     JSON operators (with filters)
  GET  /operators/export.csv  CSV download for mass mail merge
  POST /scrape            trigger all scrapers (cron-only)
  GET  /healthz           health check
"""
import os
import csv
import io
import secrets
import datetime as dt
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, abort,
    make_response, redirect, url_for, Response,
)
import scrapers
import db
import aircraft_rates
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

SCRAPE_TOKEN = os.environ.get("SCRAPE_TOKEN", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
AUTH_COOKIE = "jlc_auth"


def _is_authed(req):
    if not AUTH_PASSWORD:
        return True
    return req.cookies.get(AUTH_COOKIE) == AUTH_PASSWORD


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _is_authed(request):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
@login_required
def index():
    return render_template("cockpit.html")


@app.route("/legacy")
@login_required
def legacy_index():
    return render_template("index.html")


@app.route("/cockpit")
@login_required
def cockpit_page():
    return render_template("cockpit.html")


@app.route("/api/airport-coords")
@login_required
def api_airport_coords():
    """Return lat/lon for every airport referenced in the current inventory."""
    import csv
    import os
    coords_path = "/tmp/airports.csv"
    # Download if not cached
    if not os.path.exists(coords_path):
        try:
            r = requests.get("https://davidmegginson.github.io/ourairports-data/airports.csv", timeout=60)
            with open(coords_path, "wb") as f:
                f.write(r.content)
        except Exception:
            return jsonify({"coords": {}})

    # Get all airports in current active inventory
    conn = db._get_connection() if hasattr(db, "_get_connection") else None
    icaos = set()
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get("DATABASE_URL", ""))
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT origin FROM legs WHERE active=TRUE AND depart_date >= CURRENT_DATE
            UNION SELECT DISTINCT destination FROM legs WHERE active=TRUE AND depart_date >= CURRENT_DATE
        """)
        icaos = {r[0] for r in cur.fetchall() if r[0]}
        conn.close()
    except Exception:
        pass

    coords = {}
    try:
        with open(coords_path) as f:
            for row in csv.DictReader(f):
                ident = (row.get("ident") or "").strip().upper()
                if not icaos or ident in icaos:
                    try:
                        coords[ident] = {
                            "lat": float(row["latitude_deg"]),
                            "lon": float(row["longitude_deg"]),
                            "name": (row.get("name") or "").strip(),
                            "city": (row.get("municipality") or "").strip(),
                            "country": (row.get("iso_country") or "").strip(),
                        }
                    except Exception:
                        pass
    except Exception:
        pass
    return jsonify({"coords": coords})


@app.route("/operators")
@login_required
def operators_page():
    return render_template("operators.html")


@app.route("/pricing")
@login_required
def pricing_page():
    return render_template("pricing.html")


@app.route("/api/pricing/rate-card")
@login_required
def api_rate_card():
    return jsonify({"categories": aircraft_rates.CATEGORY_RATES})


@app.route("/api/pricing/estimate")
@login_required
def api_pricing_estimate():
    aircraft = request.args.get("aircraft", "").strip()
    hours = float(request.args.get("hours", 0) or 0)
    if not aircraft or hours <= 0:
        return jsonify({"error": "aircraft and hours required"}), 400
    est = aircraft_rates.estimate_wholesale(aircraft, hours)
    if not est:
        return jsonify({"error": f"Aircraft '{aircraft}' not in category map"}), 400
    return jsonify({
        "wholesale_range": est,
        "fees_at_mid": aircraft_rates.trip_fees_estimate(est["mid"]),
        "fees_at_low": aircraft_rates.trip_fees_estimate(est["low"]),
        "fees_at_high": aircraft_rates.trip_fees_estimate(est["high"]),
    })


@app.route("/api/pricing/validate")
@login_required
def api_pricing_validate():
    aircraft = request.args.get("aircraft", "").strip()
    hours = float(request.args.get("hours", 0) or 0)
    quoted = float(request.args.get("quoted", 0) or 0)
    if not aircraft or hours <= 0 or quoted <= 0:
        return jsonify({"error": "aircraft, hours, quoted all required"}), 400
    return jsonify(aircraft_rates.validate_quote(aircraft, hours, quoted))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        provided = request.form.get("password", "")
        if provided == AUTH_PASSWORD:
            resp = make_response(redirect(url_for("index")))
            resp.set_cookie(AUTH_COOKIE, AUTH_PASSWORD, max_age=60 * 60 * 24 * 30,
                            httponly=True, samesite="Lax",
                            secure=request.is_secure)
            return resp
        return render_template("login.html", error="Wrong password")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.route("/api/legs")
@login_required
def api_legs():
    origin = request.args.get("origin", "").strip().upper()
    destination = request.args.get("destination", "").strip().upper()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    max_price = request.args.get("max_price", "").strip()
    source = request.args.get("source", "").strip()
    aircraft_cat = request.args.get("category", "").strip()
    legs = db.query_legs(
        origin=origin or None,
        destination=destination or None,
        date_from=date_from or None,
        date_to=date_to or None,
        max_price=float(max_price) if max_price else None,
        source=source or None,
        aircraft_category=aircraft_cat or None,
    )
    stats = db.summary_stats()
    return jsonify({"legs": legs, "stats": stats})


@app.route("/api/legs/manual", methods=["POST"])
@login_required
def api_manual_leg():
    try:
        data = request.get_json(force=True) or {}
        leg = scrapers.add_manual_leg(data)
        return jsonify({"ok": True, "leg": leg})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/operators")
@login_required
def api_operators():
    region = request.args.get("region", "").strip()
    home_base = request.args.get("home_base", "").strip()
    search = request.args.get("q", "").strip()
    ops = db.query_operators(
        region=region or None,
        home_base=home_base or None,
        search=search or None,
    )
    regions = db.operator_regions()
    home_bases = db.operator_home_bases()
    return jsonify({"operators": ops, "regions": regions, "home_bases": home_bases})


@app.route("/operators/export.csv")
@login_required
def operators_export_csv():
    """Download a CSV of operators — feeds mass email/quote-request blasts."""
    region = request.args.get("region", "").strip()
    home_base = request.args.get("home_base", "").strip()
    ops = db.query_operators(region=region or None, home_base=home_base or None)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Home base", "Home base city", "Region", "Phone", "Email", "Website", "Fleet notes"])
    for op in ops:
        w.writerow([
            op.get("name", ""), op.get("home_base", ""), op.get("home_base_city", ""),
            op.get("region", ""), op.get("phone", ""), op.get("email", ""),
            op.get("website", ""), op.get("fleet_notes", ""),
        ])
    csv_text = buf.getvalue()
    fname = f"jetluxeco_operators_{dt.date.today().isoformat()}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/scrape", methods=["POST", "GET"])
def scrape():
    if SCRAPE_TOKEN:
        provided = request.headers.get("X-Scrape-Token", "") or request.args.get("token", "")
        if provided != SCRAPE_TOKEN:
            abort(401)
    results = scrapers.run_all()
    return jsonify({
        "ran_at": dt.datetime.utcnow().isoformat() + "Z",
        "results": results,
    })


@app.route("/enrich", methods=["POST", "GET"])
def enrich():
    """Run ADSB live-flight enrichment on demand."""
    if SCRAPE_TOKEN:
        provided = request.headers.get("X-Scrape-Token", "") or request.args.get("token", "")
        if provided != SCRAPE_TOKEN:
            abort(401)
    try:
        import run_enrichment
        result = run_enrichment.run_inline()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# PUBLIC MARKETING SITE (no auth) — for client-facing sharing
# ============================================================

@app.route("/site")
@app.route("/site/")
def public_home():
    return render_template("public_home.html")


@app.route("/site/legs")
def public_legs():
    return render_template("public_legs.html")


@app.route("/site/api/featured-legs")
def public_featured():
    """Top 6 empty legs with real prices, for the homepage carousel."""
    legs = db.query_legs()
    # Filter to legs with prices and clean routes (skip ADSB-live for public view)
    filtered = [
        l for l in legs
        if l.get("price_usd")
        and l.get("source") not in ("adsb-live", "manual")
        and l.get("origin") not in ("AIRBORNE", None)
    ]
    filtered.sort(key=lambda l: (l.get("depart_date") or ""))
    return jsonify({"legs": filtered[:6]})


@app.route("/site/api/legs")
def public_all_legs():
    """Full public inventory — no ADSB-live rows, only real bookable legs."""
    origin = request.args.get("origin", "").strip().upper()
    destination = request.args.get("destination", "").strip().upper()
    max_price = request.args.get("max_price", "").strip()
    category = request.args.get("category", "").strip()
    legs = db.query_legs(
        origin=origin or None,
        destination=destination or None,
        max_price=float(max_price) if max_price else None,
        aircraft_category=category or None,
    )
    # Public view: exclude live-tracking-only rows without proper origin/dest
    legs = [
        l for l in legs
        if l.get("source") not in ("adsb-live",)
        and l.get("origin") not in ("AIRBORNE", None)
    ]
    return jsonify({"legs": legs, "stats": db.summary_stats()})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": dt.datetime.utcnow().isoformat() + "Z"})


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
