#!/usr/bin/env python3
"""Bilgi paketine ek dosyalar:

- olgular.db : Vikiveri'den kesin bilgiler (nüfus, başkent, yükseklik, doğum tarihi…)
- yerler.db  : OpenStreetMap'ten Türkiye'deki hastane, eczane, toplanma alanı, su noktası…
- anlam.db   : fastText Türkçe kelime vektörlerinden "anlamca yakın kelimeler" (aramayı genişletir)

Kullanım:
    python build_extras.py --out dist

Lisanslar: Vikiveri CC0; OpenStreetMap © OpenStreetMap katkıcıları, ODbL;
fastText vektörleri (Facebook AI) CC BY-SA 3.0.
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
                return json.loads(r.read().decode("utf-8", "replace"), strict=False)
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
    ("Doğum tarihi", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 90) ?item wdt:P569 ?v ."),
    ("Ölüm tarihi", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 90) ?item wdt:P570 ?v ."),
    ("Doğum yeri", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 90) ?item wdt:P19 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
    ("Meslek", "?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl . FILTER(?sl > 90) ?item wdt:P106 ?c . ?c rdfs:label ?v . FILTER(LANG(?v) = 'tr')"),
]


QLEVER_PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


def fmt_value(prop, raw):
    if raw is None:
        return None
    if prop.endswith("tarihi"):
        # +1881-05-19T00:00:00Z biçimi
        neg = raw.startswith("-")
        parts = raw.lstrip("+-").split("T")[0].split("-")
        if len(parts) != 3:
            return None
        y, mo, d = parts
        if neg:
            try:
                return f"MÖ {int(y)}"
            except ValueError:
                return None
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
        res = None
        try:
            url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
            res = http_json(url, timeout=120, tries=2)
        except Exception as e:  # noqa: BLE001
            print(f"WDQS başarısız ({prop}): {e}; QLever deneniyor", file=sys.stderr)
        if res is None:
            # QLever: büyük taramalarda çok daha hızlı, önekleri açıkça ister
            ql = QLEVER_PREFIXES + q
            try:
                url = "https://qlever.cs.uni-freiburg.de/api/wikidata?query=" + urllib.parse.quote(ql)
                res = http_json(url, timeout=300, tries=2)
            except Exception as e:  # noqa: BLE001
                print(f"Vikiveri sorgusu atlandı ({prop}): {e}", file=sys.stderr)
                continue
        n = 0
        for b in res["results"]["bindings"]:
            label = b["label"]["value"]
            try:
                val = fmt_value(prop, b["v"]["value"])
            except Exception:  # noqa: BLE001
                val = None
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

_UNUSED_OSM_QUERY = """
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


PBF_URL = "https://download.geofabrik.de/europe/turkey-latest.osm.pbf"


