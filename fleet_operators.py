"""
Fleet-and-hub operator inference.

For an empty-leg listing where the operator is anonymized (EmptyJet, etc.),
score-match candidate operators using two signals:
  1. FLEET MATCH: does the operator fly this aircraft type?
  2. HUB MATCH: is the leg's origin airport one of the operator's hubs?

Curated from public info (Nov 2025 – Sept 2026):
  - operator directory listings (charter broker sites)
  - FAA registry lookups on published fleet tails
  - operator press releases + fleet update posts
  - well-known N-number ranges (e.g., ###QS = NetJets, ###FX = Flexjet)

Confidence tiers:
  HIGH   — tail-prefix match OR hub is exclusive (e.g., Elite Jets from KAPF)
  MEDIUM — fleet + one hub match
  LOW    — fleet match only, no hub signal
"""

# Each entry: fleet aircraft codes + primary hub airports
# Aircraft codes match ADSB type designators + common model strings
FLEET = {
    "NetJets": {
        "aircraft": ["C68A","C700","E55P","CL35","CL60","GL7T","C56X","C750","citation latitude","citation longitude","citation xls","phenom 300","challenger 350","challenger 650","global 7500","sovereign"],
        "hubs": ["KTEB","KIAD","KHOU","KSDL","KOPF","KOMA","KMKC","KHPN","KMDW","KLAS","KVNY","KBED","KFLL"],
        "tail_prefix": "QS",  # N###QS
        "phone": "+1-877-356-5823",
        "email": "info@netjets.com",
        "website": "https://www.netjets.com",
    },
    "Flexjet": {
        "aircraft": ["cl35","challenger 350","challenger 3500","praetor 500","praetor 600","e545","e550","legacy 450","legacy 500","gulfstream g450","gulfstream g650","g450","g650","glf6"],
        "hubs": ["KRIC","KTEB","KLAS","KVNY","KDAL","KPBI","KFLL","KSDL","KTEB"],
        "tail_prefix": "FX",
        "phone": "+1-866-353-9538",
        "email": "info@flexjet.com",
        "website": "https://www.flexjet.com",
    },
    "Wheels Up": {
        "aircraft": ["c56x","citation xl","citation xls","citation x","c750","king air 350","be30","B350","challenger 300","challenger 350","cl30","cl35"],
        "hubs": ["KHKY","KSDL","KTEB","KOPF","KVNY","KPWK","KHPN"],
        "tail_prefix": "WU",
        "phone": "+1-855-359-8757",
        "email": "info@wheelsup.com",
        "website": "https://www.wheelsup.com",
    },
    "Executive Jet Management": {
        "aircraft": ["cl60","challenger 604","challenger 605","falcon 900","f900","gulfstream g200","g200","gulfstream g450","g450","f2th","falcon 2000"],
        "hubs": ["KOPF","KLUK","KTEB","KAPA","KDAL"],
        "tail_prefix": "EJ",
        "phone": "+1-877-356-5387",
        "email": "charter@ejmjets.com",
        "website": "https://www.ejmjets.com",
    },
    "Vista / VistaJet / XO": {
        "aircraft": ["glex","gl7t","global 7500","global 6000","challenger 350","challenger 850","challenger 605","challenger 3500","cl30","cl35"],
        "hubs": ["KMIA","KOPF","KTEB","KVNY","KLAS","KDAL"],
        "tail_prefix": "VJ",
        "phone": "+1-855-542-6262",
        "email": "sales@vistajet.com",
        "website": "https://www.vistajet.com",
    },
    "Jet Linx Aviation": {
        "aircraft": ["e55p","phenom 300","hawker 400","be40","c56x","citation xls","cl35","challenger 350"],
        "hubs": ["KOMA","KMKC","KSDL","KFTW","KRDU","KAUS","KIAH","KAPF","KFXE","KDAB","KJAX","KATL"],
        "tail_prefix": None,
        "phone": "+1-866-538-5469",
        "email": "info@jetlinx.com",
        "website": "https://www.jetlinx.com",
    },
    "Airshare": {
        "aircraft": ["e55p","phenom 300","cl35","challenger 3500"],
        "hubs": ["KMKC","KMCI","KOMA","KSDL","KIND","KDAL"],
        "tail_prefix": None,
        "phone": "+1-816-268-5959",
        "email": "info@airshare.com",
        "website": "https://airshare.com",
    },
    "Nicholas Air": {
        "aircraft": ["e50p","phenom 100","e55p","phenom 300","cl35","praetor 500","citation cj3","c25b","c525"],
        "hubs": ["KOLV","KMEM","KOXR","KTEB","KMIA"],
        "tail_prefix": None,
        "phone": "+1-866-935-7771",
        "email": "info@nicholasair.com",
        "website": "https://www.nicholasair.com",
    },
    "Elite Jets": {
        "aircraft": ["e55p","phenom 300","legacy 500","e550","gulfstream g550","g550"],
        "hubs": ["KAPF","KFXE","KOPF"],
        "tail_prefix": None,
        "phone": "+1-239-900-9000",
        "email": "charter@elitejets.com",
        "website": "https://www.elitejets.com",
    },
    "Solairus Aviation": {
        "aircraft": ["c68a","citation latitude","citation excel","c560","citation xls","be30","king air","cl35","challenger 350"],
        "hubs": ["KAPC","KOAK","KSJC","KCCR","KVNY"],
        "tail_prefix": None,
        "phone": "+1-800-359-2462",
        "email": "info@solairus.aero",
        "website": "https://solairus.aero",
    },
    "Priester Aviation": {
        "aircraft": ["f2th","falcon 2000","gulfstream g-iv","gulfstream g450","g450","gulfstream g550","g550","legacy 600","legacy 650"],
        "hubs": ["KPWK","KMDW","KDPA"],
        "tail_prefix": None,
        "phone": "+1-847-509-2360",
        "email": "info@priesterav.com",
        "website": "https://www.priesteraviation.com",
    },
    "Clay Lacy Aviation": {
        "aircraft": ["glf5","gulfstream g550","g550","glf6","gulfstream g650","g650","glex","global","cl60","challenger 605","challenger 650"],
        "hubs": ["KVNY","KOXR","KSNA","KSMO","KBFI","KSEA"],
        "tail_prefix": None,
        "phone": "+1-800-423-2904",
        "email": "charter@claylacy.com",
        "website": "https://www.claylacy.com",
    },
    "Jet Aviation": {
        "aircraft": ["glf6","gulfstream g650","g650","glf5","gulfstream g550","g550","fa7x","falcon 7x","fa8x","falcon 8x","glex","global 6000","global 7500"],
        "hubs": ["KTEB","KBED","KVNY","KDAL"],
        "tail_prefix": None,
        "phone": "+1-201-462-4000",
        "email": "info@jetaviation.com",
        "website": "https://www.jetaviation.com",
    },
    "Talon Air": {
        "aircraft": ["legacy 600","legacy 650","f2th","falcon 2000","g550","gulfstream g550","cl60","challenger 605","challenger 650"],
        "hubs": ["KFRG","KISP","KHPN","KTEB"],
        "tail_prefix": None,
        "phone": "+1-631-770-8000",
        "email": "info@talonairjets.com",
        "website": "https://www.talonairjets.com",
    },
    "Set Jet": {
        "aircraft": ["c56x","citation xls","citation excel"],
        "hubs": ["KSDL","KLAS","KVNY","KBUR","KTUS","KABQ","KDEN","KDAL"],
        "tail_prefix": None,
        "phone": "+1-833-738-5387",
        "email": "info@setjet.com",
        "website": "https://www.setjet.com",
    },
    "JSX": {
        "aircraft": ["e135","embraer 135","e145","embraer 145","erj-135","erj-145","erj"],
        "hubs": ["KHPN","KDAL","KOAK","KBUR","KLAS","KRNO","KSDL","KAUS","KHOU","KCRQ","KTPA","KMIA","KMCO"],
        "tail_prefix": "JX",
        "phone": "+1-800-435-9579",
        "email": "hello@jsx.com",
        "website": "https://www.jsx.com",
    },
    "Latitude 33 Aviation": {
        "aircraft": ["e55p","phenom 300","cl30","cl35","challenger 300","challenger 350"],
        "hubs": ["KCRQ","KSNA","KVNY","KBUR"],
        "tail_prefix": None,
        "phone": "+1-800-840-0227",
        "email": "info@latitude33aviation.com",
        "website": "https://latitude33aviation.com",
    },
    "Sun Air Jets": {
        "aircraft": ["c56x","citation excel","cl30","cl35","challenger 300","challenger 350","legacy 500","e550","g150","gulfstream g150"],
        "hubs": ["KCMA","KVNY","KOXR"],
        "tail_prefix": None,
        "phone": "+1-800-359-5387",
        "email": "info@sunairjets.com",
        "website": "https://sunairjets.com",
    },
    "Silverhawk Aviation": {
        "aircraft": ["e55p","phenom 300","c25b","c525","citation cj3","cl30","challenger 300"],
        "hubs": ["KLNK","KOMA","KMKC"],
        "tail_prefix": None,
        "phone": "+1-800-742-6202",
        "email": "info@silverhawkaviation.com",
        "website": "https://silverhawkaviation.com",
    },
    "Banyan Air Service": {
        "aircraft": ["c25b","c525","citation cj3","phenom 300","e55p","cl30","challenger 300"],
        "hubs": ["KFXE","KFLL","KPBI"],
        "tail_prefix": None,
        "phone": "+1-954-491-3170",
        "email": "info@banyanair.com",
        "website": "https://www.banyanair.com",
    },
    "Volo Aviation": {
        "aircraft": ["c25b","c525","citation cj3","c68a","citation sovereign","cl30","challenger 300"],
        "hubs": ["KBDR","KHPN","KTEB"],
        "tail_prefix": None,
        "phone": "+1-203-378-8656",
        "email": "info@voloaviation.com",
        "website": "https://voloaviation.com",
    },
    "Thrive Aviation": {
        "aircraft": ["cl30","challenger 300","cl35","challenger 350","gulfstream g450","g450","glf5","g550","gulfstream g550","glf6","g650","gulfstream g650"],
        "hubs": ["KLAS","KVNY"],
        "tail_prefix": None,
        "phone": "+1-702-472-9000",
        "email": "info@thriveaviation.com",
        "website": "https://www.thriveaviation.com",
    },
    "Desert Jet": {
        "aircraft": ["c56x","citation xls","phenom 300","e55p","cl30","challenger 300"],
        "hubs": ["KTRM","KPSP","KSNA","KVNY"],
        "tail_prefix": None,
        "phone": "+1-760-346-3931",
        "email": "info@desertjet.com",
        "website": "https://desertjet.com",
    },
    "Elevate Jet": {
        "aircraft": ["c56x","citation xls","cl30","challenger 300","cl35","challenger 350"],
        "hubs": ["KBED","KTEB","KPBI"],
        "tail_prefix": None,
        "phone": "+1-508-644-0400",
        "email": "info@elevatejet.com",
        "website": "https://elevatejet.com",
    },
    "PrivaireA (fmr Privaira)": {
        "aircraft": ["cl30","challenger 300","phenom 300","e55p","legacy 500","e550"],
        "hubs": ["KBCT","KFLL","KPBI"],
        "tail_prefix": None,
        "phone": "+1-561-395-8888",
        "email": "charter@tlcjetcharter.com",
        "website": "https://www.tlcjetcharter.com",
    },
    "TLC Jet Charter": {
        "aircraft": ["cl30","challenger 300","phenom 300","e55p","citation excel","c56x"],
        "hubs": ["KBCT","KFLL","KPBI","KFXE"],
        "tail_prefix": None,
        "phone": "+1-561-395-8888",
        "email": "charter@tlcjetcharter.com",
        "website": "https://www.tlcjetcharter.com",
    },
    "Wing Aviation": {
        "aircraft": ["c56x","citation excel","c68a","citation sovereign","cl30","challenger 300","cl35","challenger 350"],
        "hubs": ["KHOU","KIAH","KDAL"],
        "tail_prefix": None,
        "phone": "+1-281-583-9464",
        "email": "info@wingaviation.com",
        "website": "https://www.wingaviation.com",
    },
    "Mountain Aviation": {
        "aircraft": ["c56x","citation xls","c25b","c525","citation cj3","cl30","challenger 300"],
        "hubs": ["KBJC","KAPA","KEGE","KASE"],
        "tail_prefix": None,
        "phone": "+1-303-460-0100",
        "email": "charter@mountainaviation.com",
        "website": "https://mountainaviation.com",
    },
    "StarJets International": {
        "aircraft": ["c25a","c525","citation cj2","c56x","citation excel","cl30","challenger 300","gulfstream g200","g200"],
        "hubs": ["KFXE","KFLL","KPBI"],
        "tail_prefix": None,
        "phone": "+1-954-771-0002",
        "email": "info@starjetsintl.com",
        "website": "https://starjetsintl.com",
    },
    "Grand View Aviation": {
        "aircraft": ["c25b","c525","citation cj3","phenom 300","e55p"],
        "hubs": ["KMTN","KFDK","KADW","KIAD"],
        "tail_prefix": None,
        "phone": "+1-410-682-4600",
        "email": "info@grandviewaviation.com",
        "website": "https://grandviewaviation.com",
    },
}


