# ============================================================
# AssistantAI — tests/test_tools.py
# ------------------------------------------------------------
# Test TOOLKIT BAWAAN + REGISTRY — semuanya lokal & aman:
#   - registry: 6 tools bawaan, nama unik, skema OpenAI valid
#   - kalkulator AST: aritmetika benar, ekspresi berbahaya DITOLAK,
#     proteksi DoS (9**9**9), pembagian nol
#   - file tools: terkurung workspace (../ dan path absolut ditolak),
#     tulis-baca konsisten, potongan file raksasa
#   - konfirmasi: tulis_file tanpa izin = ditolak
#   - todo: tambah/daftar/selesai/hapus + indeks di luar rentang
#
# Jalankan:
#   python -m pytest tests/test_tools.py -v
#   python tests/test_tools.py
# ============================================================

"""Test suite tools bawaan: kalkulator aman, file workspace, todo, registry."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tools import (  # noqa: E402
    ToolRegistry,
    hitung_ekspresi,
    tool_waktu,
)


def buat_registry() -> tuple[ToolRegistry, Path]:
    """Registry dengan workspace sementara (file test tidak menyentuh data user)."""
    tmp = tempfile.mkdtemp(prefix="assistantai-ws-")
    reg = ToolRegistry(workspace=Path(tmp))
    return reg, Path(tmp)


# ------------------------------------------------------------
# Registry & skema
# ------------------------------------------------------------
def test_registry_default_enam_tools_nama_unik():
    reg, _ = buat_registry()
    nama = reg.nama_tools()
    assert len(reg) == 6
    assert len(nama) == len(set(nama))                  # tidak ada duplikat
    for wajib in ("get_time", "hitung", "baca_file", "tulis_file",
                  "daftar_folder", "todo"):
        assert wajib in nama
    # tulis_file adalah satu-satunya yang butuh konfirmasi
    assert reg.dapatkan("tulis_file").butuh_konfirmasi is True
    assert reg.dapatkan("baca_file").butuh_konfirmasi is False


def test_daftarkan_duplikat_ditolak():
    reg, _ = buat_registry()
    try:
        reg.daftarkan(reg.dapatkan("hitung"))           # nama sama
    except ValueError:
        pass
    else:
        raise AssertionError("duplikat nama harusnya ditolak")


def test_skema_openai_format_valid():
    reg, _ = buat_registry()
    skema = reg.skema_openai()
    assert len(skema) == 6
    for s in skema:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert "properties" in fn["parameters"]
    # skema hitung: wajib 'ekspresi'
    hitung = next(s for s in skema if s["function"]["name"] == "hitung")
    assert hitung["function"]["parameters"]["required"] == ["ekspresi"]


# ------------------------------------------------------------
# Tool waktu & kalkulator
# ------------------------------------------------------------
def test_tool_waktu_mengandung_tanggal():
    hasil = tool_waktu({})
    assert any(h in hasil for h in ("Senin", "Selasa", "Rabu", "Kamis",
                                    "Jumat", "Sabtu", "Minggu"))
    assert "-" in hasil and ":" in hasil                # format tanggal & jam


def test_hitung_aritmetika_benar():
    reg, _ = buat_registry()
    for ekspresi, harapan in [
        ("(2+3)*4", "20"),
        ("2**10", "1024"),
        ("10/4", "2.5"),
        ("7//2", "3"),
        ("7%3", "1"),
        ("-5+3", "-2"),
        ("2.5*2", "5"),
    ]:
        hasil = reg.eksekusi("hitung", {"ekspresi": ekspresi})
        assert hasil.ok is True, ekspresi
        assert hasil.keluaran.endswith(f"= {harapan}"), f"{ekspresi} -> {hasil.keluaran}"
    # lewat fungsi langsung pun harus konsisten
    assert hitung_ekspresi("1+1") == "2"


def test_hitung_tolak_ekspresi_berbahaya():
    reg, _ = buat_registry()
    berbahaya = [
        "__import__('os').system('dir')",
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "getattr(str, 'format')",
        "os.system('rm -rf /')",
        "(lambda: 1)()",
        "[1,2][0]",
        "'a' + 'b'",
    ]
    for eks in berbahaya:
        hasil = reg.eksekusi("hitung", {"ekspresi": eks})
        assert hasil.ok is False, f"{eks} harusnya ditolak"
        assert "diizinkan" in hasil.keluaran or "tidak valid" in hasil.keluaran \
            or "sintaks" in hasil.keluaran.lower()


def test_hitung_proteksi_dos_dan_pembagian_nol():
    reg, _ = buat_registry()
    # pangkat raksasa harus ditolak CEPAT (bukan hang)
    hasil = reg.eksekusi("hitung", {"ekspresi": "9**9**9"})
    assert hasil.ok is False
    hasil = reg.eksekusi("hitung", {"ekspresi": "1/0"})
    assert hasil.ok is False and "ZeroDivisionError" in hasil.keluaran
    hasil = reg.eksekusi("hitung", {"ekspresi": ""})
    assert hasil.ok is False
    hasil = reg.eksekusi("hitung", {"ekspresi": "2+3*"})
    assert hasil.ok is False and "sintaks" in hasil.keluaran.lower()


# ------------------------------------------------------------
# File tools (terkurung workspace)
# ------------------------------------------------------------
def test_tulis_lalu_baca_file_dalam_workspace():
    reg, ws = buat_registry()
    tulis = reg.eksekusi("tulis_file", {"jalur": "catatan/halo.txt", "isi": "hai dunia"},
                         konfirmasi=True)
    assert tulis.ok is True
    baca = reg.eksekusi("baca_file", {"jalur": "catatan/halo.txt"})
    assert baca.ok is True and baca.keluaran == "hai dunia"
    # file benar-benar ada di workspace
    assert (ws / "catatan" / "halo.txt").read_text(encoding="utf-8") == "hai dunia"
    # daftar_folder menampakkannya
    daftar = reg.eksekusi("daftar_folder", {"jalur": "catatan"})
    assert daftar.ok is True and "halo.txt" in daftar.keluaran


def test_path_keluar_workspace_ditolak():
    reg, _ = buat_registry()
    untuk_jalur = ["../rahasia.txt", "../../etc/passwd", "/etc/passwd", "C:\\Windows\\x.txt"]
    for jalur in untuk_jalur:
        baca = reg.eksekusi("baca_file", {"jalur": jalur})
        assert baca.ok is False, jalur
    # pesan spesifik utk kasus lolos relativ ("..")
    baca = reg.eksekusi("baca_file", {"jalur": "../rahasia.txt"})
    assert "di luar workspace" in baca.keluaran
    # tulis_file pun aturan yang sama
    tulis = reg.eksekusi("tulis_file", {"jalur": "../jahil.txt", "isi": "x"},
                         konfirmasi=True)
    assert tulis.ok is False


def test_baca_file_tidak_ada_dan_file_dipotong():
    reg, ws = buat_registry()
    baca = reg.eksekusi("baca_file", {"jalur": "tidak-ada.txt"})
    assert baca.ok is False and "FileNotFoundError" in baca.keluaran

    besar = "A" * 5000
    (ws / "besar.txt").write_text(besar, encoding="utf-8")
    hasil = reg.eksekusi("baca_file", {"jalur": "besar.txt", "maks_karakter": 100})
    assert hasil.ok is True
    assert "DIPOTONG" in hasil.keluaran and hasil.keluaran.startswith("A")


def test_daftar_folder_error_kasus():
    reg, _ = buat_registry()
    hasil = reg.eksekusi("daftar_folder", {"jalur": "tidak-ada/"})
    assert hasil.ok is False and "FileNotFoundError" in hasil.keluaran
    hasil = reg.eksekusi("daftar_folder", {"jalur": "."})   # root workspace ada
    assert hasil.ok is True


# ------------------------------------------------------------
# Konfirmasi
# ------------------------------------------------------------
def test_tulis_file_butuh_konfirmasi():
    reg, _ = buat_registry()
    ditolak = reg.eksekusi("tulis_file", {"jalur": "x.txt", "isi": "data"})
    assert ditolak.ok is False
    assert "konfirmasi" in ditolak.keluaran.lower()
    # teks yang dikirim ke model harus informatif
    assert "ERROR" in ditolak.teks_model
    dgn_izin = reg.eksekusi("tulis_file", {"jalur": "x.txt", "isi": "data"},
                            konfirmasi=True)
    assert dgn_izin.ok is True and "Tersimpan" in dgn_izin.keluaran


# ------------------------------------------------------------
# Tool tak dikenal & todo
# ------------------------------------------------------------
def test_tool_tidak_dikenal_pesan_informatif():
    reg, _ = buat_registry()
    hasil = reg.eksekusi("format_disk", {})
    assert hasil.ok is False
    assert "tidak dikenal" in hasil.keluaran.lower()
    assert "hitung" in hasil.keluaran                   # daftar tool tersedia ikut ditampilkan


def test_todo_siklus_lengkap():
    reg, _ = buat_registry()
    # daftar kosong
    r0 = reg.eksekusi("todo", {"aksi": "daftar"})
    assert r0.ok and "kosong" in r0.keluaran
    # tambah dua
    reg.eksekusi("todo", {"aksi": "tambah", "teks": "Belajar FASE 2"})
    reg.eksekusi("todo", {"aksi": "tambah", "teks": "Kunci rumah"})
    r1 = reg.eksekusi("todo", {"aksi": "daftar"})
    assert "[ ] Belajar FASE 2" in r1.keluaran and "[ ] Kunci rumah" in r1.keluaran
    # selesai #1
    r2 = reg.eksekusi("todo", {"aksi": "selesai", "indeks": 1})
    assert r2.ok and "[x] Belajar FASE 2" in r2.keluaran
    # hapus #2
    r3 = reg.eksekusi("todo", {"aksi": "hapus", "indeks": 2})
    assert r3.ok and "Kunci rumah" in r3.keluaran
    assert "Kunci rumah" not in reg.eksekusi("todo", {"aksi": "daftar"}).keluaran
    # indeks di luar rentang -> error terbungkus
    r4 = reg.eksekusi("todo", {"aksi": "selesai", "indeks": 99})
    assert r4.ok is False and "rentang" in r4.keluaran
    # aksi tak dikenal
    r5 = reg.eksekusi("todo", {"aksi": "ledakkan"})
    assert r5.ok is False and "tidak dikenal" in r5.keluaran


def test_todo_tambah_tanpa_teks_ditolak():
    reg, _ = buat_registry()
    hasil = reg.eksekusi("todo", {"aksi": "tambah"})
    assert hasil.ok is False and "teks" in hasil.keluaran


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
