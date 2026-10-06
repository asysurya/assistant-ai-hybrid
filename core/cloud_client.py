# ============================================================
# AssistantAI — core/cloud_client.py
# ------------------------------------------------------------
# Klien Ollama CLOUD (https://ollama.com/v1) dengan ROTASI MULTI-KEY.
#
# Aturan handling error (sesuai spesifikasi):
#   HTTP 429 (rate limit) -> key masuk cooldown 30 menit (KeyPool),
#                            lanjut ke key berikutnya
#   HTTP 401/403 (invalid)-> key diistirahatkan juga, lanjut key berikutnya
#   404                   -> nama model salah -> error langsung (rotasi tak
#                            berguna), pesan menyuruh cek Settings > Model
#   koneksi gagal         -> error langsung (cek internet)
#   SEMUA key habis       -> CloudRateLimited / CloudAuthError
#                            -> assistant.py fallback TURUN ke Tier 2 lokal
# ============================================================

"""Klien chat ke Ollama Cloud dengan rotasi API key + cooldown.

Cara test cepat (butuh API key di environment):
    set OLLAMA_CLOUD_KEYS=key1,key2   (Windows)
    python core/cloud_client.py
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
    from core.key_pool import KeyPool, KeyPoolError, NoKeyAvailableError, mask_key
    from core.local_client import ReplyDetail
except ImportError:                     # dijalankan langsung dari folder core/
    import config as cfg  # type: ignore
    from key_pool import KeyPool, KeyPoolError, NoKeyAvailableError, mask_key
    from local_client import ReplyDetail  # type: ignore

# Import "lunak" — modul tetap bisa di-import walau paket openai belum ada.
try:
    from openai import (
        APIConnectionError,
        APIStatusError,
        AuthenticationError,
        NotFoundError,
        OpenAI,
        PermissionDeniedError,
        RateLimitError,
    )
    _OPENAI_TERSEDIA = True
except ImportError:
    _OPENAI_TERSEDIA = False

logger = logging.getLogger("assistant.cloud_client")


# ------------------------------------------------------------
# Exception khusus (assistant.py menangkap ini untuk fallback turun)
# ------------------------------------------------------------
class CloudClientError(Exception):
    """Dasar error Ollama Cloud (koneksi / model tak ada / server error)."""


class CloudRateLimited(CloudClientError):
    """Semua API key sedang cooldown karena 429."""


class CloudAuthError(CloudClientError):
    """Semua API key ditolak (401/403) — key invalid atau expired."""


# ------------------------------------------------------------
# CloudClient
# ------------------------------------------------------------
class CloudClient:
    """Chat ke Ollama Cloud dengan rotasi key otomatis.

    Contoh:
        klien = CloudClient()          # key dari env OLLAMA_CLOUD_KEYS
        jawaban = klien.chat([{"role": "user", "content": "hai"}])

    Rotasi 429 sudah diuji otomatis di tests/test_cloud_client.py
    (memakai SDK palsu, tanpa panggil jaringan sungguhan).
    """

    def __init__(
        self,
        keys: Optional[list[str]] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        key_pool: Optional[KeyPool] = None,   # injection utk test
        client: Optional[Any] = None,         # injection SDK palsu utk test
    ):
        """
        Args:
            keys: daftar API key. Bila None → dibaca dari env OLLAMA_CLOUD_KEYS.
            key_pool: bila diberikan, dipakai apa adanya (mengabaikan `keys`).
            client: objek SDK siap pakai (untuk test tanpa jaringan).
        """
        self.base_url = base_url or cfg.OLLAMA_CLOUD_BASE_URL
        self.timeout = timeout or 120
        if key_pool is not None:
            self.pool = key_pool
        else:
            daftar = list(keys) if keys is not None else cfg.get_cloud_keys_from_env()
            self.pool = KeyPool(daftar)
        self._client = client
        self._cache_klien: dict[str, Any] = {}   # key -> OpenAI client (reuse koneksi)

    # ---------- kesehatan ----------
    def is_available(self) -> bool:
        """True bila ada key terdaftar (belum tentu sehat — 429 dicek saat dipakai)."""
        return len(self.pool) > 0

    # ---------- chat ----------
    def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict]] = None,     # skema OpenAI (Agent tool-calling)
        _detail: bool = False,                  # internal: True -> ReplyDetail
    ) -> str | ReplyDetail:
        """Kirim messages → teks jawaban, dengan rotasi key otomatis.

        Args:
            tools: skema tools format OpenAI; None/kosong -> tidak dikirim.
            _detail: True -> kembalikan ReplyDetail (teks + tool_calls),
                dipakai core/agent.py. Pemanggil normal tidak perlu menyentuh.

        Raises:
            CloudRateLimited: semua key cooldown (429).
            CloudAuthError: semua key ditolak (401/403).
            CloudClientError: masalah lain (koneksi, nama model, server).
        """
        if len(self.pool) == 0:
            raise CloudClientError(
                "Tidak ada API key Ollama Cloud. "
                "Set environment OLLAMA_CLOUD_KEYS, atau tunggu wizard FASE 4."
            )
        model = model or cfg.CLOUD_MODELS["chat"]
        percobaan_maks = max(1, len(self.pool))   # maksimum 1 call per key
        ada_429 = False
        ada_auth = False
        error_terakhir: Optional[Exception] = None

        for _ in range(percobaan_maks):
            # --- ambil key sehat berikutnya (round-robin + cooldown) ---
            try:
                key = self.pool.get_key()
            except NoKeyAvailableError as exc:
                ada_429 = True
                error_terakhir = exc
                break                              # semua key cooldown → stop
            except KeyPoolError as exc:
                raise CloudClientError(str(exc)) from exc

            klien = self._klien_untuk(key)
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": int(max_tokens or 2048),
            }
            if temperature is not None:
                kwargs["temperature"] = float(temperature)
            if tools:                              # jangan kirim list kosong
                kwargs["tools"] = tools

            try:
                resp = self._panggil_sdk(klien, kwargs)
            except RateLimitError:
                ada_429 = True
                self.pool.mark_rate_limited(key)   # cooldown 30 menit utk key ini
                pesan = f"Key {mask_key(key)} kena 429 — cooldown {cfg.KEY_COOLDOWN_SECONDS} detik"
                logger.warning(pesan)
                error_terakhir = CloudRateLimited(pesan)
                continue                           # coba key berikutnya
            except (AuthenticationError, PermissionDeniedError):
                ada_auth = True
                self.pool.mark_rate_limited(key)   # key invalid → diistirahatkan juga
                pesan = f"Key {mask_key(key)} ditolak (401/403) — cek keabsahan key"
                logger.warning(pesan)
                error_terakhir = CloudAuthError(pesan)
                continue
            except NotFoundError as exc:
                # Nama model salah — rotasi key tidak akan menolong.
                raise CloudClientError(
                    f"Model '{model}' tidak tersedia di Ollama Cloud. "
                    f"Cek Settings > Model. ({exc})"
                ) from exc
            except APIConnectionError as exc:
                raise CloudClientError(
                    f"Tidak bisa menghubungi {self.base_url} — cek koneksi internet. ({exc})"
                ) from exc
            except APIStatusError as exc:
                # Error server (5xx dsb.) — bukan soal key, jangan rotasi.
                raise CloudClientError(
                    f"Ollama Cloud error HTTP {getattr(exc, 'status_code', '?')}: {exc}"
                ) from exc
            except TypeError:
                # SDK baru menolak `max_tokens` → ulang dengan nama baru.
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                resp = self._panggil_sdk(klien, kwargs)
            except Exception as exc:               # di luar prediksi — tetap bungkus
                raise CloudClientError(f"Error tak terduga pada Ollama Cloud: {exc}") from exc

            self.pool.mark_success(key)
            if _detail:                        # Agent butuh tool_calls juga
                return self._ke_detail(resp)
            try:
                content = resp.choices[0].message.content or ""
            except (AttributeError, IndexError) as exc:
                raise CloudClientError(
                    f"Format jawaban Ollama Cloud tidak dikenali: {resp!r}"
                ) from exc
            logger.debug("Cloud OK via %s (model=%s, %d karakter)",
                         mask_key(key), model, len(content))
            return content

        # --- semua percobaan habis ---
        if ada_auth:
            raise CloudAuthError(
                f"Semua {len(self.pool)} API key ditolak (401/403). Perbarui key lewat Settings."
            )
        if ada_429:
            raise CloudRateLimited(
                f"Semua {len(self.pool)} API key sedang cooldown (429). "
                f"{error_terakhir}"
            )
        raise CloudClientError(f"Ollama Cloud gagal setelah {percobaan_maks} percobaan. {error_terakhir}")

    def chat_detail(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[dict]] = None,
    ) -> ReplyDetail:
        """Versi LENGKAP dari chat(): teks + tool_calls (dipakai Agent).

        Rotasi key & penanganan 429/401 identik dengan chat() — memang
        satu alur kode, hanya bentuk hasilnya berbeda.
        """
        hasil = self.chat(messages, model=model, max_tokens=max_tokens,
                          temperature=temperature, tools=tools, _detail=True)
        assert isinstance(hasil, ReplyDetail)   # _detail=True dijamin ReplyDetail
        return hasil

    def _ke_detail(self, resp: Any) -> ReplyDetail:
        """Ubah response SDK -> ReplyDetail (TOLERAN utk response minimalis test)."""
        try:
            pilihan = resp.choices[0]
            pesan = pilihan.message
        except (AttributeError, IndexError) as exc:
            raise CloudClientError(
                f"Format jawaban Ollama Cloud tidak dikenali: {resp!r}"
            ) from exc
        konten = getattr(pesan, "content", None) or ""
        daftar_calls: list[dict] = []
        for tc in (getattr(pesan, "tool_calls", None) or []):
            fungsi = getattr(tc, "function", None)
            if fungsi is None or not getattr(fungsi, "name", None):
                logger.warning("tool_calls malformasi dilewati: %r", tc)
                continue
            argumen = getattr(fungsi, "arguments", "{}") or "{}"
            if not isinstance(argumen, str):     # beberapa backend kirim dict
                import json as _json
                argumen = _json.dumps(argumen, ensure_ascii=False)
            daftar_calls.append({
                "id": getattr(tc, "id", "") or "",
                "nama": fungsi.name,
                "argumen_mentah": argumen,
            })
        logger.debug("Cloud OK (%d karakter, %d tool_calls)", len(konten), len(daftar_calls))
        return ReplyDetail(
            konten=konten,
            tool_calls=daftar_calls,
            finish_reason=getattr(pilihan, "finish_reason", "") or "",
        )

    # ---------- internal ----------
    def _klien_untuk(self, key: str) -> Any:
        """Kembalikan SDK client untuk key tertentu (dibuat sekali per key)."""
        if self._client is not None:               # injection test — satu untuk semua
            return self._client
        if not _OPENAI_TERSEDIA:
            raise CloudClientError(
                "Paket 'openai' belum terinstall — jalankan: pip install -r requirements.txt"
            )
        klien = self._cache_klien.get(key)
        if klien is None:
            klien = OpenAI(
                base_url=self.base_url,
                api_key=key,                       # key berbeda → client berbeda
                timeout=self.timeout,
                max_retries=0,                     # rotasi ditangani KeyPool, bukan SDK
            )
            self._cache_klien[key] = klien
        return klien

    def _panggil_sdk(self, klien: Any, kwargs: dict[str, Any]) -> Any:
        """Panggilan SDK terisolasi supaya penanganan error di chat() tetap rapi."""
        return klien.chat.completions.create(**kwargs)


# ------------------------------------------------------------
# Demo / test mandiri (aman tanpa key & tanpa internet)
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/cloud_client.py — Ollama Cloud + rotasi key")
    print("=" * 60)

    klien = CloudClient()
    print(f"\nEndpoint  : {klien.base_url}")
    print(f"Key regis : {len(klien.pool)} ({', '.join(klien.pool.masked_keys) or '-'})")

    if not klien.is_available():
        print("Status    : belum ada API key (set OLLAMA_CLOUD_KEYS untuk mencoba).")
        print("Sesuai desain graceful degradation: demo tetap selesai tanpa error.")
        raise SystemExit(0)

    print("\nMencoba chat dengan gpt-oss:120b-cloud ...")
    try:
        jawaban = klien.chat(
            [{"role": "user", "content": "Balas HANYA dengan kata: OKE"}],
            max_tokens=16,
        )
        print(f"Jawaban: {jawaban!r}")
    except CloudClientError as exc:
        print(f"Gagal: {exc}")

    print("\nStatistik key pool:")
    print(klien.pool.format_stats())
    print("\nDemo selesai.")
