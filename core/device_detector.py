# ============================================================
# AssistantAI — core/device_detector.py
# ------------------------------------------------------------
# ADAPTIVE RUNTIME — FASE 3A: deteksi kemampuan device saat startup.
#
# TUJUAN: binary harus bisa jalan di SEMUA device (RAM 2 GB s/d 32 GB)
# tanpa konfigurasi manual. Modul ini mengukur OS, arsitektur, RAM, CPU,
# disk, dan GPU, lalu mengklasifikasikan device menjadi:
#
#   high   (RAM >= 6 GB DAN CPU >= 4 core) -> full lokal (offline, gratis)
#   medium (RAM >= 3 GB DAN CPU >= 2 core) -> hybrid (lokal ringan + cloud)
#   low    (sisanya)                       -> full cloud (lokal TIDAK di-load)
#
# KEPUTUSAN TEKNIS (penyempurnaan dari sketsa awal, alasannya eksplisit):
#   1. PSUTIL = sensor UTAMA, tapi TIDAK wajib. Kalau terpasang (sudah
#      tercantum di requirements.txt, wheel win_arm64 tersedia di psutil
#      7.x), dipakai untuk angka paling presisi. Kalau TIDAK ada (mis.
#      Termux/Android yang sulit memasang psutil), modul tetap bekerja
#      penuh lewat fallback stdlib:
#        - Windows : ctypes GlobalMemoryStatusEx (bawaan Python)
#        - Linux   : /proc/meminfo (Termux, server, WSL)
#        - macOS   : sysctl hw.memsize + vm_stat
#      Pola yang sama dengan import "lunak" openai di core/local_client.py.
#   2. Disk diukur dari folder HOME, BUKAN "/" — di Windows path "/"
#      tidak valid (sketsa awal akan gagal di target utama!).
#   3. RAM gagal terukur -> (0, 0) -> device terklasifikasi "low" = aman
#      (full cloud). Aplikasi TIDAK PERNAH crash karena deteksi gagal.
#   4. Semua field bisa DI-INJECT lewat constructor supaya test 100%
#      deterministik lintas mesin (pola DI seperti local_client/key_pool).
#
# PEMAKAI hasil deteksi (fase berikutnya):
#   - core/model_autoselect.py (FASE 3B): pilih model lokal sesuai RAM
#   - core/memory_watcher.py   (FASE 3B): monitor RAM real-time
#   - core/adaptive_router.py  (FASE 3B): routing lokal/cloud dinamis
#   - ui/wizard.py             (FASE 3C): step "Analisis Device"
# ============================================================

"""Deteksi & klasifikasi kemampuan device (adaptive runtime).

Cara test cepat:
    python core/device_detector.py
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# ------------------------------------------------------------
# psutil OPSIONAL — sensor utama bila terpasang (lihat catatan header)
# ------------------------------------------------------------
try:
    import psutil                              # type: ignore
    _PSUTIL_TERSEDIA = True
except ImportError:                            # Termux / venv minim
    psutil = None                              # type: ignore[assignment]
    _PSUTIL_TERSEDIA = False

GB = 1024 ** 3                       # 1 GB dalam byte
_KLASIFIKASI_VALID = ("high", "medium", "low")

# Label mode utk wizard/settings — sengaja sekolah di SATU tempat.
_LABEL_MODE = {
    "high": "HIGH — full lokal (offline & gratis)",
    "medium": "MEDIUM — hybrid (lokal ringan + cloud)",
    "low": "LOW — full cloud (lokal tidak di-load)",
}


# ------------------------------------------------------------
# Pengukuran RAM (psutil -> fallback stdlib per-OS)
# ------------------------------------------------------------
def _ram_via_psutil() -> tuple[float, float]:
    """(total_gb, available_gb) via psutil — paling presisi, semua OS."""
    vm = psutil.virtual_memory()               # type: ignore[union-attr]
    return vm.total / GB, vm.available / GB


def _ram_windows() -> tuple[float, float]:
    """RAM via ctypes GlobalMemoryStatusEx — TANPA dependensi eksternal."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        # Struktur Win32 lengkap — urutan field WAJIB sama dgn API Windows.
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError("GlobalMemoryStatusEx gagal")
    return stat.ullTotalPhys / GB, stat.ullAvailPhys / GB


