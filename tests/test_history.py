# ============================================================
# AssistantAI — tests/test_history.py
# ------------------------------------------------------------
# Test RIWAYAT CHAT SQLite — DB sementara per test (tmp),
# tidak menyentuh history.db milik user.
#
# Skenario:
#   - buat sesi, simpan & baca pesan (urutan terjaga)
#   - judul otomatis dari pesan user pertama
#   - format konteks OpenAI untuk model (role user/assistant saja)
#   - daftar sesi + jumlah pesan + urutan terbaru
#   - pencarian keyword (case-insensitive + escape LIKE)
#   - hapus sesi = pesan ikut terhapus (CASCADE)
#   - ekspor md / json / txt + format tak dikenal -> ValueError
#   - data persist setelah koneksi ditutup & dibuka ulang
#   - statistik per tier
#   - validasi: peran salah / isi kosong / sesi tak ada -> ValueError
#
# Jalankan:
#   python -m pytest tests/test_history.py -v
#   python tests/test_history.py
# ============================================================

"""Test suite ChatHistory: sesi, pesan, cari, ekspor, statistik."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.history import JUDUL_DEFAULT, ChatHistory  # noqa: E402


def buat_history() -> tuple[ChatHistory, Path]:
    """ChatHistory + folder tmp (folder TIDAK dihapus otomatis di sini;
    cukup aman karena tmp sistem). Dipasangkan dengan helper tutup()."""
    tmp = tempfile.mkdtemp(prefix="assistantai-test-")
    db = Path(tmp) / "riwayat-test.db"
    return ChatHistory(db), db


# ------------------------------------------------------------
# Sesi & pesan dasar
# ------------------------------------------------------------
def test_buat_sesi_dan_simpan_baca_pesan():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    assert sesi >= 1
    assert riwayat.get_sesi(sesi)["judul"] == JUDUL_DEFAULT

    riwayat.tambah_pesan(sesi, "user", "halo dunia")
    riwayat.tambah_pesan(sesi, "assistant", "Hai! Ada yang bisa dibantu?",
                         tier="tier1", durasi_ms=123.4)

    pesan = riwayat.pesan_sesi(sesi)
    assert len(pesan) == 2
    assert pesan[0]["peran"] == "user" and pesan[0]["isi"] == "halo dunia"
    assert pesan[1]["peran"] == "assistant"
    assert pesan[1]["tier"] == "tier1"
    assert pesan[1]["durasi_ms"] == 123.4
    riwayat.tutup()


def test_urutan_pesan_terjaga_lama_ke_baru():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    for i in range(5):
        riwayat.tambah_pesan(sesi, "user", f"pesan {i}")
    isi = [p["isi"] for p in riwayat.pesan_sesi(sesi)]
    assert isi == [f"pesan {i}" for i in range(5)]      # urut lama -> baru
    riwayat.tutup()


def test_judul_otomatis_dari_pesan_user_pertama():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "assistant", "Hai!")     # assistant dulu: tak mengubah judul
    assert riwayat.get_sesi(sesi)["judul"] == JUDUL_DEFAULT
    riwayat.tambah_pesan(sesi, "user", "Bagaimana cara membuat kue bolu yang lembut?")
    judul = riwayat.get_sesi(sesi)["judul"]
    assert judul.startswith("Bagaimana cara membuat")   # dari pesan user pertama
    assert "\n" not in judul                            # dibersihkan dari newline
    riwayat.tutup()


def test_ganti_judul_manual():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    assert riwayat.ganti_judul(sesi, "Proyek Kantor") is True
    assert riwayat.get_sesi(sesi)["judul"] == "Proyek Kantor"
    assert riwayat.ganti_judul(sesi, "   ") is False    # judul kosong ditolak
    assert riwayat.ganti_judul(999, "x") is False       # sesi tak ada
    riwayat.tutup()


def test_riwayat_chat_format_openai_tanpa_role_tool():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "user", "hitung 2+2")
    riwayat.tambah_pesan(sesi, "tool", "hitung({ekspresi}) -> OK")   # harus disaring
    riwayat.tambah_pesan(sesi, "assistant", "hasilnya 4")
    konteks = riwayat.riwayat_chat(sesi, maks=10)
    assert konteks == [
        {"role": "user", "content": "hitung 2+2"},
        {"role": "assistant", "content": "hasilnya 4"},
    ]
    # maks=1 -> hanya pesan terakhir
    assert riwayat.riwayat_chat(sesi, maks=1) == [
        {"role": "assistant", "content": "hasilnya 4"},
    ]
    riwayat.tutup()


# ------------------------------------------------------------
# Daftar sesi & pencarian
# ------------------------------------------------------------
def test_daftar_sesi_urut_terbaru_dengan_jumlah_pesan():
    riwayat, _ = buat_history()
    s1 = riwayat.buat_sesi()
    riwayat.tambah_pesan(s1, "user", "sesi satu")
    s2 = riwayat.buat_sesi()
    riwayat.tambah_pesan(s2, "user", "sesi dua")
    riwayat.tambah_pesan(s2, "assistant", "siap")

    daftar = riwayat.daftar_sesi(10)
    assert len(daftar) == 2
    terbaru = daftar[0]
    assert terbaru["id"] == s2                          # diperbarui paling akhir
    assert terbaru["jumlah_pesan"] == 2
    assert terbaru["pesan_terakhir"] == "siap"
    riwayat.tutup()


def test_cari_keyword_case_insensitive():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "user", "Bagaimana Cara Membuat KOPI enak?")
    assert len(riwayat.cari("kopi")) == 1               # lowercase menemukan UPPERCASE
    assert len(riwayat.cari("MEMBUAT")) == 1
    assert riwayat.cari("teh") == []
    assert riwayat.cari("   ") == []                    # keyword kosong -> []
    riwayat.tutup()


def test_cari_escape_karakter_like():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "user", "harga 100% naik _banyak_ sekali")
    # '%' dan '_' user TIDAK boleh berperan sebagai wildcard LIKE
    assert len(riwayat.cari("100%")) == 1
    assert len(riwayat.cari("_banyak_")) == 1
    # tapi wildcard murni tidak boleh mencocokkan semuanya
    assert riwayat.cari("zzz%") == []
    riwayat.tutup()


# ------------------------------------------------------------
# Hapus & persist
# ------------------------------------------------------------
def test_hapus_sesi_cascade_pesan():
    riwayat, _ = buat_history()
    s1 = riwayat.buat_sesi()
    riwayat.tambah_pesan(s1, "user", "satu")
    s2 = riwayat.buat_sesi()
    riwayat.tambah_pesan(s2, "user", "dua")

    assert riwayat.hapus_sesi(s1) is True
    assert riwayat.hapus_sesi(s1) is False              # sudah terhapus
    assert riwayat.pesan_sesi(s1) == []                 # pesan ikut hilang
    assert riwayat.statistik()["pesan"] == 1            # sisa pesan milik s2
    riwayat.tutup()


def test_data_persist_setelah_tutup_dan_buka_ulang():
    tmp = tempfile.mkdtemp(prefix="assistantai-test-")
    db = Path(tmp) / "persist.db"
    sesi = None
    with ChatHistory(db) as riwayat:
        sesi = riwayat.buat_sesi()
        riwayat.tambah_pesan(sesi, "user", "data harus bertahan")
        riwayat.tambah_pesan(sesi, "assistant", "ya", tier="tier2")
    with ChatHistory(db) as lagi:                       # simulasi restart aplikasi
        assert lagi.statistik()["sesi"] == 1
        assert lagi.statistik()["pesan"] == 2
        assert lagi.pesan_sesi(sesi)[0]["isi"] == "data harus bertahan"
        assert lagi.statistik()["per_tier"].get("tier2") == 1


# ------------------------------------------------------------
# Ekspor
# ------------------------------------------------------------
def test_export_md_json_txt():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "user", "siapa kamu?")
    riwayat.tambah_pesan(sesi, "assistant", "Aku AssistantAI.", tier="tier1")

    md = riwayat.export_sesi(sesi, "md")
    assert md.startswith(f"# Sesi #{sesi}")
    assert "**Anda**" in md and "**AI**" in md
    assert "tier: tier1" in md

    data = json.loads(riwayat.export_sesi(sesi, "json"))
    assert data["sesi"]["id"] == sesi
    assert len(data["pesan"]) == 2

    txt = riwayat.export_sesi(sesi, "txt")
    assert "Anda > siapa kamu?" in txt
    assert "AI > Aku AssistantAI." in txt
    riwayat.tutup()


def test_export_format_tidak_dikenal_dan_sesi_hilang():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    riwayat.tambah_pesan(sesi, "user", "halo")
    try:
        riwayat.export_sesi(sesi, "pdf")
    except ValueError as exc:
        assert "md, json, txt" in str(exc)
    else:
        raise AssertionError("format 'pdf' harusnya raise ValueError")
    try:
        riwayat.export_sesi(9999, "md")
    except ValueError as exc:
        assert "9999" in str(exc)
    else:
        raise AssertionError("sesi 9999 harusnya raise ValueError")
    riwayat.tutup()


# ------------------------------------------------------------
# Validasi input
# ------------------------------------------------------------
def test_validasi_peran_isi_dan_sesi():
    riwayat, _ = buat_history()
    sesi = riwayat.buat_sesi()
    for kasus in (
        lambda: riwayat.tambah_pesan(sesi, "robot", "hi"),        # peran salah
        lambda: riwayat.tambah_pesan(sesi, "user", "   "),        # isi kosong
        lambda: riwayat.tambah_pesan(12345, "user", "hi"),        # sesi tak ada
    ):
        try:
            kasus()
        except ValueError:
            pass
        else:
            raise AssertionError("harusnya raise ValueError")
    riwayat.tutup()


def test_statistik_distribusi_per_tier():
    riwayat, _ = buat_history()
    s = riwayat.buat_sesi()
    riwayat.tambah_pesan(s, "user", "a")
    riwayat.tambah_pesan(s, "assistant", "b", tier="tier1")
    riwayat.tambah_pesan(s, "user", "c")
    riwayat.tambah_pesan(s, "assistant", "d", tier="tier3")
    stat = riwayat.statistik()
    assert stat["sesi"] == 1
    assert stat["pesan"] == 4
    assert stat["per_tier"] == {"tier1": 1, "tier3": 1}
    riwayat.tutup()


# ------------------------------------------------------------
# Runner tanpa pytest
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