# -------- airport distance for hub proximity scoring --------

_AIRPORT_COORDS = None

def _load_coords():
    global _AIRPORT_COORDS
    if _AIRPORT_COORDS is not None:
        return _AIRPORT_COORDS
    import csv, math
    _AIRPORT_COORDS = {}
    try:
        with open("/tmp/airports.csv") as f:
            for r in csv.DictReader(f):
                ident = (r.get("ident") or "").strip().upper()
                if len(ident) == 4:
                    try:
                        _AIRPORT_COORDS[ident] = (float(r["latitude_deg"]), float(r["longitude_deg"]))
                    except:
                        pass
    except FileNotFoundError:
        pass
    return _AIRPORT_COORDS


def _distance_nm(a, b):
    import math
    coords = _load_coords()
    if a not in coords or b not in coords: return None
    lat1, lon1 = coords[a]; lat2, lon2 = coords[b]
    lat1r, lon1r = math.radians(lat1), math.radians(lon1)
    lat2r, lon2r = math.radians(lat2), math.radians(lon2)
    dlat = lat2r - lat1r; dlon = lon2r - lon1r
    aa = math.sin(dlat/2)**2 + math.cos(lat1r)*math.cos(lat2r)*math.sin(dlon/2)**2
    return 2 * 3440.065 * math.asin(math.sqrt(aa))