def _ram_linux() -> tuple[float, float]:
    """RAM via /proc/meminfo — Linux, Termux/Android, WSL, server.

    MemAvailable hanya ada di kernel >= 3.14; kernel lama dihitung manual
    (total - free - buffers - cached) agar angka tetap masuk akal.
    """
    nilai: dict[str, float] = {}
    with open("/proc/meminfo", encoding="ascii") as f:
        for baris in f:
            bagian = baris.split()
            if bagian:
                nilai[bagian[0].rstrip(":")] = float(bagian[1]) * 1024  # kB -> byte
    total = nilai.get("MemTotal", 0.0)
    if total <= 0:
        raise OSError("/proc/meminfo tidak terbaca")
    if "MemAvailable" in nilai:
        avail = nilai["MemAvailable"]
    else:
        avail = max(0.0, total - nilai.get("MemFree", 0.0)
                    - nilai.get("Buffers", 0.0) - nilai.get("Cached", 0.0))
    return total / GB, avail / GB


def _ram_macos() -> tuple[float, float]:
    """RAM total via sysctl; available via vm_stat (perkiraan free+inactive)."""
    total = float(subprocess.run(
        ["sysctl", "-n", "hw.memsize"],
        capture_output=True, text=True, timeout=3, check=True,
    ).stdout.strip() or 0)
    if total <= 0:
        raise OSError("sysctl hw.memsize gagal")
    avail = total          # fallback optimis; pasang psutil utk angka presisi
    try:
        keluaran = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                  timeout=3, check=True).stdout
        halaman: dict[str, int] = {}
        ukuran_halaman = 4096
        for baris in keluaran.splitlines():
            if "page size of" in baris:            # header: "(page size of 16384 bytes)"
                try:
                    ukuran_halaman = int(baris.split()[3])
                except (ValueError, IndexError):
                    pass
            if ":" in baris:
                kunci, _, v = baris.partition(":")
                v = v.strip().rstrip(".")
                if v.isdigit():
                    halaman[kunci.strip()] = int(v)
        bebas = (halaman.get("Pages free", 0) + halaman.get("Pages inactive", 0)) * ukuran_halaman
        if bebas > 0:
            avail = min(bebas, total)
    except Exception:
        pass               # vm_stat gagal -> pakai fallback di atas
    return total / GB, avail / GB


def _ukur_ram(sistem: Optional[str] = None, pakai_psutil: bool = True) -> tuple[float, float]:
    """Ukur (ram_total_gb, ram_available_gb) — TIDAK PERNAH raise.

    Urutan: psutil (bila ada) -> stdlib per-OS -> (0.0, 0.0).
    RAM 0 membuat device terklasifikasi "low" (full cloud) = perilaku aman:
    aplikasi tetap hidup walau deteksi gagal total.

    Args:
        sistem: paksa nama OS (untuk test); None = platform.system().
        pakai_psutil: False = lewati psutil (untuk test jalur fallback).
    """
    if pakai_psutil and _PSUTIL_TERSEDIA:
        try:
            return _ram_via_psutil()
        except Exception:
            pass                     # psutil ada tapi error -> coba stdlib
    sistem = sistem or platform.system()
    try:
        if sistem == "Windows":
            return _ram_windows()
        if sistem == "Linux":
            return _ram_linux()
        if sistem == "Darwin":
            return _ram_macos()
    except Exception:
        pass
    return 0.0, 0.0


# ------------------------------------------------------------
# Deteksi lingkungan: Termux, nama OS, GPU, CPU freq, disk
# ------------------------------------------------------------
def _deteksi_termux(env: dict) -> bool:
    """True bila proses jalan di Termux/Android (heuristik env khas)."""
    prefix = env.get("PREFIX", "") or ""
    return "com.termux" in prefix or bool(env.get("ANDROID_ROOT"))


def _nama_os(sistem: Optional[str] = None, env: Optional[dict] = None,
             build_windows: Optional[int] = None) -> str:
    """Nama OS ramah tampilan: 'Windows 11', 'macOS', 'Android (Termux)', dst.

    Windows 11 dikenali dari build >= 22000 (platform.release() sering
    masih melaporkan "10" di Windows 11 — makanya cek build langsung).
    """
    sistem = sistem or platform.system()
    env = env if env is not None else dict(os.environ)
    if sistem == "Windows":
        if build_windows is None:
            try:
                build_windows = sys.getwindowsversion().build   # type: ignore[attr-defined]
            except Exception:
                build_windows = 0
        return "Windows 11" if (build_windows or 0) >= 22000 else "Windows 10"
    if sistem == "Darwin":
        return "macOS"
    if sistem == "Linux":
        return "Android (Termux)" if _deteksi_termux(env) else "Linux"
    return sistem or "Tidak dikenal"


