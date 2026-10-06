# ============================================================
# AssistantAI — core/tier_router.py
# ------------------------------------------------------------
# ROUTER 3-TIER: memilih "kekuatan" model berdasarkan beratnya prompt.
#
#   TIER_1  qwen3.5:2b     chat ringan & sapaan        (lokal, ~2 GB RAM)
#   TIER_2  qwen3.5:2b     tools + tugas menengah      (lokal, model yang sama)
#   TIER_3  gpt-oss:120b   kompleks / panjang          (cloud)
#   VISION  qwen3.5:2b     bypass: ada gambar          (lokal, multimodal)
#
# Klasifikasi memakai HEURISTIK keyword Bahasa Indonesia: cepat (mikrodetik),
# gratis, dan offline. Kata kunci dicocokkan dengan WORD BOUNDARY sehingga
# "akhir" tidak salah tertangkap sebagai "hi".
#
# TITIK UPGRADE (FASE 10): begitu model router (qwen3-router-id) terlatih,
# cukup ganti isi classify() memanggil classifier LLM — heuristik ini tetap
# dipakai sebagai fallback bila model router belum di-download.
#
# Fallback chain (1 -> 2 -> 3) dan escalation diurus core/assistant.py.
# ============================================================

"""Router 3-tier dengan heuristik keyword Bahasa Indonesia.

Cara test cepat:
    python core/tier_router.py
    python core/tier_router.py "buatkan kode scraper python"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung dari folder core/
    import config as cfg  # type: ignore

logger = logging.getLogger("assistant.tier_router")


# ------------------------------------------------------------
# Enum tier
# ------------------------------------------------------------
class Tier(str, Enum):
    """Level model yang menangani prompt, dari paling ringan ke paling berat."""
    TIER_1 = "tier1"    # chat ringan (lokal kecil)
    TIER_2 = "tier2"    # tool calling / tugas menengah (lokal sedang)
    TIER_3 = "tier3"    # kompleks (cloud)
    VISION = "vision"   # bypass: pesan mengandung gambar -> model vision cloud


@dataclass
class RoutingDecision:
    """Hasil klasifikasi router (untuk log, debug, dan UI nanti)."""
    tier: Tier
    reason: str                                   # alasan singkat Bahasa Indonesia
    matched: list[str] = field(default_factory=list)  # keyword yang cocok


# ------------------------------------------------------------
# Kata kunci heuristik (default).
# Bisa dioverride lewat konstruktor; nanti dibaca dari config.json (FASE 4)
# supaya bisa di-tuning tanpa mengubah kode.
# ------------------------------------------------------------
# Tier 3 — sinyal "berat": coding, analisis mendalam, dokumen panjang
KEYWORD_TIER3: tuple[str, ...] = (
    "buatkan kode", "buat kode", "membuat kode",
    "buatkan program", "buat program", "membuat program",
    "buatkan script", "buat script", "buatkan aplikasi", "buatkan fungsi",
    "refactor", "debug", "perbaiki error", "traceback", "error berikut",
    "arsitektur", "algoritma", "optimasi", "optimalkan",
    "analisis mendalam", "analisa mendalam", "analisis data",
    "esai", "makalah", "proposal", "laporan lengkap",
    "review kode", "code review", "evaluasi mendalam",
    "strategi", "rencana bisnis", "migrasi database", "desain sistem",
    "konversi kode", "porting", "unit test", "integration test",
)

# Tier 2 — sinyal "butuh tools / tugas menengah"
KEYWORD_TIER2: tuple[str, ...] = (
    "todo", "tugas", "ingatkan", "catat", "jadwal", "pengingat",
    "baca file", "tulis file", "buka file", "hapus file",
    "cari di web", "web search", "googling", "cari online",
    "jam berapa", "tanggal berapa", "tanggal sekarang", "waktu sekarang",
    "ringkas", "rangkum", "ringkasan", "buat ringkasan",
    "terjemah", "translate", "hitung", "menghitung", "kalkulasi",
    "konversi", "berapa hasil", "jelaskan cara", "langkah-langkah",
    "langkah langkah", "resep", "daftar", "buat catatan",
)

# Tier 1 — sinyal "ringan": sapaan, terima kasih, pertanyaan tentang bot
KEYWORD_TIER1: tuple[str, ...] = (
    "hai", "halo", "hallo", "hei", "hi",
    "pagi", "siang", "sore", "malam",
    "apa kabar", "terima kasih", "makasih", "thanks", "thank you",
    "oke", "sip", "mantap", "bagus",
    "siapa kamu", "kamu siapa", "nama kamu", "bisa apa",
    "test", "tes", "ping",
)


def _match_keyword(prompt_lower: str, keywords: Iterable[str]) -> list[str]:
    """Cocokkan keyword dengan word boundary (\b) agar bebas salah tangkap.

    Contoh: keyword "hi" TIDAK akan cocok dengan kata "akhir" karena di depan
    huruf 'h' ada huruf 'k' (bagian dari kata), bukan batas kata.
    """
    cocok: list[str] = []
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw) + r"\b", prompt_lower):
            cocok.append(kw)
    return cocok


# ------------------------------------------------------------
# TierRouter
# ------------------------------------------------------------
class TierRouter:
    """Klasifikasi prompt -> Tier (1/2/3), memakai heuristik keyword.

    Contoh:
        router = TierRouter()
        keputusan = router.classify("halo, apa kabar?")
        print(keputusan.tier)   # Tier.TIER_1
    """

    def __init__(
        self,
        keywords_tier1: Optional[Iterable[str]] = None,
        keywords_tier2: Optional[Iterable[str]] = None,
        keywords_tier3: Optional[Iterable[str]] = None,
        tier2_prompt_length: Optional[int] = None,
        tier3_prompt_length: Optional[int] = None,
    ):
        """
        Args:
            keywords_tier1/2/3: override daftar keyword (default dari modul ini).
            tier2_prompt_length: ambang panjang -> minimal Tier 2 (default cfg).
            tier3_prompt_length: ambang panjang -> Tier 3 (default cfg.LOCAL_MAX_PROMPT_LENGTH).
        """
        self.kw_tier1 = tuple(keywords_tier1) if keywords_tier1 else KEYWORD_TIER1
        self.kw_tier2 = tuple(keywords_tier2) if keywords_tier2 else KEYWORD_TIER2
        self.kw_tier3 = tuple(keywords_tier3) if keywords_tier3 else KEYWORD_TIER3
        self.ambang_tier2 = tier2_prompt_length or cfg.TIER2_PROMPT_LENGTH
        self.ambang_tier3 = tier3_prompt_length or cfg.LOCAL_MAX_PROMPT_LENGTH

    def classify(self, prompt: str) -> RoutingDecision:
        """Klasifikasi satu prompt -> RoutingDecision.

        Urutan pengecekan SENGAJA dari tier paling berat ke paling ringan:
        1. keyword Tier 3  -> TIER_3   (sinyal berat selalu menang atas sapaan)
        2. panjang > 400   -> TIER_3
        3. keyword Tier 2  -> TIER_2   (butuh tools / tugas menengah)
        4. panjang > 150   -> TIER_2
        5. keyword Tier 1  -> TIER_1
        6. default         -> TIER_1   (prompt pendek tanpa sinyal = ringan)

        Titik upgrade FASE 10: bila model router (qwen3-router-id) tersedia,
        panggil classifier LLM di sini dan pakai heuristik ini sebagai fallback.
        """
        prompt_lower = (prompt or "").strip().lower()

        # --- 1. keyword kompleks (Tier 3) ---
        cocok3 = _match_keyword(prompt_lower, self.kw_tier3)
        if cocok3:
            return RoutingDecision(Tier.TIER_3, "keyword kompleks/berat", cocok3)

        # --- 2. prompt sangat panjang (Tier 3) ---
        if len(prompt) > self.ambang_tier3:
            return RoutingDecision(
                Tier.TIER_3, f"prompt sangat panjang (>{self.ambang_tier3} karakter)", [])

        # --- 3. keyword tools / menengah (Tier 2) ---
        cocok2 = _match_keyword(prompt_lower, self.kw_tier2)
        if cocok2:
            return RoutingDecision(Tier.TIER_2, "keyword tools/tugas menengah", cocok2)

        # --- 4. prompt cukup panjang (minimal Tier 2) ---
        if len(prompt) > self.ambang_tier2:
            return RoutingDecision(
                Tier.TIER_2, f"prompt cukup panjang (>{self.ambang_tier2} karakter)", [])

        # --- 5. sapaan / chat ringan (Tier 1) ---
        cocok1 = _match_keyword(prompt_lower, self.kw_tier1)
        if cocok1:
            return RoutingDecision(Tier.TIER_1, "sapaan/chat ringan", cocok1)

        # --- 6. default: ringan ---
        return RoutingDecision(
            Tier.TIER_1, "prompt pendek tanpa sinyal khusus -> dianggap ringan", [])

    def explain(self, prompt: str) -> str:
        """Ringkasan keputusan dalam teks rapi (untuk CLI --route dan debugging)."""
        d = self.classify(prompt)
        cocok = ", ".join(d.matched) if d.matched else "-"
        return (
            f"Teks     : {(prompt or '')[:80]}{'...' if len(prompt or '') > 80 else ''}\n"
            f"Tier     : {d.tier.value}\n"
            f"Alasan   : {d.reason}\n"
            f"Cocok    : {cocok}\n"
            f"Panjang  : {len(prompt or '')} karakter"
        )


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("DEMO core/tier_router.py — klasifikasi 3-tier")
    print("=" * 60)

    # Kalau user memberi argumen, klasifikasikan teks itu saja:
    #   python core/tier_router.py "buatkan kode scraper"
    if len(sys.argv) > 1:
        print("\n" + TierRouter().explain(" ".join(sys.argv[1:])))
        sys.exit(0)

    router = TierRouter()
    contoh: list[tuple[str, str]] = [
        ("halo, apa kabar?",                        "sapaan ringan"),
        ("terima kasih banyak ya!",                 "ucapan terima kasih"),
        ("tambah todo: beli susu besok pagi",       "butuh tools (todo)"),
        ("jam berapa sekarang?",                    "butuh tools (waktu)"),
        ("tolong rangkum artikel ini",              "tugas menengah"),
        ("buatkan kode webhook flask lengkap",      "coding -> berat"),
        ("buatkan esai 1000 kata tentang AI",       "dokumen -> berat"),
        ("analisis mendalam data penjualan Q3",     "analisis -> berat"),
        ("a" * 500,                                 "prompt sangat panjang"),
    ]
    for teks, label in contoh:
        d = router.classify(teks)
        tampil = teks[:40] + ("..." if len(teks) > 40 else "")
        print(f"\n[{label}]")
        print(f"  \"{tampil}\"")
        print(f"  -> {d.tier.value} ({d.reason})")

    print("\nDemo selesai. Coba juga: python core/tier_router.py \"prompt Anda\"")
