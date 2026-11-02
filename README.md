# İmece bilgi paketi

[İmece](https://github.com/gokhangokdemir) uygulamasının internetsiz arama için telefona indirdiği
çevrimdışı ansiklopedi paketi.

- **Kaynak:** Türkçe Vikipedi (Hugging Face `wikimedia/wikipedia`, `20231101.tr`)
- **İçerik:** En kapsamlı ~130.000 maddenin giriş bölümleri, SQLite FTS5 ile aranabilir, 8 parça
- **Lisans:** Paket içeriği Vikipedi katkıcılarına aittir ve
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.tr) ile lisanslanmıştır.
  Her maddenin kaynağı paketteki adres alanında yer alır.

Paket `build_pack.py` ile GitHub Actions'ta üretilir ve
[`paket`](../../releases/tag/paket) sürümünde yayınlanır.

```bash
pip install pyarrow
python build_pack.py --out dist --limit 130000 --shards 8 train-*.parquet
```