def _deteksi_gpu(runner: Optional[Callable] = None,
                 sistem: Optional[str] = None,
                 arch: Optional[str] = None) -> bool:
    """True bila ada GPU "makmur": NVIDIA (via nvidia-smi) atau Apple Silicon.

    Catatan jujur: GPU Intel/AMD diskrit di Windows sulit dideteksi tanpa
    WMI/psutil tambahan — dianggap False untuk sekarang. Nilai ini INFORMASI
    saja (TIDAK dipakai classify()); GPU acceleration menyusul di fase lain.

    Args:
        runner: fungsi eksekusi perintah (injection utk test; default
            subprocess.run) — dipanggil dgn argv nvidia-smi.
    """
    sistem = sistem or platform.system()
    arch = arch or platform.machine()
    if sistem == "Darwin" and arch == "arm64":
        return True                     # Apple Silicon: GPU selalu menyatu
    runner = runner or subprocess.run
    try:
        hasil = runner(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, timeout=3,
        )
        keluar = str(getattr(hasil, "stdout", "") or "").strip()
        return getattr(hasil, "returncode", 1) == 0 and bool(keluar)
    except Exception:                   # FileNotFoundError/OSError/Timeout/dll.
        return False


def _cpu_freq_mhz() -> float:
    """Frekuensi CPU maksimum (MHz) — 0.0 bila tidak diketahui.

    Hanya untuk DISPLAY (wizard/status); classify() tidak memakainya,
    jadi kegagalan baca frekuensi tidak berdampak ke keputusan routing.
    """
    if _PSUTIL_TERSEDIA:
        try:
            f = psutil.cpu_freq()       # type: ignore[union-attr]
            if f is not None:
                return float(f.max or f.current or 0.0)
        except Exception:
            pass
    return 0.0


def _disk_bebas_gb(jalur: Optional[Path] = None) -> float:
    """Ruang disk bebas (GB) pada drive tempat folder HOME berada.

    PENTING: memakai Path.home(), BUKAN "/" — di Windows "/" tidak valid.
    """
    try:
        return shutil.disk_usage(jalur or Path.home()).free / GB
    except Exception:
        return 0.0


