# ============================================================
# AssistantAI — tests/test_config_manager.py
# ------------------------------------------------------------
# Test otomatis untuk core/config_manager.py.
#
# Jalankan:
#   python -m pytest tests/test_config_manager.py -v   (disarankan)
#   python tests/test_config_manager.py                (tanpa pytest)
#
# Semua test memakai file config di folder TEMPORARY — tidak
# menyentuh config asli di %APPDATA%/AssistantAI.
# ============================================================

"""Test suite ConfigManager: first-run, dot-notation, persist, recovery korrupt."""

import json
import sys
import tempfile
from pathlib import Path

# Pastikan folder root proyek bisa di-import walau file dijalankan langsung
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_manager import ConfigManager, _default_config, deep_merge  # noqa: E402


def buat_manager(tmp) -> ConfigManager:
    """ConfigManager dengan file config di folder sementara (aman untuk test).

    Args:
        tmp: path folder sementara (str/Path), mis. hasil dari
             `with tempfile.TemporaryDirectory() as tmp:` — bentuknya string.
    """
    return ConfigManager(config_file=Path(tmp) / "config.json")


# ------------------------------------------------------------
# Test
# ------------------------------------------------------------
def test_first_run_ketika_file_belum_ada():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        assert mgr.is_first_run is True


def test_get_set_dot_notation_dan_persist():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        assert mgr.get("ui.theme") == "auto"                  # default dari core/config.py
        mgr.set("ui.theme", "light")
        mgr.set("hotkey.toggle", "ctrl+alt+m")

        mgr2 = buat_manager(tmp)                              # simulasi aplikasi restart
        assert mgr2.get("ui.theme") == "light"
        assert mgr2.get("hotkey.toggle") == "ctrl+alt+m"
        assert mgr2.get("ollama_cloud.models.chat") == "gpt-oss:120b-cloud"


def test_get_default_bila_jalur_tidak_ada():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        assert mgr.get("tidak.ada.jalur", "fallback") == "fallback"


def test_mark_first_run_done_persist():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        mgr.mark_first_run_done()
        assert mgr.is_first_run is False
        assert buat_manager(tmp).is_first_run is False        # tetap setelah reload


def test_json_parsial_tetap_dilengkapi_default():
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "config.json"
        file.write_text(json.dumps({"ui": {"theme": "dark"}}), encoding="utf-8")
        mgr = ConfigManager(config_file=file)
        assert mgr.get("ui.theme") == "dark"                  # dari file user
        assert mgr.get("hotkey.toggle") == "ctrl+space"       # dari default
        assert mgr.get("app.version") == "0.1.0"              # dari default


def test_file_corrupt_fallback_default_dan_backup():
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "config.json"
        file.write_text("{ ini bukan json valid !!!", encoding="utf-8")
        mgr = ConfigManager(config_file=file)
        # Graceful degradation: pakai default + file korrupt dibackup
        assert mgr.get("ui.theme") == "auto"
        backups = list(Path(tmp).glob("config.corrupt-*.json"))
        assert len(backups) == 1


def test_save_atomik_tidak_meninggalkan_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        mgr.set("voice.enabled", False)
        assert list(Path(tmp).glob("*.tmp")) == []            # .tmp sudah di-replace
        assert (Path(tmp) / "config.json").exists()
        assert mgr.get("voice.enabled") is False


def test_deep_merge():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    over = {"a": {"b": 10}, "e": 4}
    hasil = deep_merge(base, over)
    assert hasil == {"a": {"b": 10, "c": 2}, "d": 3, "e": 4}
    assert base["a"]["b"] == 1                                # base tidak dimutasi


def test_reset_to_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = buat_manager(tmp)
        mgr.set("ui.theme", "light")
        mgr.reset_to_defaults()
        assert mgr.get("ui.theme") == "auto"

        mgr.set("ui.theme", "light")
        mgr.reset_to_defaults(delete_file=True)
        assert not (Path(tmp) / "config.json").exists()
        assert mgr.is_first_run is True                       # kembali seperti fresh install


def test_default_config_tidak_menyimpan_api_key():
    """Keamanan: struktur default TIDAK boleh punya field API key/secret.

    Catatan: "max_tokens" (batas token model) adalah setting yang sah dan
    TIDAK termasuk kategori ini — yang dicek adalah nama field kredensial.
    """
    terlarang = {"api_key", "apikey", "keys", "secret", "password", "access_token"}

    def kumpulkan_leaf(d: dict, awalan: str = "") -> list[str]:
        hasil = []
        for k, v in d.items():
            jalur = f"{awalan}.{k}" if awalan else k
            if isinstance(v, dict):
                hasil.extend(kumpulkan_leaf(v, jalur))
            else:
                hasil.append(jalur)
        return hasil

    for jalur in kumpulkan_leaf(_default_config()):
        leaf = jalur.split(".")[-1].lower()
        assert leaf not in terlarang, f"Field kredensial terdeteksi: {jalur}"


# ------------------------------------------------------------
# Runner tanpa pytest: python tests/test_config_manager.py
# ------------------------------------------------------------
if __name__ == "__main__":
    daftar = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    gagal = 0
    for fn in daftar:
        try:
            fn()
            print(f"LULUS  : {fn.__name__}")
        except AssertionError as exc:
            gagal += 1
            print(f"GAGAL  : {fn.__name__} -> {exc}")
    print(f"\n{len(daftar) - gagal}/{len(daftar)} test lulus.")
    sys.exit(1 if gagal else 0)
