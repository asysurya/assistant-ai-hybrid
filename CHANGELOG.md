# Changelog

Semua perubahan penting pada proyek ini akan didokumentasikan di file ini.

Format berdasarkan [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
dan proyek ini mengikuti [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Fitur yang sedang dalam pengembangan

### Changed
- Perubahan pada fitur yang sudah ada

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
