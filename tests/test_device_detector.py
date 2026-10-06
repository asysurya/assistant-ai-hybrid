# ============================================================
# AssistantAI — tests/test_device_detector.py
# ------------------------------------------------------------
# Test DEVICE DETECTOR (FASE 3A) — hampir semua test memakai
# NILAI INJECT, jadi hasilnya deterministik di mesin APAPUN
# (Windows/Linux/macOS/Termux) dan tidak butuh psutil.
#
# Skenario yang diuji:
#   - klasifikasi high/medium/low + SEMUA batas angka (6/3 GB, 4/2 core)
#   - skenario spec: laptop 8 GB -> high, HP 2 GB -> low
#   - RAM 0 (deteksi gagal) -> low = aman (full cloud)
#   - ke_dict()/dari_dict() round-trip utk cache config.json
#   - ringkasan() siap wizard
#   - deteksi GPU: NVIDIA (nvidia-smi fake), Apple Silicon, gagal/timeout
#   - deteksi Termux + pemetaan nama OS (Win11 build 22000+, macOS, dll.)
#   - jalur fallback stdlib RAM (Linux nyata diuji; OS lain = graceful)
#   - deteksi nyata di mesin ini: semua field terisi, tidak crash
#
# Jalankan:
#   python -m pytest tests/test_device_detector.py -v
#   python tests/test_device_detector.py
# ============================================================

"""Test suite DeviceDetector: klasifikasi adaptive high/medium/low."""

import platform
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.device_detector import (  # noqa: E402
    DeviceProfile,
    _cpu_freq_mhz,
    _deteksi_gpu,
    _deteksi_termux,
    _disk_bebas_gb,
    _nama_os,
    _ram_linux,
    _ukur_ram,
)

DI_LINUX = platform.system() == "Linux"       # guard test khusus Linux
DI_WINDOWS = platform.system() == "Windows"   # guard test khusus Windows


# ------------------------------------------------------------
# Pembangun profil palsu — deterministik, tanpa mengukur mesin
# ------------------------------------------------------------
def profil(ram: float, cpu: int, **tambahan) -> DeviceProfile:
    """DeviceProfile dengan nilai injeksi penuh (tidak menyentuh mesin)."""
    bawaan = dict(
        os_name="Windows 11", arch="ARM64",
        ram_total_gb=ram, ram_available_gb=ram * 0.5,
        cpu_count=cpu, cpu_freq_mhz=2200.0, disk_free_gb=100.0,
        has_gpu=False,
    )
    bawaan.update(tambahan)
    return DeviceProfile(**bawaan)


def runner_nvidia(ok: bool = True, stdout: str = "NVIDIA GeForce RTX\n"):
    """Fake subprocess.run: meniru hasil panggilan nvidia-smi."""
    if ok:
        return lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout)
    return lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"")


# ------------------------------------------------------------
# Klasifikasi high / medium / low (+ batas angka)
# ------------------------------------------------------------
def test_laptop_8gb_adalah_high():
    # Skenario Test 1 spec: laptop 8 GB, CPU 8 core target -> full lokal
    assert profil(7.8, 8).classify() == "high"


def test_batas_ram_6gb_tepat_adalah_high():
    assert profil(6.0, 4).classify() == "high"     # >= 6 -> high
    assert profil(5.9, 4).classify() == "medium"   # sedikit di bawah -> turun


def test_ram_cukup_tapi_cpu_kurang_jadi_medium():
    # CPU < 4 core MELARANG "high" walau RAM besar (aturan spec: DAN)
    assert profil(8.0, 2).classify() == "medium"


def test_hybrid_4gb_adalah_medium():
    # Skenario Test 2 spec arah "medium": RAM cukup, model lokal hemat
    assert profil(4.0, 4).classify() == "medium"


def test_batas_ram_3gb():
    assert profil(3.0, 2).classify() == "medium"   # >= 3 -> medium
    assert profil(2.9, 2).classify() == "low"      # di bawahnya -> low


def test_hp_lowend_2gb_adalah_low():
    # Skenario Test 3 spec: HP low-end, model lokal TIDAK di-load
    assert profil(1.9, 4).classify() == "low"


def test_ram_nol_deteksi_gagal_adalah_low_aman():
    # RAM gagal terukur (0) -> "low" = full cloud = perilaku paling aman
    assert profil(0.0, 4).classify() == "low"


def test_label_mode_sesuai_klasifikasi():
    assert "HIGH" in profil(8.0, 8).label_mode()
    assert "MEDIUM" in profil(4.0, 4).label_mode()
    assert "LOW" in profil(2.0, 2).label_mode()


# ------------------------------------------------------------
# Serialisasi: ke_dict / dari_dict (cache config.json)
# ------------------------------------------------------------
def test_ke_dict_bentuk_sesuai_spec():
    d = profil(7.8, 8).ke_dict()
    wajib = {"cached_at", "os", "arch", "ram_total_gb", "ram_available_gb",
             "cpu_count", "cpu_freq_mhz", "disk_free_gb", "has_gpu",
             "classification"}
    assert wajib.issubset(d.keys())
    assert d["classification"] == "high"
    assert "T" in d["cached_at"]                   # format ISO timestamp
    assert isinstance(d["cpu_count"], int)
    assert isinstance(d["has_gpu"], bool)
    assert isinstance(d["ram_total_gb"], float)


def test_dari_dict_roundtrip():
    p1 = profil(3.7, 4, os_name="Android (Termux)", arch="aarch64")
    p2 = DeviceProfile.dari_dict(p1.ke_dict())
    assert p2.classify() == p1.classify()
    assert p2.os == "Android (Termux)"
    assert p2.arch == "aarch64"
    assert abs(p2.ram_total_gb - 3.7) < 0.01
    assert p2.ke_dict()["classification"] == "medium"


