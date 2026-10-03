# ============================================================
# AssistantAI — core/key_pool.py
# ------------------------------------------------------------
# Pool API key Ollama Cloud dengan:
#   - Rotasi ROUND-ROBIN antar key yang sehat
#   - COOLDOWN 30 menit (KEY_COOLDOWN_SECONDS) untuk key yang kena
#     rate limit HTTP 429
#   - Kalau SEMUA key sedang cooldown -> raise NoKeyAvailableError;
#     lapisan di atas (cloud_client, lanjutan FASE 1) menangkap ini
#     lalu fallback otomatis ke model lokal
#   - THREAD-SAFE (dipakai bersama UI thread + worker thread)
#
# KEAMANAN:
#   - Key TIDAK PERNAH ditulis utuh ke log — selalu lewat mask_key().
#   - File ini TIDAK menyimpan key; list key dioper dari luar
#     (env var untuk sekarang, keyring/keys.enc di FASE 4).
# ============================================================

"""Pool API key dengan rotasi round-robin dan cooldown anti rate-limit.

Cara test cepat:
    python core/key_pool.py
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung: python core/key_pool.py
    import config as cfg  # type: ignore

logger = logging.getLogger("assistant.key_pool")


# ------------------------------------------------------------
# Exception khusus
# ------------------------------------------------------------
class KeyPoolError(Exception):
    """Dasar error untuk semua masalah KeyPool."""


class NoKeyAvailableError(KeyPoolError):
    """Semua API key sedang cooldown (rate limit).

    Atribut:
        wait_seconds: perkiraan detik tunggu sampai ada key yang siap lagi.
    """

    def __init__(self, message: str, wait_seconds: float = 0.0):
        super().__init__(message)
        self.wait_seconds = wait_seconds


# ------------------------------------------------------------
# Util masking (jangan pernah bocorkan key di log)
# ------------------------------------------------------------
def mask_key(key: str) -> str:
    """Samarkan isi key untuk log/UI, mis. "sk-demo-abcdefgh12345678" -> "sk-dem...5678"."""
    key = (key or "").strip()
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


# ------------------------------------------------------------
# Status satu key
# ------------------------------------------------------------
@dataclass
class KeyState:
    """Status satu API key di dalam pool.

    Catatan desain: perhitungan waktu (is_available_at / cooldown_remaining_at)
    menerima `now` sebagai argumen supaya mudah di-test dengan jam palsu.
    """
    key: str
    cooldown_until: float = 0.0     # timestamp epoch saat cooldown berakhir
    total_requests: int = 0         # berapa kali key ini dipakai
    total_rate_limited: int = 0     # berapa kali kena 429
    last_used: float = 0.0          # timestamp terakhir dipakai

    def is_available_at(self, now: float) -> bool:
        """Key boleh dipakai bila waktu sekarang sudah lewat masa cooldown."""
        return now >= self.cooldown_until

    def cooldown_remaining_at(self, now: float) -> float:
        """Sisa detik cooldown (0 bila sudah bebas)."""
        return max(0.0, self.cooldown_until - now)


# ------------------------------------------------------------
# KeyPool
# ------------------------------------------------------------
class KeyPool:
    """Pool API key: rotasi round-robin + cooldown untuk key yang kena 429.

    Contoh pemakaian di cloud_client (lanjutan FASE 1):

        pool = KeyPool(daftar_key)
        for attempt in range(len(pool)):
            key = pool.get_key()
            try:
                response = call_ollama_cloud(key, messages)
                pool.mark_success(key)
                return response
            except RateLimitError:
                pool.mark_rate_limited(key)   # key ini cooldown 30 menit
        # semua key kena 429 -> fallback ke model lokal
    """

    def __init__(
        self,
        keys: Optional[Iterable[str]] = None,
        cooldown_seconds: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        """
        Args:
            keys: daftar API key awal. Entri kosong & duplikat dibuang otomatis.
            cooldown_seconds: lama cooldown key setelah 429.
                Default: cfg.KEY_COOLDOWN_SECONDS (1800 detik / 30 menit).
            clock: fungsi sumber waktu (default time.time). Di-inject supaya
                test cooldown tidak perlu sleep — cepat dan deterministik.
        """
        self._clock: Callable[[], float] = clock or time.time
        self._cooldown = cfg.KEY_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
        self._lock = threading.RLock()
        self._states: "OrderedDict[str, KeyState]" = OrderedDict()
        self._rr_index = 0                      # penunjuk round-robin
        for k in keys or []:
            k = (k or "").strip()
            if k and k not in self._states:
                self._states[k] = KeyState(key=k)
        if not self._states:
            logger.warning("KeyPool dibuat TANPA API key — fitur cloud akan nonaktif.")

    # ---------- pengambilan key ----------
    def get_key(self) -> str:
        """Ambil key sehat berikutnya (round-robin).

        Returns:
            Satu API key yang tidak sedang cooldown.

        Raises:
            KeyPoolError: pool tidak punya key sama sekali.
            NoKeyAvailableError: semua key sedang cooldown (sisa tunggu
                ada di atribut wait_seconds -> sinyal fallback ke lokal).
        """
        now = self._clock()
        with self._lock:
            if not self._states:
                raise KeyPoolError(
                    "Tidak ada API key terdaftar. Tambahkan key lewat Settings "
                    "atau environment variable OLLAMA_CLOUD_KEYS."
                )
            states = list(self._states.values())
            n = len(states)
            for i in range(n):
                idx = (self._rr_index + i) % n
                st = states[idx]
                if st.is_available_at(now):
                    self._rr_index = (idx + 1) % n   # geser penunjuk utk permintaan berikut
                    st.total_requests += 1
                    st.last_used = now
                    logger.debug("KeyPool: memakai key %s", mask_key(st.key))
                    return st.key
            # Tidak ada key yang tersedia -> info tunggu tercepat + saran fallback
            wait = min(st.cooldown_remaining_at(now) for st in states)
            menit, detik = divmod(int(wait + 0.999), 60)   # pembulatan ke atas
            raise NoKeyAvailableError(
                f"Semua {n} API key sedang cooldown (rate limit 429). "
                f"Coba lagi dalam {menit} menit {detik} detik, "
                f"atau fallback ke model lokal.",
                wait_seconds=wait,
            )

    # ---------- penandaan status ----------
    def mark_rate_limited(self, key: str) -> bool:
        """Tandai key kena HTTP 429 -> masuk cooldown selama `cooldown_seconds`.

        Returns:
            True bila key terdaftar dan ditandai; False bila key tidak dikenal.
        """
        with self._lock:
            st = self._states.get((key or "").strip())
            if st is None:
                logger.warning("mark_rate_limited: key %s tidak terdaftar", mask_key(key))
                return False
            st.cooldown_until = self._clock() + self._cooldown
            st.total_rate_limited += 1
            logger.warning(
                "Key %s kena rate limit -> cooldown %.0f detik",
                mask_key(st.key), self._cooldown,
            )
            return True

    def mark_success(self, key: str) -> bool:
        """Tandai panggilan BERHASIL -> key dipastikan bebas cooldown.

        Dipanggil cloud_client setelah response sukses, supaya key yang sempat
        kena cooldown karena error sesaat langsung bisa dipakai lagi.
        """
        with self._lock:
            st = self._states.get((key or "").strip())
            if st is None:
                return False
            st.cooldown_until = 0.0
            return True

    # ---------- kelola isi pool ----------
    def add_key(self, key: str) -> bool:
        """Tambah key saat runtime (nanti dipakai Settings > API, FASE 6).

        Returns:
            False bila key kosong atau sudah ada di pool.
        """
        k = (key or "").strip()
        with self._lock:
            if not k or k in self._states:
                return False
            self._states[k] = KeyState(key=k)
            logger.info("Key baru ditambahkan: %s", mask_key(k))
            return True

    def remove_key(self, key: str) -> bool:
        """Hapus key dari pool.

        Returns:
            False bila key tidak ditemukan.
        """
        k = (key or "").strip()
        with self._lock:
            if k not in self._states:
                return False
            del self._states[k]
            self._rr_index = 0      # reset penunjuk supaya indeks tetap valid
            logger.info("Key dihapus: %s", mask_key(k))
            return True

    # ---------- inspeksi ----------
    def __len__(self) -> int:
        """Jumlah key yang terdaftar di pool."""
        with self._lock:
            return len(self._states)

    @property
    def masked_keys(self) -> list[str]:
        """Daftar key dalam bentuk tersamarkan (aman untuk log/UI)."""
        with self._lock:
            return [mask_key(k) for k in self._states]

    def available_count(self) -> int:
        """Berapa key yang saat ini bebas cooldown."""
        now = self._clock()
        with self._lock:
            return sum(1 for st in self._states.values() if st.is_available_at(now))

    def next_available_in(self) -> float:
        """Detik tunggu sampai ada key tersedia (0 bila sudah ada yang siap)."""
        now = self._clock()
        with self._lock:
            return min(
                (st.cooldown_remaining_at(now) for st in self._states.values()),
                default=0.0,
            )

    def format_stats(self) -> str:
        """Statistik pool dalam tabel teks rapi (untuk log/debug/Settings)."""
        with self._lock:
            now = self._clock()
            header = (
                f"{'#':<3} {'KEY':<20} {'STATUS':<10} "
                f"{'COOLDOWN(s)':>11} {'DIPAKAI':>8} {'429':>5}"
            )
            baris = [header, "-" * len(header)]
            for i, st in enumerate(self._states.values(), start=1):
                sisa = st.cooldown_remaining_at(now)
                status = "tersedia" if st.is_available_at(now) else "cooldown"
                sisa_str = f"{sisa:.0f}" if sisa > 0 else "-"
                baris.append(
                    f"{i:<3} {mask_key(st.key):<20} {status:<10} "
                    f"{sisa_str:>11} {st.total_requests:>8} {st.total_rate_limited:>5}"
                )
            return "\n".join(baris)


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/key_pool.py — rotasi key + cooldown 429")
    print("=" * 60)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")

    # Cooldown dipendekkan jadi 2 detik HANYA untuk demo (aslinya 1800 detik)
    demo_keys = ["sk-demo-aaaa11112222", "sk-demo-bbbb33334444", "sk-demo-cccc55556666"]
    pool = KeyPool(keys=demo_keys, cooldown_seconds=2.0)

    print("\n1) Rotasi round-robin 5x permintaan:")
    for i in range(5):
        print(f"   permintaan #{i + 1} -> {mask_key(pool.get_key())}")

    print("\n2) Simulasi SEMUA key kena HTTP 429:")
    for k in demo_keys:
        pool.mark_rate_limited(k)
    try:
        pool.get_key()
        print("   GAGAL: seharusnya raise NoKeyAvailableError")
    except NoKeyAvailableError as exc:
        print(f"   OK — NoKeyAvailableError: {exc}")
        print("   -> sinyal bagi cloud_client untuk fallback ke model lokal")

    print("\n3) Tunggu cooldown demo (2.1 detik) lalu coba lagi...")
    time.sleep(2.1)
    print(f"   permintaan baru -> {mask_key(pool.get_key())} (cooldown habis)")

    print("\n4) Statistik pool:")
    print(pool.format_stats())
    print("\nDemo selesai.")