# ------------------------------------------------------------
# DeviceProfile
# ------------------------------------------------------------
class DeviceProfile:
    """Profil kemampuan device + klasifikasi adaptive (high/medium/low).

    Semua argumen constructor OPSIONAL:
      - None  -> nilai diukur otomatis dari mesin nyata (pemakaian normal).
      - Nilai -> dipakai apa adanya (dipakai TEST & replay dari cache).

    Contoh pemakaian nyata (main.py FASE 3B):
        profil = DeviceProfile()
        if profil.classify() == "low":
            ...  # skip model lokal, full cloud
    """

    def __init__(
        self,
        os_name: Optional[str] = None,
        arch: Optional[str] = None,
        ram_total_gb: Optional[float] = None,
        ram_available_gb: Optional[float] = None,
        cpu_count: Optional[int] = None,
        cpu_freq_mhz: Optional[float] = None,
        disk_free_gb: Optional[float] = None,
        has_gpu: Optional[bool] = None,
    ):
        # Catatan: atribut self.os sesuai spec — lookup global `os` di
        # dalam method tetap ke modul stdlib (instance attr tidak menutupi).
        self.os = os_name if os_name is not None else _nama_os()
        self.arch = arch if arch is not None else platform.machine()
        if ram_total_gb is None or ram_available_gb is None:
            total, avail = _ukur_ram()
            self.ram_total_gb = float(ram_total_gb if ram_total_gb is not None else total)
            self.ram_available_gb = float(ram_available_gb if ram_available_gb is not None else avail)
        else:
            self.ram_total_gb = float(ram_total_gb)
            self.ram_available_gb = float(ram_available_gb)
        self.cpu_count = int(cpu_count) if cpu_count is not None else (os.cpu_count() or 1)
        self.cpu_freq_mhz = float(cpu_freq_mhz) if cpu_freq_mhz is not None else _cpu_freq_mhz()
        self.disk_free_gb = float(disk_free_gb) if disk_free_gb is not None else _disk_bebas_gb()
        self.has_gpu = bool(has_gpu) if has_gpu is not None else _deteksi_gpu()

    # ---------- klasifikasi ----------
    def classify(self) -> str:
        """Klasifikasikan device: 'high' | 'medium' | 'low'.

        Aturan (sesuai spec adaptive runtime):
          high   : RAM >= 6 GB  DAN CPU >= 4 core -> full lokal
          medium : RAM >= 3 GB  DAN CPU >= 2 core -> hybrid
          low    : sisanya                        -> full cloud

        RAM 0 (deteksi gagal) jatuh ke "low" — perilaku paling aman.
        """
        if self.ram_total_gb >= 6 and self.cpu_count >= 4:
            return "high"
        if self.ram_total_gb >= 3 and self.cpu_count >= 2:
            return "medium"
        return "low"

    def label_mode(self) -> str:
        """Label mode operasi utk tampilan (wizard/settings/status bar)."""
        return _LABEL_MODE[self.classify()]

    # ---------- tampilan & serialisasi ----------
    def ringkasan(self) -> str:
        """Teks siap tampil di wizard step 'Analisis Device' (FASE 3C)."""
        gpu = "Ada (NVIDIA / Apple Silicon)" if self.has_gpu else "Tidak terdeteksi"
        freq = f" @ {self.cpu_freq_mhz:.0f} MHz" if self.cpu_freq_mhz else ""
        return "\n".join([
            f"OS      : {self.os} ({self.arch})",
            f"CPU     : {self.cpu_count} core{freq}",
            f"RAM     : {self.ram_total_gb:.1f} GB total, "
            f"{self.ram_available_gb:.1f} GB tersedia",
            f"Storage : {self.disk_free_gb:.1f} GB bebas",
            f"GPU     : {gpu}",
            f"Mode    : {self.label_mode()}",
        ])

    def ke_dict(self) -> dict:
        """Bentuk dict utk di-cache ke config.json (section device_profile).

        Sesuai spec config JSON: cached_at + angka + classification —
        supaya wizard/rekomendasi tidak perlu mengukur ulang tiap kali.
        """
        return {
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "os": self.os,
            "arch": self.arch,
            "ram_total_gb": round(self.ram_total_gb, 2),
            "ram_available_gb": round(self.ram_available_gb, 2),
            "cpu_count": self.cpu_count,
            "cpu_freq_mhz": round(self.cpu_freq_mhz, 1),
            "disk_free_gb": round(self.disk_free_gb, 2),
            "has_gpu": self.has_gpu,
            "classification": self.classify(),
        }

    @classmethod
    def dari_dict(cls, data: dict) -> "DeviceProfile":
        """Bangun ulang profil dari hasil ke_dict() (cache config.json)."""
        return cls(
            os_name=data.get("os"),
            arch=data.get("arch"),
            ram_total_gb=data.get("ram_total_gb"),
            ram_available_gb=data.get("ram_available_gb"),
            cpu_count=data.get("cpu_count"),
            cpu_freq_mhz=data.get("cpu_freq_mhz"),
            disk_free_gb=data.get("disk_free_gb"),
            has_gpu=data.get("has_gpu"),
        )

    def __repr__(self) -> str:                   # debug log lebih mudah dibaca
        return (f"DeviceProfile(os={self.os!r}, arch={self.arch!r}, "
                f"ram={self.ram_total_gb:.1f}GB, cpu={self.cpu_count}, "
                f"classify={self.classify()!r})")


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/device_detector.py — profil & klasifikasi device")
    print("=" * 60)
    print(f"psutil terpasang : {_PSUTIL_TERSEDIA}")

    profil = DeviceProfile()
    print("\n--- Device ini (deteksi nyata) ---")
    print(profil.ringkasan())

    print("\ndict utk config.json (device_profile):")
    for kunci, nilai in profil.ke_dict().items():
        print(f"  {kunci:18} = {nilai}")

    print("\n--- Skenario spec (dengan injeksi, tanpa ukur mesin) ---")
    skenario = [
        ("Laptop 8 GB (target)", 7.8, 8),    # Test 1 spec
        ("HP 8 GB", 7.5, 8),                 # Test 2 spec
        ("HP low-end 2 GB", 1.9, 4),         # Test 3 spec
    ]
    for nama, ram, cpu in skenario:
        p = DeviceProfile(ram_total_gb=ram, cpu_count=cpu,
                          ram_available_gb=ram * 0.6)
        print(f"  {nama:22} -> {p.classify()} | {p.label_mode()}")

    assert profil.classify() in _KLASIFIKASI_VALID
    assert DeviceProfile(ram_total_gb=1.9, cpu_count=4).classify() == "low"
    assert DeviceProfile(ram_total_gb=7.8, cpu_count=8).classify() == "high"
    print("\nDemo selesai — semua asersi lulus.")
