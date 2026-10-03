# AssistantAI — Asisten AI Hybrid Desktop

Asisten AI pribadi untuk **Windows 11 ARM64** (target uji: ThinkPad E580, RAM 8GB, SSD 239GB).
Chat teks + suara, analisis file, tool calling, system monitor — dengan **routing hybrid**
antara model **lokal** (Ollama, offline & gratis) dan model **cloud** (Ollama Cloud).

> Proyek dibangun **bertahap per fase** (12 fase). Setiap fase wajib lulus test
> sebelum lanjut ke fase berikutnya. Status terkini: **FASE 1 — batch pertama
> (fondasi config, key pool, CI/CD)**.

| | |
|---|---|
| Build CI | [![Build](https://github.com/asysurya/assistant-ai-hybrid/actions/workflows/build.yml/badge.svg)](https://github.com/asysurya/assistant-ai-hybrid/actions/workflows/build.yml) |
| Lisensi | MIT — lihat [LICENSE](LICENSE) |
| Python | 3.11+ |

> Catatan: ganti `[Nama Anda]` pada LICENSE dengan nama Anda.

## Kenapa hybrid?

Laptop 8GB RAM tidak realistis menjalankan model besar secara lokal, tetapi memakai
cloud untuk semua hal juga boros dan bergantung internet. Strategi hybrid memberi
tiga keuntungan sekaligus. Pertama, tugas ringan (pertanyaan singkat, draft teks)
ditangani `qwen2.5:1.5b` di lokal — cepat, gratis, dan tetap jalan tanpa internet.
Kedua, tugas berat (analisis panjang, coding, vision) diteruskan ke Ollama Cloud
dengan rotasi multi API key agar tidak mudah kena rate limit. Ketiga, keduanya
saling menjadi fallback: lokal error → cloud, cloud 429 → rotasi key → lokal.

## Arsitektur routing

```
                        Input user (teks / gambar / file)
                                      |
                       +--------------v---------------+
                       |            Router            |
                       |  (Qwen3-0.6B fine-tuned ID,  |
                       |   fallback: heuristik)       |
                       +---+-------------+----------+-+
             LOCAL          |             |           |   CLOUD_VISION
                 +----------+             |           +-------------+
                 v                       v                         v
       +------------------+   +----------------------+   +------------------+
       |  Ollama lokal    |   |  Ollama Cloud        |   |  Ollama Cloud    |
       |  qwen2.5:1.5b    |   |  gpt-oss:120b-cloud  |   |  gemma4:31b      |
       |  (offline, gratis)|  |  (multi-key rotasi)  |   |  (vision)        |
       +--------+---------+   +----------+-----------+   +--------+---------+
                |                        |                        |
                |  error / RAM penuh     | 429 -> rotasi key      |
                +-------> fallback <-----+------------------------+
```

Aturan penting routing:

- Ada gambar (`image_url`) di pesan → langsung `CLOUD_VISION`, tanpa menunggu router.
- Prompt pendek & sederhana → `LOCAL`; panjang/kompleks → `CLOUD_TEXT`.
- Cloud kena 429 → rotasi API key berikutnya (cooldown 30 menit per key).
- Semua key sedang cooldown ATAU lokal error → fallback otomatis ke lane lain.

## Status pengembangan (12 fase)

| Fase | Cakupan | Status |
|------|---------|--------|
| 1 | Core backend + git setup | **berjalan** — config, config_manager, key_pool selesai; client/router/assistant menyusul |
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
├── core/               Backend: config, key_pool, client, router, tools
├── ui/                 CustomTkinter (FASE 5+)
├── voice/              STT/TTS (FASE 8+)
├── assets/             Ikon, font, aset statis
├── scripts/            Skrip build & training router
├── tests/              Test suite pytest
└── main.py             Entry point (dibuat di lanjutan FASE 1 / FASE 5)
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

# Modul FASE 1 bisa dijalankan standalone (masing-masing punya demo):
python core/config.py
python core/config_manager.py
python core/key_pool.py

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