# -------- inference --------

def _norm_ac(name):
    if not name: return ""
    return name.lower().strip().replace(" ","")


def infer_operator(aircraft: str | None, origin_icao: str | None, tail: str | None = None) -> dict | None:
    """
    Score every operator against this leg and return the best match.
      - Tail prefix match = HIGH confidence (fastest, most accurate)
      - Exact fleet + hub-at-origin = HIGH
      - Fleet + hub within 100 nm = MEDIUM
      - Fleet only = LOW
    Returns dict or None (no plausible match).
    """
    # 1. Tail prefix (extremely reliable when we have the tail)
    if tail:
        t = tail.strip().upper()
        for op, cfg in FLEET.items():
            prefix = cfg.get("tail_prefix")
            if prefix and t.endswith(prefix):
                return {"operator": op, "confidence": "HIGH", "reason": f"tail suffix {prefix}",
                        "phone": cfg["phone"], "email": cfg["email"], "website": cfg["website"]}

    # 2. Fleet + hub scoring
    if not aircraft:
        return None
    ac_norm = _norm_ac(aircraft)

    candidates = []
    for op, cfg in FLEET.items():
        # Fleet match
        fleet_match = any(_norm_ac(a) in ac_norm or ac_norm in _norm_ac(a) for a in cfg["aircraft"])
        if not fleet_match:
            continue
        # Hub scoring
        best_hub_dist = None
        best_hub = None
        if origin_icao and origin_icao in cfg["hubs"]:
            best_hub_dist = 0
            best_hub = origin_icao
        elif origin_icao:
            for hub in cfg["hubs"]:
                d = _distance_nm(origin_icao, hub)
                if d is not None and (best_hub_dist is None or d < best_hub_dist):
                    best_hub_dist = d
                    best_hub = hub
        # Score
        if best_hub_dist == 0:
            score, conf = 100, "HIGH"
        elif best_hub_dist is not None and best_hub_dist < 50:
            score, conf = 80, "HIGH"
        elif best_hub_dist is not None and best_hub_dist < 150:
            score, conf = 60, "MEDIUM"
        elif best_hub_dist is not None and best_hub_dist < 400:
            score, conf = 40, "LOW"
        else:
            score, conf = 25, "LOW"
        candidates.append({"operator": op, "score": score, "confidence": conf,
                           "reason": f"fleet+{best_hub}(~{int(best_hub_dist) if best_hub_dist else '?'}nm)",
                           "phone": cfg["phone"], "email": cfg["email"], "website": cfg["website"]})

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["score"])
    return candidates[0]


if __name__ == "__main__":
    import sys
    # Smoke tests
    for ac, o, t in [
        ("Learjet 60", "KOMA", None),          # Silverhawk?
        ("Citation CJ3+", "KFXE", None),        # Banyan
        ("Gulfstream G650ER", "KFLL", "N654FX"),# Flexjet (tail)
        ("Global 7500", "KTEB", None),          # NetJets or Jet Aviation
        ("Phenom 300", "KAPF", None),           # Elite Jets
        ("Falcon 2000", "KPWK", None),          # Priester
    ]:
        print(f"{ac:20s} from {o:6s} tail={t}")
        r = infer_operator(ac, o, t)
        print(f"    → {r}")
        print()
