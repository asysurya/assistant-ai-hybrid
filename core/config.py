# ============================================================
# AssistantAI — core/config.py
# ------------------------------------------------------------
# SATU-SATUNYA tempat mendefinisikan konstanta & path aplikasi.
#
# Prinsip penting:
#   1. Nilai default hanya didefinisikan DI SINI (single source of truth).
#      Preferensi user yang bisa berubah disimpan di config.json
#      (dikelola core/config_manager.py), dengan file ini sebagai
#      sumber nilai default-nya.
#   2. API key TIDAK PERNAH disimpan di file ini maupun di config.json.
#      Key dikelola via keyring / Windows Credential Manager (FASE 4)
#      dan dipakai oleh core/key_pool.py.
#   3. Import file ini TIDAK boleh menimbulkan side-effect (tidak
#      membuat folder, tidak menulis file). Pembuatan folder dilakukan
#      eksplisit lewat ensure_app_dirs().
#
# Target utama   : Windows 11 ARM64 (laptop RAM 8GB).
# Cross-platform : di Linux/macOS otomatis fallback ke ~/.assistantai.
# ============================================================

"""Konstanta, path, dan konfigurasi terpusat AssistantAI.

Cara test cepat:
    python core/config.py
"""

from __future__ import annotations

import os
from pathlib import Path

# ------------------------------------------------------------
# 1. Identitas aplikasi
# ------------------------------------------------------------
APP_NAME = "AssistantAI"
APP_VERSION = "0.1.0"   # ikuti Semantic Versioning, sinkron dengan CHANGELOG.md
APP_AUTHOR = "AssistantAI Contributors"
APP_DESCRIPTION = "Asisten AI hybrid desktop (lokal + cloud) untuk Windows"

# Ganti USERNAME dengan username GitHub Anda setelah repo dibuat
GITHUB_REPO_URL = "https://github.com/USERNAME/assistant-ai-hybrid"

# ------------------------------------------------------------
# 2. Direktori data aplikasi
# ------------------------------------------------------------
def _get_app_dir() -> Path:
    """Tentukan folder data aplikasi lintas platform.

    Windows    : %APPDATA%/AssistantAI
                 (mis. C:\\Users\\<nama>\\AppData\\Roaming\\AssistantAI)
    Linux/macOS: ~/.assistantai  (fallback bila APPDATA tidak ada)
    """
    appdata = os.environ.get("APPDATA")  # hanya ada di Windows
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


APP_DIR: Path = _get_app_dir()

# Semua "anak" folder/berkas di bawah APP_DIR — cukup ubah di satu tempat
# kalau suatu saat lokasi perlu dipindah.
CONFIG_FILE: Path = APP_DIR / "config.json"   # preferensi user (NON-rahasia)
KEYS_FILE: Path = APP_DIR / "keys.enc"        # API key terenkripsi (FASE 4)
HISTORY_DB: Path = APP_DIR / "history.db"     # SQLite riwayat chat (FASE 2+)
LOG_DIR: Path = APP_DIR / "logs"              # log rotasi (assistant.log)
MODEL_DIR: Path = APP_DIR / "models"          # model on-demand (vosk/piper/router)

# ------------------------------------------------------------
# 3. Ollama Cloud (model besar, via https://ollama.com/v1)
# ------------------------------------------------------------
OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"

# Daftar key diisi saat RUNTIME (wizard / settings / env), BUKAN hardcode di sini!
# Cara cepat lewat environment variable (beberapa key dipisah koma):
#   Windows : setx OLLAMA_CLOUD_KEYS "key1,key2,key3"
#   Linux   : export OLLAMA_CLOUD_KEYS=key1,key2,key3
OLLAMA_CLOUD_KEYS: list[str] = []

# Model per peran (role) — bisa diganti lewat Settings > Model (FASE 6)
CLOUD_MODELS: dict[str, str] = {
    "chat":   "gpt-oss:120b-cloud",       # chat & analisis teks berat
    "coding": "qwen3-coder:480b-cloud",   # tugas coding
    "vision": "gemma4:31b-cloud",         # analisis gambar
}

# ------------------------------------------------------------
# 4. Ollama lokal (offline, hemat RAM)
# ------------------------------------------------------------
OLLAMA_LOCAL_BASE_URL = "http://localhost:11434/v1"
OLLAMA_LOCAL_MODEL = "qwen2.5:1.5b"    # kecil (~1.5GB), cocok untuk RAM 8GB
OLLAMA_LOCAL_TIMEOUT = 90              # detik
OLLAMA_LOCAL_MAX_TOKENS = 1024

# ------------------------------------------------------------
# 5. Router (pilih lane: LOCAL / CLOUD_TEXT / CLOUD_VISION)
# ------------------------------------------------------------
ROUTER_MODEL = "qwen3-router-id"       # Qwen3-0.6B fine-tuned Bahasa ID (FASE 10)
ROUTER_FALLBACK = "heuristic"          # model router belum ada -> heuristik keyword
LOCAL_MAX_PROMPT_LENGTH = 400          # prompt lebih panjang dari ini -> cloud
KEY_COOLDOWN_SECONDS = 1800            # 30 menit; key yang kena 429 di-"istirahatkan"

