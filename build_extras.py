#!/usr/bin/env python3
"""Bilgi paketine ek dosyalar:

- olgular.db : Vikiveri'den kesin bilgiler (nüfus, başkent, yükseklik, doğum tarihi…)
- yerler.db  : OpenStreetMap'ten Türkiye'deki hastane, eczane, toplanma alanı, su noktası…

Kullanım:
    python build_extras.py --out dist

Lisanslar: Vikiveri CC0; OpenStreetMap © OpenStreetMap katkıcıları, ODbL.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

from build_pack import normalize

UA = "ImeceBilgiPaketi/1.0 (https://github.com/gokhangokdemir/imece-paket)"


def http_json(url, data=None, timeout=900, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  deneme {i + 1} başarısız: {e}", file=sys.stderr)
            time.sleep(15 * (i + 1))
    raise last


# ---------------- Vikiveri ----------------

# (özellik adı, SPARQL gövdesi). ?item ve ?val döndürmeli; etiketler Türkçe.
WD_QUERIES = [
    # Türkiye illeri ve ilçeleri
    ("Nüfus", "?item wdt:P31/wdt:P279* wd:Q48336 . ?item wdt:P1082 ?v ."),
    ("Nüfus", "?item wdt:P31 wd:Q1529096 . ?item wdt:P1082 ?v ."),
    ("Yüzölçümü (km²)", "?item wdt:P31/wdt:P279* wd:Q48336 . ?item wdt:P2046 ?v ."),
    ("Rakım (m)", "?item wdt:P31/wdt:P279* wd:Q48336 . ?item wdt:P2044 ?v ."),
    # Ülkeler
    ("Başkent", "?item wdt:P31 wd:Q6256 . ?item wdt:P36 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
    ("Nüfus", "?item wdt:P31 wd:Q6256 . ?item wdt:P1082 ?v ."),
    ("Yüzölçümü (km²)", "?item wdt:P31 wd:Q6256 . ?item wdt:P2046 ?v ."),
    ("Para birimi", "?item wdt:P31 wd:Q6256 . ?item wdt:P38 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
    ("Resmî dil", "?item wdt:P31 wd:Q6256 . ?item wdt:P37 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
    # Büyük şehirler
    ("Nüfus", "?item wdt:P31/wdt:P279* wd:Q515 . ?item wdt:P1082 ?v . FILTER(?v > 500000)"),
    # Dağlar, nehirler, göller
    ("Yükseklik (m)", "?item wdt:P31 wd:Q8502 . ?item wdt:P2044 ?v . FILTER(?v > 2500)"),
    ("Uzunluk (km)", "?item wdt:P31 wd:Q4022 . ?item wdt:P2043 ?v . FILTER(?v > 100)"),
    ("Yüzölçümü (km²)", "?item wdt:P31 wd:Q23397 . ?item wdt:P2046 ?v . FILTER(?v > 50)"),
    # Tanınmış kişiler (en az 60 dilde maddesi olanlar)
    ("Doğum tarihi", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 60) ?item wdt:P569 ?v ."),
    ("Ölüm tarihi", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 60) ?item wdt:P570 ?v ."),
    ("Doğum yeri", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 60) ?item wdt:P19 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
    ("Meslek", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 60) ?item wdt:P106 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
]


def fmt_value(prop, raw):
    if raw is None:
        return None
    if prop.endswith("tarihi"):
        # +1881-05-19T00:00:00Z biçimi
        m = raw.lstrip("+")[:10]
        y, mo, d = m.split("-")
        aylar = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
        try:
            return f"{int(d)} {aylar[int(mo) - 1]} {int(y)}" if mo != "00" and d != "00" else str(int(y))
        except (ValueError, IndexError):
            return m
    try:
        num = float(raw)
        if num.is_integer():
            return f"{int(num):,}".replace(",", ".")
        return f"{num:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except ValueError:
        return raw


def build_facts(out):
    rows = {}
    for prop, body in WD_QUERIES:
        q = f"""SELECT ?item ?label ?v WHERE {{
            {body}
            ?item rdfs:label ?label . FILTER(LANG(?label) = 'tr')
        }}"""
        url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
        try:
            res = http_json(url, timeout=120)
        except Exception as e:  # noqa: BLE001
            print(f"Vikiveri sorgusu atlandı ({prop}): {e}", file=sys.stderr)
            continue
        n = 0
        for b in res["results"]["bindings"]:
            label = b["label"]["value"]
            val = fmt_value(prop, b["v"]["value"])
            if not val:
                continue
            key = (label, prop)
            try:
                weight = float(b["v"]["value"])
            except ValueError:
                weight = 0.0
            prev = rows.get(key)
            if prev is None:
                rows[key] = (val, weight)
                n += 1
            elif prop == "Nüfus" and weight > prev[1]:
                rows[key] = (val, weight)
            elif prop in ("Meslek", "Resmî dil", "Para birimi") and val not in prev[0] and prev[0].count(",") < 2:
                rows[key] = (f"{prev[0]}, {val}", prev[1])
        print(f"Vikiveri {prop}: {n}")
        time.sleep(2)

    path = os.path.join(out, "olgular.db")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE o (id INTEGER PRIMARY KEY, s TEXT NOT NULL, ns TEXT NOT NULL, p TEXT NOT NULL, v TEXT NOT NULL);
        CREATE INDEX o_ns ON o(ns);
        """
    )
    for (label, prop), (val, _) in rows.items():
        con.execute("INSERT INTO o (s, ns, p, v) VALUES (?, ?, ?, ?)", (label, normalize(label), prop, val))
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"olgular.db: {len(rows)} bilgi")
    return path, len(rows)


