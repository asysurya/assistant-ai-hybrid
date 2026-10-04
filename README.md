# AssistantAI — Asisten AI Hybrid Desktop

Asisten AI pribadi untuk **Windows 11 ARM64** (target uji: ThinkPad E580, RAM 8GB, SSD 239GB).
Chat teks + suara, analisis file, tool calling, system monitor — dengan **routing hybrid**
antara model **lokal** (Ollama, offline & gratis) dan model **cloud** (Ollama Cloud).

> Proyek dibangun **bertahap per fase** (12 fase). Setiap fase wajib lulus test
> sebelum lanjut ke fase berikutnya. Status terkini: **FASE 1 selesai — core
> backend + routing 3-tier berjalan**.

| | |
|---|---|
| Build CI | [![Build](https://github.com/asysurya/assistant-ai-hybrid/actions/workflows/build.yml/badge.svg)](https://github.com/asysurya/assistant-ai-hybrid/actions/workflows/build.yml) |
| Lisensi | MIT — lihat [LICENSE](LICENSE) |
| Python | 3.11+ |

> Catatan: ganti `[Nama Anda]` pada LICENSE dengan nama Anda.

## Kenapa hybrid?

Laptop 8GB RAM tidak realistis menjalankan model besar secara lokal, tetapi memakai
cloud untuk semua hal juga boros dan bergantung internet. Strategi **3-tier** memberi
tiga keuntungan sekaligus. Pertama, tugas ringan ditangani `qwen2.5:0.5b` di lokal —
cepat, gratis, dan tetap jalan tanpa internet. Kedua, tugas menengah + tool calling
ditangani `qwen3:1.7b` di lokal. Ketiga, tugas berat (analisis panjang, coding,
vision) diteruskan ke Ollama Cloud dengan rotasi multi API key. Semuanya saling
menjadi fallback: tier 1 gagal -> naik tier 2 -> naik tier 3; cloud gagal -> turun
ke tier 2 lokal.

## Arsitektur routing 3-tier

```
                      Input user (teks / gambar / file)
                                    |
                     +--------------v---------------+
                     |          TierRouter          |
                     |  heuristik keyword Bahasa ID |
                     |  (upgrade: classifier LLM,   |
                     |   FASE 10)                   |
                     +---+------------+----------+--+
          TIER_1         |       TIER_2 |           |   TIER_3 / VISION
    gambar? -> VISION    |              |           |   (cloud)
             +-----------+              |           +--------------+
             v                        v                          v
   +------------------+   +----------------------+   +----------------------+
   | Tier 1 - lokal   |   | Tier 2 - lokal       |   | Tier 3 - cloud       |
   | qwen2.5:0.5b     |   | qwen3:1.7b           |   | gpt-oss:120b-cloud   |
   | chat ringan      |   | tool calling         |   | kompleks + vision    |
   | (~0.5 GB RAM)    |   | (~1.7 GB RAM)        |   | (multi-key rotasi)   |
   +--------+---------+   +----------+-----------+   +----------+-----------+
            |  gagal /               |  gagal                     |  gagal
            |  jawaban kosong        |                            |  (429 habis /
            |                        |                            |   koneksi)
            +--------> escalate -----+          <--- fallback turun ke Tier 2

                        chain: 1 --> 2 --> 3
```

Total RAM lokal (tier 1 + 2) sekitar 2-3 GB + Windows sekitar 3 GB = sekitar 6 GB
- aman untuk laptop RAM 8 GB.

Aturan penting routing:

- Ada gambar (`image_url`) di pesan -> langsung model vision di cloud (bypass router).
- Sapaan/pertanyaan ringan -> Tier 1; butuh tools/ringkasan/hitung -> Tier 2;
  coding/analisis/panjang (>400 karakter) -> Tier 3.
- Tier rendah **gagal atau menyerah** (jawaban kosong) -> escalation otomatis
  ke tier berikutnya: 1 -> 2 -> 3.
- Cloud gagal (semua key 429 / koneksi mati) -> fallback **turun** ke Tier 2 lokal.
- Cloud kena 429 -> rotasi API key berikutnya (cooldown 30 menit per key).

## Status pengembangan (12 fase)

| Fase | Cakupan | Status |
|------|---------|--------|
| 1 | Core backend + git setup | **selesai** — config, key_pool, klien lokal/cloud, router 3-tier, assistant, CLI |
| 2 | Tools + agent loop | belum |
| 3 | File parser (Diceo) | belum |
| 4 | Crypto (keyring) + wizard | belum |
| 5 | UI dasar (CustomTkinter) | belum |
| 6 | Settings dialog | belum |
| 7 | System monitor | belum |
| 8 | Voice (Vosk + Piper) | belum |
| 9 | Download manager | belum |
| 10 | Router training (Colab/Kaggle) | belum |
| 11 | Packaging (PyInstaller/Nuitka) | belum |
| 12 | Polish + rilis v1.0.0 | belum |

## Struktur proyek

```
assistant-ai-hybrid/
├── .github/            CI/CD (build.yml, release.yml) + template issue/PR
├── core/               Backend: config, key_pool, tier_router, client, assistant
├── ui/                 CustomTkinter (FASE 5+)
├── voice/              STT/TTS (FASE 8+)
├── assets/             Ikon, font, aset statis
├── scripts/            Skrip build & training router
├── tests/              Test suite pytest (43 test)
└── main.py             Entry point — CLI test routing (GUI di FASE 5)
```

## Mulai (pengembangan)

Prasyarat: Python 3.11+ dan Git. Target utama Windows 11, tetapi kode tetap
cross-platform (di Linux/macOS data disimpan di `~/.assistantai`).

```powershell
git clone https://github.com/asysurya/assistant-ai-hybrid.git
cd assistant-ai-hybrid

python -m venv .venv
.venv\Scripts\activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Modul core bisa dijalankan standalone (masing-masing punya demo):
python core/config.py
python core/config_manager.py
python core/key_pool.py
python core/tier_router.py

# CLI uji routing 3-tier:
python main.py --route "buatkan kode scraper python"   # inspeksi keputusan router
python main.py --cek                                   # diagnosis kesiapan sistem
python main.py                                         # chat interaktif

# Test otomatis:
python -m pytest tests/ -v
```

## Konfigurasi & lokasi data

Semua konstanta default terpusat di `core/config.py`; preferensi user tersimpan di:

- **Windows**: `%APPDATA%\AssistantAI\` → `config.json`, `keys.enc`, `history.db`, `logs/`, `models/`
- **Linux/macOS**: `~/.assistantai/`

Untuk mencoba cepat tanpa menyimpan file, API key bisa lewat environment variable
(didukung `core/config.py`):

```powershell
setx OLLAMA_CLOUD_KEYS "key1,key2,key3"   # Windows — restart terminal setelahnya
```

Aturan keamanan: API key tidak pernah ditulis di `config.json` maupun log
(selalu disamarkan, mis. `sk-dem...5678`). Penyimpanan permanen memakai
Windows Credential Manager via `keyring` (diimplementasikan di FASE 4).

## Catatan penting

- **Diceo** (parser 11 format) belum ada di PyPI → install dari wheel lokal yang
  Anda miliki (lihat komentar di `requirements.txt`). Implementasi parser di FASE 3.
- **Build CI**: job `arm64` memakai runner `windows-11-arm` (native) karena PyInstaller
  tidak bisa cross-compile antar arsitektur; job ini sengaja dibuat `continue-on-error`
  sampai semua wheel dependensi aman untuk ARM64.
- Langkah "Build binary" di CI otomatis **aktif** begitu `assistant.spec` ada (FASE 11);
  sebelum itu CI hanya memastikan dependensi + test hijau di kedua arsitektur.
- Aplikasi ini **tidak** memakai obfuscation/anti-debug/anti-VM — aplikasi legitimate.

## Rilis

Semantic versioning (`vMAJOR.MINOR.PATCH`). Push tag `v*` → GitHub Actions membangun
exe + ZIP portable dan membuat GitHub Release otomatis (lihat
`.github/workflows/release.yml`). Model besar (voice pack, router) tidak pernah
di-commit — disebarkan sebagai aset Release terpisah.

## Lisensi

[MIT](LICENSE) — Copyright (c) 2026 [Nama Anda]
