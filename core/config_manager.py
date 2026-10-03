# ============================================================
# AssistantAI — core/config_manager.py
# ------------------------------------------------------------
# Jembatan antara konstanta default (core/config.py) dan
# preferensi user yang tersimpan di config.json.
#
# Fitur:
#   - Deteksi first-run (config.json belum ada -> tampilkan wizard, FASE 4)
#   - Deep merge: config.json parsial tetap dilengkapi nilai default
#   - Notasi titik: get("ui.theme"), set("hotkey.toggle", "ctrl+alt+m")
#   - Simpan ATOMIK (tulis ke *.tmp lalu os.replace) -> config tidak
#     pernah "setengah jadi" walau proses mati mendadak
#   - Recovery: config.json korrupt -> dibackup otomatis, pakai default
#     (graceful degradation — aplikasi tetap jalan)
#   - setup_logging(): log rotasi ke %APPDATA%/AssistantAI/logs/
#
# CATATAN KEAMANAN: API key tidak boleh lewat sini — key dikelola
# oleh keyring (FASE 4) + core/key_pool.py.
# ============================================================

"""Manajer konfigurasi runtime AssistantAI (config.json).

Cara test cepat:
    python core/config_manager.py
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung: python core/config_manager.py
    import config as cfg  # type: ignore

logger = logging.getLogger("assistant.config_manager")

# Versi skema config.json — naikkan bila struktur berubah besar (migrasi di FASE 4+)
CONFIG_SCHEMA_VERSION = 1


# ------------------------------------------------------------
# Config default (dibangun dari core/config.py)
# ------------------------------------------------------------
def _default_config() -> dict:
    """Bangun struktur config default FRESH setiap kali dipanggil.

    Sengaja berupa fungsi (bukan konstanta modul) supaya setiap pemanggil
    mendapat salinannya sendiri dan tidak ada mutasi tak disengaja.
    """
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "app": {
            "name": cfg.APP_NAME,
            "version": cfg.APP_VERSION,
            "first_run_completed": False,   # False -> wizard tampil (FASE 4)
        },
        "ollama_cloud": {
            "base_url": cfg.OLLAMA_CLOUD_BASE_URL,
            "models": dict(cfg.CLOUD_MODELS),
        },
        "ollama_local": {
            "base_url": cfg.OLLAMA_LOCAL_BASE_URL,
            "model": cfg.OLLAMA_LOCAL_MODEL,
            "timeout_seconds": cfg.OLLAMA_LOCAL_TIMEOUT,
            "max_tokens": cfg.OLLAMA_LOCAL_MAX_TOKENS,
        },
        "router": {
            "model": cfg.ROUTER_MODEL,
            "fallback": cfg.ROUTER_FALLBACK,
            "local_max_prompt_length": cfg.LOCAL_MAX_PROMPT_LENGTH,
        },
        "key_pool": {
            "cooldown_seconds": cfg.KEY_COOLDOWN_SECONDS,
        },
        "voice": {
            "enabled": cfg.VOICE_ENABLED,
            "stt_engine": cfg.STT_ENGINE,
            "tts_engine": cfg.TTS_ENGINE,
            "tts_voice": cfg.TTS_VOICE,
        },
        "ui": {
            "theme": cfg.THEME,
            "floating_button": cfg.FLOATING_BUTTON,
            "start_minimized": cfg.START_MINIMIZED,
        },
        "hotkey": {
            "toggle": cfg.HOTKEY_TOGGLE,
            "voice": cfg.HOTKEY_VOICE,
        },
        "agent": {
            "max_iterations": cfg.AGENT_MAX_ITERATIONS,
        },
        "files": {
            "max_tokens_chunk": cfg.FILE_MAX_TOKENS,
        },
        "logging": {
            "level": cfg.LOG_LEVEL,
        },
        # PENTING: sengaja TIDAK ada field API key di sini.
        # Key disimpan di Windows Credential Manager via keyring (FASE 4).
    }


def deep_merge(base: dict, override: dict) -> dict:
    """Gabungkan `override` di atas `base` secara rekursif (deep merge).

    Aturan:
      - Key hanya ada di base    -> dipertahankan (nilai default tetap hidup)
      - Key hanya ada di override -> ditambahkan
      - Dua-duanya dict -> digabung rekursif; selain itu -> override menang
      - `base` TIDAK dimutasi (dipakai deep copy di awal)

    Ini yang membuat config.json user yang hanya berisi sebagian key
    (mis. {"ui": {"theme": "dark"}}) tetap mendapat seluruh nilai default.
    """
    hasil = copy.deepcopy(base)
    for kunci, nilai in override.items():
        if isinstance(nilai, dict) and isinstance(hasil.get(kunci), dict):
            hasil[kunci] = deep_merge(hasil[kunci], nilai)
        else:
            hasil[kunci] = copy.deepcopy(nilai)
    return hasil


# ------------------------------------------------------------
# ConfigManager
# ------------------------------------------------------------
class ConfigManager:
    """Baca-tulis config.json dengan default aman + logging rotasi.

    Contoh:
        mgr = ConfigManager()
        if mgr.is_first_run:
            ...  # tampilkan wizard (FASE 4)
        tema = mgr.get("ui.theme", "auto")
        mgr.set("hotkey.toggle", "ctrl+alt+m")
    """

    def __init__(self, config_file: Path | None = None):
        """
        Args:
            config_file: lokasi config.json. Default: %APPDATA%/AssistantAI/config.json.
                Parameter ini juga memudahkan test (pakai file sementara).
        """
        self.config_file = Path(config_file) if config_file else cfg.CONFIG_FILE
        self._lock = threading.RLock()          # aman dipakai lintas thread UI
        self._data: dict = _default_config()
        self.load()

    # ---------- properti dasar ----------
    @property
    def is_first_run(self) -> bool:
        """True bila config.json belum ada ATAU wizard belum pernah diselesaikan."""
        return (not self.config_file.exists()) or \
               (self.get("app.first_run_completed") is not True)

    def mark_first_run_done(self) -> None:
        """Panggil setelah wizard 5 langkah selesai (FASE 4)."""
        self.set("app.first_run_completed", True)

    # ---------- load / save ----------
    def load(self) -> None:
        """Muat config.json dari disk, lalu deep-merge di atas nilai default.

        Bila file korrupt (bukan JSON valid / bukan dict): file dipindah ke
        backup `config.corrupt-<timestamp>.json`, aplikasi memakai nilai
        default, dan pesan error jelas dicatat ke log. Aplikasi TETAP jalan.
        """
        with self._lock:
            self._data = _default_config()
            if not self.config_file.exists():
                logger.info("Config belum ada (first-run): %s", self.config_file)
                return
            try:
                mentah = json.loads(self.config_file.read_text(encoding="utf-8"))
                if not isinstance(mentah, dict):
                    raise ValueError("Isi config.json bukan objek JSON (dict).")
            except (json.JSONDecodeError, ValueError) as exc:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup = self.config_file.with_name(f"config.corrupt-{stamp}.json")
                try:
                    shutil.move(str(self.config_file), str(backup))
                    logger.error(
                        "config.json korrupt (%s). Dibackup ke: %s. Memakai nilai default.",
                        exc, backup,
                    )
                except OSError as err:
                    logger.error(
                        "config.json korrupt (%s) dan GAGAL dibackup: %s", exc, err
                    )
                return
            versi = mentah.get("schema_version")
            if versi is not None and versi != CONFIG_SCHEMA_VERSION:
                logger.warning(
                    "Schema config (%s) berbeda dari yang didukung (%s). "
                    "Migrasi akan ditangani di FASE 4.",
                    versi, CONFIG_SCHEMA_VERSION,
                )
            self._data = deep_merge(self._data, mentah)
            logger.debug("Config dimuat: %s", self.config_file)

    def save(self) -> None:
        """Simpan config secara ATOMIK: tulis ke *.tmp lalu os.replace().

        os.replace() atomik di Windows maupun POSIX, jadi config.json tidak
        akan pernah rusak walau listrik/proses mati di tengah penulisan.
        """
        with self._lock:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_file.with_name(self.config_file.name + ".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self.config_file))
            logger.debug("Config disimpan: %s", self.config_file)

    # ---------- akses nilai ----------
    def get(self, path: str, default: Any = None) -> Any:
        """Ambil nilai dengan notasi titik, mis. get("ui.theme", "auto").

        Args:
            path: jalur key, dipisah titik ("ollama_cloud.models.chat").
            default: nilai bila jalur tidak ditemukan.
        """
        with self._lock:
            node: Any = self._data
            for bagian in path.split("."):
                if isinstance(node, dict) and bagian in node:
                    node = node[bagian]
                else:
                    return default
            return node

    def set(self, path: str, value: Any, save: bool = True) -> None:
        """Set nilai dengan notasi titik, mis. set("hotkey.toggle", "ctrl+alt+m").

        Args:
            save: langsung simpan ke disk (default True). Set False bila ingin
                mengubah banyak nilai lalu memanggil save() sekali di akhir.
        """
        with self._lock:
            node = self._data
            bagian2 = path.split(".")
            for bagian in bagian2[:-1]:
                node = node.setdefault(bagian, {})
            node[bagian2[-1]] = copy.deepcopy(value)
        if save:
            self.save()

    def as_dict(self) -> dict:
        """Salinan config lengkap (deep copy) — aman untuk dibaca/diubah pemanggil."""
        with self._lock:
            return copy.deepcopy(self._data)

    def reset_to_defaults(self, delete_file: bool = False) -> None:
        """Kembalikan config ke nilai default.

        Args:
            delete_file: True -> config.json juga dihapus (persis fresh
                install; first-run akan terdeteksi lagi oleh wizard).
        """
        with self._lock:
            self._data = _default_config()
            if delete_file and self.config_file.exists():
                self.config_file.unlink()
                logger.info("config.json dihapus — kembali seperti fresh install.")
            else:
                self.save()

    def lokasi_berkas(self) -> dict[str, Path]:
        """Info lokasi semua berkas data (dipakai Settings > Path, FASE 6)."""
        return {
            "config.json": self.config_file,
            "keys.enc": cfg.KEYS_FILE,
            "history.db": cfg.HISTORY_DB,
            "logs/": cfg.LOG_DIR,
            "models/": cfg.MODEL_DIR,
        }

    # ---------- logging ----------
    def setup_logging(self, level: str | None = None) -> logging.Logger:
        """Pasang logging rotasi untuk logger root paket ("assistant").

        - Berkas : %APPDATA%/AssistantAI/logs/assistant.log
                   (rotasi ~1 MB, maksimal 3 berkas cadangan)
        - Konsol : ikut tercetak saat dijalankan dari terminal

        Aman dipanggil berkali-kali — handler hanya dipasang sekali.
        """
        cfg.ensure_app_dirs()
        logger_root = logging.getLogger("assistant")
        if logger_root.handlers:            # sudah pernah dipasang -> jangan dobel
            return logger_root
        level_nama = str(level or self.get("logging.level", cfg.LOG_LEVEL) or "INFO").upper()
        logger_root.setLevel(getattr(logging, level_nama, logging.INFO))
        format_ = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler_berkas = logging.handlers.RotatingFileHandler(
            cfg.LOG_DIR / "assistant.log",
            maxBytes=cfg.LOG_MAX_BYTES,
            backupCount=cfg.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler_berkas.setFormatter(format_)
        handler_konsol = logging.StreamHandler()
        handler_konsol.setFormatter(format_)
        logger_root.addHandler(handler_berkas)
        logger_root.addHandler(handler_konsol)
        logger_root.propagate = False
        return logger_root


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/config_manager.py")
    print("=" * 60)
    cfg.ensure_app_dirs()
    print(f"Lokasi config : {cfg.CONFIG_FILE}")

    mgr = ConfigManager()
    print(f"First run?    : {mgr.is_first_run}")
    print(f"ui.theme awal : {mgr.get('ui.theme')}")
    print(f"model chat    : {mgr.get('ollama_cloud.models.chat')}")
    print(f"hotkey toggle : {mgr.get('hotkey.toggle')}")

    # --- ubah nilai, simpan, buktikan persist setelah reload ---
    tema_awal = mgr.get("ui.theme")
    mgr.set("ui.theme", "dark")
    print(f"\nset('ui.theme', 'dark') -> tersimpan di {mgr.config_file}")

    mgr2 = ConfigManager()  # objek baru = simulasi aplikasi di-restart
    print(f"Setelah reload: ui.theme = {mgr2.get('ui.theme')}")
    assert mgr2.get("ui.theme") == "dark", "Perubahan config harus persist!"

    # kembalikan nilai awal supaya demo bisa diulang tanpa efek sisa
    mgr2.set("ui.theme", tema_awal)
    print(f"Nilai awal dikembalikan: ui.theme = {mgr2.get('ui.theme')}")

    # --- logging rotasi ---
    log = mgr2.setup_logging()
    log.info("Log INFO contoh dari demo config_manager")
    log.warning("Log WARNING contoh — cek isi folder logs/")
    print(f"\nLog ditulis ke: {cfg.LOG_DIR / 'assistant.log'}")
    print("\nDemo selesai.")