def _centroid(geom):
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return c[1], c[0]
    pts = []

    def walk(x):
        if isinstance(x, (list, tuple)) and x and isinstance(x[0], (int, float)):
            pts.append(x)
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)

    walk(c)
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def build_places(out):
    """Türkiye OSM verisi (Geofabrik) + osmium ile acil durum yerleri."""
    import subprocess

    work = os.path.join(out, "..", "osm")
    os.makedirs(work, exist_ok=True)
    pbf = os.path.join(work, "turkey.osm.pbf")
    flt = os.path.join(work, "filtered.osm.pbf")
    seq = os.path.join(work, "yerler.geojsonseq")
    if not os.path.exists(pbf):
        subprocess.run(["curl", "-fL", "--retry", "5", "-o", pbf, PBF_URL], check=True)
    subprocess.run(
        [
            "osmium", "tags-filter", pbf,
            "nwr/amenity=hospital,clinic,pharmacy,doctors,fire_station,police,drinking_water,shelter,school",
            "nwr/emergency=assembly_point,ambulance_station",
            "nwr/healthcare=hospital",
            "-o", flt, "--overwrite",
        ],
        check=True,
    )
    subprocess.run(["osmium", "export", flt, "-f", "geojsonseq", "-o", seq, "--overwrite"], check=True)

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
    seen = set()
    with open(seq, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except ValueError:
                continue
            tags = feat.get("properties", {})
            kind = KIND.get(tags.get("emergency")) or KIND.get(tags.get("amenity")) or ("hastane" if tags.get("healthcare") == "hospital" else None)
            if not kind:
                continue
            c = _centroid(feat.get("geometry") or {})
            if not c:
                continue
            lat, lon = round(c[0], 6), round(c[1], 6)
            name0 = (tags.get("name:tr") or tags.get("name") or "").strip().lower()
            key = (kind, name0, round(lat, 3), round(lon, 3)) if name0 else (kind, round(lat, 4), round(lon, 4))
            if key in seen:
                continue
            seen.add(key)
            name = tags.get("name:tr") or tags.get("name") or ""
            addr = ", ".join(x for x in (tags.get("addr:street"), tags.get("addr:district"), tags.get("addr:city")) if x)
            con.execute("INSERT INTO y (k, n, lat, lon, a) VALUES (?, ?, ?, ?, ?)", (kind, name[:80], lat, lon, addr[:120]))
            n += 1
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"yerler.db: {n} yer")
    return path, n


# ---------------- Anlam (kelime vektörleri) ----------------

VEC_URL = "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.tr.300.vec.gz"


def build_related(out, words=80000, k=6, min_sim=0.55):
    import gzip

    import numpy as np

    vocab, vecs, seen = [], [], set()
    req = urllib.request.Request(VEC_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r, gzip.GzipFile(fileobj=r) as gz:
        gz.readline()  # başlık
        for raw in gz:
            parts = raw.decode("utf-8", "ignore").rstrip().split(" ")
            w = parts[0]
            if not w.isalpha() or len(w) < 3:
                continue
            nw = normalize(w)
            if not nw or " " in nw or nw in seen:
                continue
            seen.add(nw)
            vocab.append(nw)
            vecs.append(np.asarray(parts[1:301], dtype=np.float32))
            if len(vocab) >= words:
                break
    m = np.vstack(vecs)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-8
    path = os.path.join(out, "anlam.db")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE r (w TEXT PRIMARY KEY, n TEXT NOT NULL) WITHOUT ROWID")
    n = 0
    step = 2000
    for i in range(0, len(vocab), step):
        sims = m[i : i + step] @ m.T
        for j in range(sims.shape[0]):
            sims[j, i + j] = -1
        top = np.argpartition(-sims, k, axis=1)[:, :k]
        for j in range(sims.shape[0]):
            w = vocab[i + j]
            cand = sorted(((float(sims[j, t]), vocab[t]) for t in top[j]), reverse=True)
            # aynı kökten türeyen kelimeleri ("ankara" -> "ankarada") ele, anlamca yakın olanları tut
            rel = [v for s_, v in cand if s_ >= min_sim and not v.startswith(w[:4]) and not w.startswith(v[:4])]
            if rel:
                con.execute("INSERT INTO r VALUES (?, ?)", (w, " ".join(rel[:4])))
                n += 1
        print(f"  anlam: {min(i + step, len(vocab))}/{len(vocab)}")
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"anlam.db: {n} kelime")
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
    try:
        places = build_places(a.out)
    except Exception as e:  # noqa: BLE001
        print(f"yerler.db üretilemedi: {e}", file=sys.stderr)
        places = (None, 0)
    try:
        related = build_related(a.out)
    except Exception as e:  # noqa: BLE001
        print(f"anlam.db üretilemedi: {e}", file=sys.stderr)
        related = (None, 0)
    add_to_manifest(
        a.out,
        [
            ("olgular", facts[0], facts[1], "Vikiveri", "CC0"),
            ("yerler", places[0], places[1], "OpenStreetMap", "ODbL · © OpenStreetMap katkıcıları"),
            ("anlam", related[0], related[1], "fastText (Facebook AI)", "CC BY-SA 3.0"),
        ],
    )
