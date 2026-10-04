# Changelog

Semua perubahan penting pada proyek ini akan didokumentasikan di file ini.

Format berdasarkan [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
dan proyek ini mengikuti [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Riwayat chat SQLite (core/history.py): sesi berjudul otomatis, pesan + metadata tier/model/durasi, pencarian keyword aman (escape LIKE), ekspor Markdown/JSON/TXT, statistik per tier — mode WAL, thread-safe, tahan crash
- Toolkit bawaan (core/tools.py) dengan registry + skema OpenAI tool calling: get_time, hitung (kalkulator AST aman + proteksi DoS), baca_file, tulis_file (butuh konfirmasi), daftar_folder, todo — semua tool file terkurung di folder workspace
- Agent loop tool-calling (core/agent.py): maks AGENT_MAX_ITERATIONS iterasi, argumen JSON rusak dikirim balik agar model mengoreksi diri, callback konfirmasi untuk tool penulis (default: tolak)
- chat_detail() pada klien lokal & cloud — kembalikan teks + tool_calls (ReplyDetail) tanpa mengubah perilaku chat() lama
- Integrasi agent ke Assistant: Tier 2 & Tier 3 diproses via tool-calling bila tools aktif; Tier 1 tetap chat polos; jejak tools terakhir tersedia untuk CLI/UI
- Pembersih blok <think>…</think> jawaban model Qwen3
- CLI main.py: chat kini berkonteks (N pesan terakhir dari SQLite), sesi tersimpan otomatis, perintah baru /baru /riwayat /sesi /cari /export /alat + flag --riwayat; log INFO/WARNING hanya ke file agar terminal bersih
- Test suite FASE 2: history (14), tools (15), agent+integrasi (15) — total 87 test

### Changed
- KONSOLIDASI MODEL (update arsitektur Qwen3.5-2B): satu model lokal `qwen3.5:2b`
  kini menangani Tier 1 + Tier 2 + Vision sekaligus (lulus 7/7 test: tool calling
  5/5 JSON valid, chat natural tanpa false tool call, ambiguous OK) — `qwen2.5:0.5b`,
  `qwen3:1.7b`, dan `qwen2.5:1.5b` tidak lagi dibutuhkan; cukup `ollama pull qwen3.5:2b`
- Vision pindah dari cloud (`gemma4:31b-cloud`) ke model lokal `qwen3.5:2b` —
  konstanta baru `cfg.VISION_MODEL`, jalur vision kini lewat LocalClient (hemat
  kuota key cloud, tetap jalan offline)
- Cloud disederhanakan jadi SATU model: `gpt-oss:120b-cloud` untuk chat + coding
  berat; `qwen3-coder:480b-cloud` dihapus dari konfigurasi (coding menengah cukup
  oleh model lokal, coding berat masuk Tier 3)
- Estimasi RAM model lokal turun ~3.5 GB -> ~2 GB (total ~5 GB termasuk Windows)
- Test fake assistant mendukung skenario urutan panggilan (tier 1 & 2 kini model sama)

### Fixed
- Bug yang diperbaiki

---

## [1.0.0] - 2026-10-03

### Added
- Rilis pertama
- Floating button dengan hotkey Ctrl+Space
- Chat teks dengan bubble UI + markdown support
- Voice input (Vosk STT) dan output (Piper TTS)
- Upload file: PDF, DOCX, XLSX, PPTX, TXT, kode
- Upload gambar → routing ke vision model
- Tool calling: todos, read_file, web_search, get_time
- System monitor: CPU, RAM, disk, network, battery
- Settings dialog dengan 7 kategori
- First-run wizard 5 langkah
- Model download on-demand (voice, router)
- Hybrid routing: lokal (Qwen2.5:1.5b) + cloud (gpt-oss:120b)
- Multi API key rotation dengan cooldown
- Binary .exe ~40 MB + model terpisah
- GitHub Actions untuk auto-build & release

### Security
- API key disimpan di Windows Credential Manager via keyring

---

## [0.9.0] - 2026-09-25

### Added
- Beta version untuk internal testing
- Core backend: config, key_pool, clients
- Router heuristik berbasis keyword

### Fixed
- Bug pada parsing config saat first-run

---

## [0.1.0] - 2026-09-15

### Added
- Proof of concept
- Chat CLI sederhana
- Koneksi ke Ollama lokal
