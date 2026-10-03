# ============================================================
# AssistantAI — tests/test_key_pool.py
# ------------------------------------------------------------
# Test otomatis untuk core/key_pool.py.
#
# Jalankan:
#   python -m pytest tests/test_key_pool.py -v   (disarankan)
#   python tests/test_key_pool.py                (tanpa pytest)
#
# Trik penting: jam "palsu" (JamPalsu) di-inject ke KeyPool supaya
# test cooldown TIDAK perlu sleep — cepat dan deterministik.
# ============================================================

"""Test suite KeyPool: rotasi round-robin, cooldown 429, dan recovery."""

import sys
from pathlib import Path

# Pastikan folder root proyek bisa di-import walau file dijalankan langsung
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.key_pool import (  # noqa: E402
    KeyPool,
    KeyPoolError,
    NoKeyAvailableError,
    mask_key,
)


# ------------------------------------------------------------
# Jam palsu (menggantikan time.time())
# ------------------------------------------------------------
class JamPalsu:
    """Objek callable yang meniru time.time(), tapi bisa dimajukan manual."""

    def __init__(self, mulai: float = 1000.0):
        self.sekarang = mulai

    def __call__(self) -> float:
        return self.sekarang

    def maju(self, detik: float) -> None:
        self.sekarang += detik


KEYS = ["key-aaaa1111", "key-bbbb2222", "key-cccc3333"]


# ------------------------------------------------------------
# Test
# ------------------------------------------------------------
def test_rotasi_round_robin():
    jam = JamPalsu()
    pool = KeyPool(KEYS, cooldown_seconds=60, clock=jam)
    urutan = [pool.get_key() for _ in range(6)]
    assert urutan == KEYS + KEYS  # A,B,C,A,B,C — adil antar key


def test_key_duplikat_dan_kosong_diabaikan():
    pool = KeyPool(["key-a1111111", "", "   ", "key-a1111111", "key-b2222222"])
    assert len(pool) == 2  # kosong / spasi / duplikat dibuang otomatis


def test_cooldown_setelah_rate_limit():
    jam = JamPalsu()
    pool = KeyPool(KEYS, cooldown_seconds=60, clock=jam)
    key_pertama = pool.get_key()                    # key-aaaa1111
    assert pool.mark_rate_limited(key_pertama) is True
    assert pool.available_count() == 2              # tinggal 2 yang sehat
    key_kedua = pool.get_key()                      # harus rotasi ke key lain
    assert key_kedua != key_pertama
    jam.maju(59)                                    # masih di dalam masa 60 detik
    for _ in range(5):
        assert pool.get_key() != key_pertama        # key cooldown tidak boleh dipakai


def test_semua_key_cooldown_raise():
    jam = JamPalsu()
    pool = KeyPool(KEYS, cooldown_seconds=60, clock=jam)
    for k in KEYS:
        pool.get_key()
        pool.mark_rate_limited(k)
    try:
        pool.get_key()
    except NoKeyAvailableError as exc:
        assert 0 < exc.wait_seconds <= 60           # info tunggu harus terisi
    else:
        raise AssertionError("Harusnya raise NoKeyAvailableError")


def test_recovery_setelah_cooldown_berakhir():
    jam = JamPalsu()
    pool = KeyPool(KEYS, cooldown_seconds=60, clock=jam)
    for k in KEYS:
        pool.get_key()
        pool.mark_rate_limited(k)
    jam.maju(61)                                    # lewati masa cooldown
    assert pool.available_count() == 3
    assert pool.get_key() in KEYS


def test_mark_success_menghapus_cooldown():
    jam = JamPalsu()
    pool = KeyPool(KEYS, cooldown_seconds=60, clock=jam)
    k = pool.get_key()
    pool.mark_rate_limited(k)
    assert pool.available_count() == 2
    assert pool.mark_success(k) is True             # panggilan sukses
    assert pool.available_count() == 3              # key langsung sehat lagi


def test_mark_rate_limited_key_tak_terdaftar():
    pool = KeyPool(KEYS, cooldown_seconds=60)
    assert pool.mark_rate_limited("key-tidak-ada") is False
    assert pool.mark_success("key-tidak-ada") is False


def test_pool_kosong_raise():
    pool = KeyPool([], cooldown_seconds=60)
    try:
        pool.get_key()
    except KeyPoolError:
        pass  # pesan error diharapkan — pool memang kosong
    else:
        raise AssertionError("Pool kosong harusnya raise KeyPoolError")


def test_add_remove_key():
    pool = KeyPool(KEYS[:1], cooldown_seconds=60)
    assert pool.add_key("key-baru9999") is True
    assert pool.add_key("key-baru9999") is False    # duplikat ditolak
    assert pool.remove_key("key-baru9999") is True
    assert pool.remove_key("key-baru9999") is False # sudah tidak ada


def test_mask_key_tidak_bocorkan_key_utuh():
    tersamarkan = mask_key("sk-demo-abcdefgh12345678")
    assert tersamarkan == "sk-dem...5678"
    assert "abcdefgh" not in tersamarkan            # bagian tengah tersembunyi
    assert mask_key("pendek") == "***"
    assert mask_key("") == "***"


# ------------------------------------------------------------
# Runner tanpa pytest: python tests/test_key_pool.py
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