# ------------------------------------------------------------
# ringkasan() — bahan wizard FASE 3C
# ------------------------------------------------------------
def test_ringkasan_memuat_informasi_kunci():
    teks = profil(7.8, 8, has_gpu=True).ringkasan()
    for potongan in ("Windows 11", "ARM64", "8 core", "RAM", "7.8 GB",
                     "Storage", "GPU", "HIGH"):
        assert potongan in teks, f"ringkasan kurang: {potongan}"


def test_ringkasan_gpu_tidak_ada():
    assert "Tidak terdeteksi" in profil(7.8, 8, has_gpu=False).ringkasan()


def test_repr_mudah_dibaca():
    r = repr(profil(4.0, 4))
    assert "DeviceProfile" in r and "medium" in r


# ------------------------------------------------------------
# Deteksi GPU (nvidia-smi fake + Apple Silicon)
# ------------------------------------------------------------
def test_gpu_nvidia_terdeteksi():
    ada = _deteksi_gpu(runner=runner_nvidia(True, "Tesla T4\n"),
                       sistem="Windows", arch="AMD64")
    assert ada is True


def test_gpu_nvidia_returncode_bukan_nol():
    tidak = _deteksi_gpu(runner=runner_nvidia(False),
                         sistem="Windows", arch="AMD64")
    assert tidak is False


def test_gpu_nvidia_tidak_terpasang_file_not_found():
    def runner_gagal(*a, **k):
        raise FileNotFoundError("nvidia-smi tidak ada")
    assert _deteksi_gpu(runner=runner_gagal, sistem="Linux", arch="x86_64") is False


def test_gpu_nvidia_timeout():
    def runner_lambat(*a, **k):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3)
    assert _deteksi_gpu(runner=runner_lambat, sistem="Linux", arch="x86_64") is False


def test_gpu_apple_silicon_selalu_ada_tanpa_panggil_runner():
    def runner_haram(*a, **k):
        raise AssertionError("Apple Silicon tidak boleh memanggil nvidia-smi")
    assert _deteksi_gpu(runner=runner_haram, sistem="Darwin", arch="arm64") is True


def test_gpu_mac_intel_tidak_otomatis_true():
    def runner_gagal(*a, **k):
        raise FileNotFoundError
    assert _deteksi_gpu(runner=runner_gagal, sistem="Darwin", arch="x86_64") is False


# ------------------------------------------------------------
# Termux & nama OS
# ------------------------------------------------------------
def test_deteksi_termux():
    assert _deteksi_termux({"PREFIX": "/data/data/com.termux/files/usr"}) is True
    assert _deteksi_termux({"ANDROID_ROOT": "/system"}) is True
    assert _deteksi_termux({"PREFIX": "/usr"}) is False
    assert _deteksi_termux({}) is False


def test_nama_os_windows_dari_build():
    # platform.release() sering masih "10" di Win11 — makna cek build:
    assert _nama_os(sistem="Windows", build_windows=22631) == "Windows 11"
    assert _nama_os(sistem="Windows", build_windows=19045) == "Windows 10"
    assert _nama_os(sistem="Windows", build_windows=0) == "Windows 10"


def test_nama_os_lainnya():
    assert _nama_os(sistem="Darwin") == "macOS"
    assert _nama_os(sistem="Linux", env={"PREFIX": "/usr"}) == "Linux"
    assert _nama_os(sistem="Linux",
                    env={"PREFIX": "/data/data/com.termux/files/usr"}) == "Android (Termux)"
    assert _nama_os(sistem="FreeBSD") == "FreeBSD"


# ------------------------------------------------------------
# Jalur fallback RAM (stdlib tanpa psutil)
# ------------------------------------------------------------
def test_ukur_ram_os_tidak_dikenal_aman():
    # OS aneh + psutil dilewati -> (0, 0), TIDAK raise -> device jadi "low"
    assert _ukur_ram(sistem="SunOS", pakai_psutil=False) == (0.0, 0.0)


def test_ukur_ram_tidak_pernah_raise():
    # Sistem palsu yang "menjanjikan" Linux tapi file tidak ada:
    # simulasi via OS tak dikenal saja — intinya selalu kembalikan tuple.
    hasil = _ukur_ram(sistem="TidakAda", pakai_psutil=False)
    assert isinstance(hasil, tuple) and len(hasil) == 2


if DI_LINUX:
    def test_ram_linux_nyata():
        total, avail = _ram_linux()
        assert total > 0
        assert 0 < avail <= total

    def test_ukur_ram_fallback_linux_stdlib():
        total, avail = _ukur_ram(sistem="Linux", pakai_psutil=False)
        assert total > 0 and 0 < avail <= total


# ------------------------------------------------------------
# Deteksi nyata di mesin tempat test dijalankan
# ------------------------------------------------------------
def test_deteksi_nyata_semua_field_terisi():
    p = DeviceProfile()
    assert p.ram_total_gb > 0                      # Windows: ctypes / Linux: /proc
    assert p.cpu_count >= 1
    assert p.disk_free_gb > 0
    assert p.arch
    assert p.os
    assert p.classify() in ("high", "medium", "low")


def test_cpu_freq_tidak_pernah_raise():
    assert _cpu_freq_mhz() >= 0                    # 0 = tidak diketahui, sah


def test_disk_bebas_nyata_positif():
    assert _disk_bebas_gb() > 0


if DI_WINDOWS:
    def test_ram_windows_nyata():
        from core.device_detector import _ram_windows
        total, avail = _ram_windows()
        assert total > 0 and 0 < avail <= total


# ------------------------------------------------------------
# Runner tanpa pytest
# ------------------------------------------------------------
if __name__ == "__main__":
    daftar = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
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
