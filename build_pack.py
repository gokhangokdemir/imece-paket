#!/usr/bin/env python3
"""İmece çevrimdışı bilgi paketi üretici.

Türkçe Vikipedi'den (Hugging Face: wikimedia/wikipedia, düz metin) en kapsamlı
maddeleri seçer, her maddenin giriş bölümünü alır ve telefonda internetsiz
aranabilecek parçalı SQLite (FTS5) dosyaları üretir.

Kullanım:
    python build_pack.py --out dist --limit 130000 train-*.parquet

Lisans: içerik Vikipedi'den alınmıştır, CC BY-SA 4.0.
"""

import argparse
import hashlib
import heapq
import json
import os
import re
import sqlite3
import time
import unicodedata

import pyarrow.parquet as pq

# Uygulamadaki src/lib/text.ts normalize() ile birebir aynı olmalı.
FOLD = {"ç": "c", "ğ": "g", "ı": "i", "i": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"}
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def normalize(s: str) -> str:
    low = unicodedata.normalize("NFC", tr_lower(s))
    folded = "".join(FOLD.get(ch, ch) for ch in low)
    stripped = "".join(ch for ch in unicodedata.normalize("NFD", folded) if not unicodedata.combining(ch))
    return NON_ALNUM.sub(" ", stripped).strip()


SKIP_TITLE = re.compile(r"^(Liste|Kategori|Vikipedi|Şablon|Dosya|Portal)[: ]|\(anlam ayrımı\)", re.I)
SENT_END = re.compile(r"[.!?](?=\s+[A-ZÇĞİÖŞÜ0-9\"“(])")


def lead_of(text: str, min_len: int, max_len: int) -> str:
    """Maddenin giriş bölümü: paragrafları min_len'e ulaşana kadar al, max_len'i cümle sınırında kes."""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out = ""
    for p in paras:
        # başlık satırlarını (kısa, noktasız) atla
        if len(p) < 60 and not p.endswith("."):
            if out:
                break
            continue
        out = f"{out}\n{p}" if out else p
        if len(out) >= min_len:
            break
    if len(out) > max_len:
        cut = out[:max_len]
        ends = [m.end() for m in SENT_END.finditer(cut)]
        out = cut[: ends[-1]] if ends and ends[-1] > max_len * 0.5 else cut.rsplit(" ", 1)[0] + "…"
    return out


def candidates(files, min_text):
    for path in files:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(columns=["title", "text", "url"], batch_size=4000):
            d = batch.to_pydict()
            for title, text, url in zip(d["title"], d["text"], d["url"]):
                if not title or not text or len(text) < min_text or SKIP_TITLE.search(title):
                    continue
                head = text[:300]
                if "anlamlara gelebilir" in head or "anlamına gelebilir" in head:
                    continue
                yield title, text, url


def build(files, out, limit, shards, min_text, lead_min, lead_max):
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    # en uzun (en kapsamlı) maddeleri tut: madde uzunluğu önemin iyi bir göstergesi
    heap = []
    seen = 0
    for i, (title, text, url) in enumerate(candidates(files, min_text)):
        seen += 1
        item = (len(text), i, title, text, url)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item[0] > heap[0][0]:
            heapq.heapreplace(heap, item)
    chosen = sorted(heap, key=lambda x: -x[0])
    print(f"{seen} uygun madde, {len(chosen)} seçildi ({time.time() - t0:.0f} sn)")

    dbs = []
    for k in range(shards):
        path = os.path.join(out, f"bilgi-{k}.db")
        if os.path.exists(path):
            os.remove(path)
        con = sqlite3.connect(path)
        con.executescript(
            """
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE a (id INTEGER PRIMARY KEY, t TEXT NOT NULL, b TEXT NOT NULL, u TEXT NOT NULL);
            CREATE VIRTUAL TABLE f USING fts5(nt, nb, content='', tokenize='unicode61');
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        dbs.append(con)

    counts = [0] * shards
    for n, (_, _, title, text, url) in enumerate(chosen):
        lead = lead_of(text, lead_min, lead_max)
        if len(lead) < 120:
            continue
        k = n % shards
        rid = counts[k] + 1
        counts[k] = rid
        con = dbs[k]
        con.execute("INSERT INTO a (id, t, b, u) VALUES (?, ?, ?, ?)", (rid, title, lead, url))
        con.execute("INSERT INTO f (rowid, nt, nb) VALUES (?, ?, ?)", (rid, normalize(title), normalize(lead)))

    created = int(time.time() * 1000)
    manifest = {
        "version": created,
        "created": time.strftime("%Y-%m-%d", time.gmtime()),
        "title": "Türkçe Vikipedi",
        "source": "Vikipedi (Hugging Face wikimedia/wikipedia, 20231101.tr)",
        "license": "CC BY-SA 4.0",
        "articles": sum(counts),
        "shards": [],
    }
    for k, con in enumerate(dbs):
        con.execute("INSERT INTO meta VALUES ('version', ?)", (str(created),))
        con.execute("INSERT INTO f(f) VALUES ('optimize')")
        con.commit()
        con.execute("VACUUM")
        con.close()
        path = os.path.join(out, f"bilgi-{k}.db")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        manifest["shards"].append({"name": f"bilgi-{k}.db", "bytes": os.path.getsize(path), "count": counts[k], "sha256": h.hexdigest()})
    manifest["bytes"] = sum(s["bytes"] for s in manifest["shards"])
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"{manifest['articles']} madde, {manifest['bytes'] / 1e6:.0f} MB, {shards} parça ({time.time() - t0:.0f} sn)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--limit", type=int, default=130000)
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--min-text", type=int, default=400)
    ap.add_argument("--lead-min", type=int, default=600)
    ap.add_argument("--lead-max", type=int, default=1300)
    a = ap.parse_args()
    build(a.files, a.out, a.limit, a.shards, a.min_text, a.lead_min, a.lead_max)
