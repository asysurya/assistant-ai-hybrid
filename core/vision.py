# ============================================================
# AssistantAI — core/vision.py
# ------------------------------------------------------------
# Helper VISION: mendeteksi gambar di dalam struktur messages
# format OpenAI (dipakai Ollama juga, karena API-nya kompatibel).
#
# Aturan routing (sesuai spesifikasi):
#   Ada gambar di pesan -> LANGSUNG model vision di cloud (bypass router).
#   Deteksi ini jalan SEBELUM klasifikasi tier di core/assistant.py.
# ============================================================

"""Deteksi gambar (image_url) dalam messages format OpenAI chat.

Cara test cepat:
    python core/vision.py
"""

from __future__ import annotations

from typing import Any


def ambil_url_gambar(messages: list[dict]) -> list[str]:
    """Kumpulkan semua URL/base64 gambar dari list messages.

    Struktur yang didukung (format OpenAI multimodal):
        {"role": "user", "content": [
            {"type": "text", "text": "apa ini?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]}

    Returns:
        Daftar URL gambar (bisa base64 data-URI) — kosong bila tidak ada.
    """
    urls: list[str] = []
    for pesan in messages or []:
        content = pesan.get("content") if isinstance(pesan, dict) else None
        if not isinstance(content, list):
            continue
        for bagian in content:
            if not isinstance(bagian, dict):
                continue
            if bagian.get("type") == "image_url":
                gambar = bagian.get("image_url")
                url = gambar.get("url") if isinstance(gambar, dict) else None
                if url:
                    urls.append(str(url))
    return urls


def punya_gambar(messages: list[dict]) -> bool:
    """True bila ada minimal satu gambar di messages — sinyal bypass ke vision."""
    return len(ambil_url_gambar(messages)) > 0


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/vision.py — deteksi gambar di messages")
    print("=" * 60)

    teks_saja = [{"role": "user", "content": "halo, apa kabar?"}]
    dengan_gambar = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "gambar ini isi apa?"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ],
    }]

    print(f"\nmessages teks saja  -> punya_gambar = {punya_gambar(teks_saja)}")
    print(f"messages + gambar   -> punya_gambar = {punya_gambar(dengan_gambar)}")
    print(f"URL terdeteksi      -> {ambil_url_gambar(dengan_gambar)}")

    assert punya_gambar(teks_saja) is False
    assert punya_gambar(dengan_gambar) is True
    assert len(ambil_url_gambar(dengan_gambar)) == 1
    print("\nDemo selesai — semua asersi lulus.")