# ------------------------------------------------------------
# 6. Voice (STT/TTS) — model di-download on-demand (FASE 8-9)
# ------------------------------------------------------------
STT_ENGINE = "vosk"
TTS_ENGINE = "piper"                   # fallback otomatis ke pyttsx3 (Windows SAPI)
TTS_VOICE = "id_ID-news_tts-medium"
VOICE_ENABLED = True
VOICE_IDLE_UNLOAD_SECONDS = 300        # unload model suara setelah 5 menit idle

# ------------------------------------------------------------
# 7. UI
# ------------------------------------------------------------
THEME = "auto"            # "light" | "dark" | "auto" (ikut tema Windows)
FLOATING_BUTTON = True    # floating button 56x56, bisa di-drag
START_MINIMIZED = False   # mulai tersembunyi di system tray
WINDOW_MIN_WIDTH = 420
WINDOW_MIN_HEIGHT = 560

# ------------------------------------------------------------
# 8. Hotkey global
# ------------------------------------------------------------
HOTKEY_TOGGLE = "ctrl+space"      # toggle jendela utama
HOTKEY_VOICE = "ctrl+shift+v"     # mulai/stop rekam suara

# ------------------------------------------------------------
# 9. Agent & tools (FASE 2)
# ------------------------------------------------------------
AGENT_MAX_ITERATIONS = 5          # batas iterasi loop tool-calling
FILE_MAX_TOKENS = 4000            # file lebih besar dari ini -> chunk + summarize

# ------------------------------------------------------------
# 10. Logging (rotasi agar SSD tidak penuh)
# ------------------------------------------------------------
LOG_LEVEL = "INFO"                # DEBUG | INFO | WARNING | ERROR
LOG_MAX_BYTES = 1_000_000         # ~1 MB per berkas log
LOG_BACKUP_COUNT = 3              # simpan maks. 3 berkas lama (assistant.log.1 dst.)


# ------------------------------------------------------------
# Fungsi bantu
# ------------------------------------------------------------
def ensure_app_dirs() -> list[Path]:
    """Buat APP_DIR + subfolder (logs/, models/) bila belum ada.

    Dipanggil eksplisit oleh aplikasi utama, wizard, dan ConfigManager —
    BUKAN saat modul ini di-import (prinsip: import tanpa side-effect).

    Returns:
        Daftar folder yang BARU dibuat (list kosong bila semua sudah ada).
    """
    dibuat: list[Path] = []
    for folder in (APP_DIR, LOG_DIR, MODEL_DIR):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            dibuat.append(folder)
    return dibuat


def get_cloud_keys_from_env() -> list[str]:
    """Baca API key dari environment variable (cara cepat tanpa simpan file).

    Didukung:
      - OLLAMA_CLOUD_KEYS : beberapa key dipisah koma, mis. "key1,key2"
      - OLLAMA_API_KEY    : satu key saja

    Returns:
        List key yang sudah dibersihkan (tanpa spasi, tanpa entri kosong).
    """
    mentah = os.environ.get("OLLAMA_CLOUD_KEYS", "") or os.environ.get("OLLAMA_API_KEY", "")
    return [k.strip() for k in mentah.split(",") if k.strip()]


def ringkasan() -> str:
    """Ringkasan konfigurasi utama untuk debugging/demo (tanpa data rahasia)."""
    return "\n".join([
        f"{APP_NAME} v{APP_VERSION}",
        f"  Folder data : {APP_DIR}",
        f"  Config      : {CONFIG_FILE}",
        f"  Log         : {LOG_DIR}",
        f"  Model       : {MODEL_DIR}",
        f"  Cloud URL   : {OLLAMA_CLOUD_BASE_URL}",
        f"  Model cloud : {', '.join(CLOUD_MODELS.values())}",
        f"  Lokal       : {OLLAMA_LOCAL_MODEL} @ {OLLAMA_LOCAL_BASE_URL}",
        f"  Router      : {ROUTER_MODEL} (fallback: {ROUTER_FALLBACK})",
        f"  Voice       : STT={STT_ENGINE}, TTS={TTS_ENGINE} ({TTS_VOICE})",
        f"  Theme       : {THEME} | Hotkey: {HOTKEY_TOGGLE} / {HOTKEY_VOICE}",
    ])


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/config.py — konstanta & path")
    print("=" * 60)
    print(ringkasan())

    print("\nMembuat folder aplikasi (bila belum ada)...")
    baru = ensure_app_dirs()
    if baru:
        for p in baru:
            print(f"  dibuat : {p}")
    else:
        print("  semua folder sudah ada.")

    env_keys = get_cloud_keys_from_env()
    print(f"\nAPI key dari environment : {len(env_keys)} key "
          f"(set OLLAMA_CLOUD_KEYS untuk mencoba)")
    print("\nDemo selesai. Tidak ada config yang ditulis "
          "(itu tugas core/config_manager.py).")