# ---------------- OpenStreetMap ----------------

OSM_QUERY = """
[out:json][timeout:900][maxsize:2000000000];
area["ISO3166-1"="TR"][admin_level=2]->.tr;
(
  nwr["amenity"~"^(hospital|clinic|pharmacy|doctors|fire_station|police|drinking_water|shelter|school)$"](area.tr);
  nwr["emergency"~"^(assembly_point|ambulance_station)$"](area.tr);
  nwr["healthcare"="hospital"](area.tr);
);
out center tags;
"""

KIND = {
    "hospital": "hastane",
    "clinic": "saglik",
    "doctors": "saglik",
    "pharmacy": "eczane",
    "fire_station": "itfaiye",
    "police": "polis",
    "drinking_water": "su",
    "shelter": "barinak",
    "school": "okul",
    "assembly_point": "toplanma",
    "ambulance_station": "ambulans",
}


def build_places(out):
    data = urllib.parse.urlencode({"data": OSM_QUERY}).encode()
    res = None
    for ep in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            res = http_json(ep, data=data, timeout=1200, tries=2)
            break
        except Exception as e:  # noqa: BLE001
            print(f"Overpass ({ep}) başarısız: {e}", file=sys.stderr)
    if res is None:
        return None, 0
    path = os.path.join(out, "yerler.db")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE y (id INTEGER PRIMARY KEY, k TEXT NOT NULL, n TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, a TEXT NOT NULL);
        CREATE INDEX y_k_lat ON y(k, lat);
        """
    )
    n = 0
    for el in res.get("elements", []):
        tags = el.get("tags", {})
        kind = KIND.get(tags.get("emergency")) or KIND.get(tags.get("amenity")) or ("hastane" if tags.get("healthcare") == "hospital" else None)
        if not kind:
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None or lon is None:
            continue
        name = tags.get("name:tr") or tags.get("name") or ""
        addr = ", ".join(x for x in (tags.get("addr:street"), tags.get("addr:district"), tags.get("addr:city")) if x)
        con.execute("INSERT INTO y (k, n, lat, lon, a) VALUES (?, ?, ?, ?, ?)", (kind, name[:80], round(lat, 6), round(lon, 6), addr[:120]))
        n += 1
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"yerler.db: {n} yer")
    return path, n


def add_to_manifest(out, entries):
    import hashlib

    mpath = os.path.join(out, "manifest.json")
    with open(mpath, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["extras"] = []
    for kind, path, count, source, lic in entries:
        if not path:
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        manifest["extras"].append(
            {"kind": kind, "name": os.path.basename(path), "bytes": os.path.getsize(path), "count": count, "sha256": h.hexdigest(), "source": source, "license": lic}
        )
    manifest["bytes"] = sum(s["bytes"] for s in manifest["shards"]) + sum(e["bytes"] for e in manifest["extras"])
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    facts = build_facts(a.out)
    places = build_places(a.out)
    add_to_manifest(
        a.out,
        [
            ("olgular", facts[0], facts[1], "Vikiveri", "CC0"),
            ("yerler", places[0], places[1], "OpenStreetMap", "ODbL · © OpenStreetMap katkıcıları"),
        ],
    )
