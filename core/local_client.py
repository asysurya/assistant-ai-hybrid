# ============================================================
# AssistantAI — core/local_client.py
# ------------------------------------------------------------
# Klien Ollama LOKAL (http://localhost:11434/v1) via SDK `openai`
# (API Ollama kompatibel OpenAI). Dipakai oleh Tier 1 dan Tier 2.
#
# Prinsip desain:
#   - max_retries=0 → gagal cepat, supaya escalation antar tier terasa instan
#   - Semua error dibungkus LocalClientError dengan pesan SOLUSI yang jelas
#     (mis. "jalankan ollama serve" / "ollama pull <model>")
#   - is_available(): ping cepat 2 detik untuk cek Ollama hidup/tidak
#   - import openai sifatnya "lunak": bila paket belum terinstall, modul
#     tetap bisa di-import (mengikuti prinsip graceful degradation)
# ============================================================

"""Klien chat ke Ollama lokal (API kompatibel OpenAI).

Cara test cepat (perlu Ollama jalan di localhost:11434):
    python core/local_client.py
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung dari folder core/
    import config as cfg  # type: ignore

# Import "lunak": modul tetap bisa di-import walau paket openai belum ada.
try:
    from openai import APIConnectionError, APIStatusError, NotFoundError, OpenAI
    _OPENAI_TERSEDIA = True
except ImportError:
    _OPENAI_TERSEDIA = False

logger = logging.getLogger("assistant.local_client")


# ------------------------------------------------------------
# Exception khusus (assistant.py menangkap ini untuk escalation)
# ------------------------------------------------------------
class LocalClientError(Exception):
    """Gagal memanggil Ollama lokal (tidak jalan / timeout / error lain)."""


class LocalModelNotAvailable(LocalClientError):
    """Model lokal belum di-pull. Solusi: `ollama pull <model>`."""


# ------------------------------------------------------------
# LocalClient
# ------------------------------------------------------------
class LocalClient:
    """Chat ke model lokal via endpoint OpenAI-compatible milik Ollama.

    Contoh:
        klien = LocalClient()
        if klien.is_available():
            jawaban = klien.chat(
                [{"role": "user", "content": "halo"}],
                model="qwen2.5:0.5b",
                max_tokens=512,
            )
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        api_key: str = "ollama",          # dummy — Ollama lokal tak butuh key
        client: Optional[Any] = None,     # injection utk test (fake SDK)
    ):
        """
        Args:
            base_url: default dari cfg.OLLAMA_LOCAL_BASE_URL.
            timeout: detik; default cfg.OLLAMA_LOCAL_TIMEOUT (90).
            client: objek SDK siap pakai (untuk test) — bila diberikan,
                semua parameter lain diabaikan.
        """
        self.base_url = base_url or cfg.OLLAMA_LOCAL_BASE_URL
        self.timeout = timeout or cfg.OLLAMA_LOCAL_TIMEOUT
        self._api_key = api_key
        self._client = client
        if client is None:
            if not _OPENAI_TERSEDIA:
                raise LocalClientError(
                    "Paket 'openai' belum terinstall — jalankan: pip install -r requirements.txt"
                )
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self._api_key,
                timeout=self.timeout,
                max_retries=0,            # gagal cepat → escalation tetap responsif
            )

    # ---------- kesehatan ----------
    def is_available(self, timeout: float = 2.0) -> bool:
        """Ping cepat ke daftar model (timeout 2 detik).

        Dipakai assistant untuk MEMOTONG tier lokal tanpa menunggu timeout
        penuh bila Ollama memang sedang tidak jalan.
        """
        if not _OPENAI_TERSEDIA:
            return False
        try:
            ping = OpenAI(
                base_url=self.base_url,
                api_key=self._api_key,
                timeout=timeout,
                max_retries=0,
            )
            ping.models.list()
            return True
        except Exception as exc:          # apa pun itu → lokal dianggap tidak siap
            logger.debug("Ollama lokal tidak tersedia: %s", exc)
            return False

    # ---------- chat ----------
    def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Kirim messages → kembalikan TEKS jawaban (bisa kosong bila model aneh).

        Raises:
            LocalModelNotAvailable: model belum di-pull (404).
            LocalClientError: masalah lain (koneksi, HTTP, format jawaban).
        """
        model = model or cfg.OLLAMA_LOCAL_MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens or cfg.OLLAMA_LOCAL_MAX_TOKENS),
        }
        if temperature is not None:
            kwargs["temperature"] = float(temperature)

        resp = self._panggil_sdk(kwargs)

        try:
            content = resp.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise LocalClientError(
                f"Format jawaban Ollama lokal tidak dikenali: {resp!r}"
            ) from exc
        logger.debug("Lokal OK (model=%s, %d karakter)", model, len(content))
        return content

    def _panggil_sdk(self, kwargs: dict[str, Any]) -> Any:
        """Panggil SDK + terjemahkan error ke exception lokal yang jelas.

        Catatan kompatibilitas: SDK openai versi baru mengganti parameter
        `max_tokens` -> `max_completion_tokens`. Bila versi baru menolak,
        kita coba ulang sekali dengan nama parameter penggantinya.
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except TypeError:
            if "max_tokens" in kwargs:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                return self._client.chat.completions.create(**kwargs)
            raise
        except APIConnectionError as exc:
            raise LocalClientError(
                f"Ollama lokal tidak bisa dihubungi di {self.base_url}. "
                f"Pastikan `ollama serve` jalan. ({exc})"
            ) from exc
        except NotFoundError as exc:
            raise LocalModelNotAvailable(
                f"Model '{kwargs.get('model')}' belum ada di Ollama. "
                f"Jalankan: ollama pull {kwargs.get('model')}"
            ) from exc
        except APIStatusError as exc:
            raise LocalClientError(
                f"Ollama lokal error HTTP {getattr(exc, 'status_code', '?')}: {exc}"
            ) from exc


# ------------------------------------------------------------
# Demo / test mandiri (aman walau Ollama tidak jalan)
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/local_client.py — chat ke Ollama lokal")
    print("=" * 60)

    try:
        klien = LocalClient()
    except LocalClientError as exc:
        print(f"\nGagal menyiapkan klien: {exc}")
        raise SystemExit(1)

    print(f"Endpoint   : {klien.base_url}")
    print(f"Ollama hidup? {'YA' if klien.is_available() else 'TIDAK (mulai: ollama serve)'}")

    if klien.is_available():
        print(f"\nMencoba chat dengan {cfg.TIER1_MODEL} ...")
        try:
            jawaban = klien.chat(
                [{"role": "user", "content": "Balas HANYA dengan kata: OKE"}],
                model=cfg.TIER1_MODEL,
                max_tokens=16,
            )
            print(f"Jawaban: {jawaban!r}")
        except LocalClientError as exc:
            print(f"Gagal: {exc}")
    else:
        print("\nSesuai desain graceful degradation: demo tetap selesai tanpa error.")

    print("\nDemo selesai.")
