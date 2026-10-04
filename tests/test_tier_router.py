# ============================================================
# AssistantAI — tests/test_tier_router.py
# ------------------------------------------------------------
# Test klasifikasi 3-tier (heuristik keyword Bahasa Indonesia).
#
# Jalankan:
#   python -m pytest tests/test_tier_router.py -v
#   python tests/test_tier_router.py
# ============================================================

"""Test suite TierRouter: sapaan, tools, kompleks, panjang, word-boundary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tier_router import Tier, TierRouter, _match_keyword  # noqa: E402


def test_sapaan_ringan_ke_tier1():
    router = TierRouter()
    assert router.classify("halo, apa kabar?").tier == Tier.TIER_1
    assert router.classify("terima kasih banyak ya").tier == Tier.TIER_1
    assert router.classify("siapa kamu sih?").tier == Tier.TIER_1


def test_keyword_tools_ke_tier2():
    router = TierRouter()
    assert router.classify("tambah todo beli susu").tier == Tier.TIER_2
    assert router.classify("jam berapa sekarang?").tier == Tier.TIER_2
    assert router.classify("tolong rangkum artikel ini").tier == Tier.TIER_2
    assert router.classify("cari di web tentang berita hari ini").tier == Tier.TIER_2


def test_keyword_kompleks_ke_tier3():
    router = TierRouter()
    assert router.classify("tolong buatkan kode webhook flask").tier == Tier.TIER_3
    assert router.classify("buatkan esai tentang kecerdasan buatan").tier == Tier.TIER_3
    assert router.classify("analisis mendalam data penjualan").tier == Tier.TIER_3


def test_prompt_panjang_naik_tier():
    router = TierRouter()
    # 200 karakter tanpa keyword -> minimal Tier 2
    assert router.classify("a" * 200).tier == Tier.TIER_2
    # 500 karakter -> Tier 3
    assert router.classify("b" * 500).tier == Tier.TIER_3
    # 50 karakter -> tetap Tier 1 (default)
    assert router.classify("c" * 50).tier == Tier.TIER_1


def test_word_boundary_mencegah_salah_tangkap():
    # "akhir" mengandung "hi" — tapi bukan kata "hi" utuh
    router = TierRouter()
    d = router.classify("akhir pekan nanti bagaimana rencananya")
    assert "hi" not in d.matched
    # "hitung" mengandung "hi" juga — justru keyword tier2 "hitung" yang cocok
    d = router.classify("hitung 17 x 23 dong")
    assert d.tier == Tier.TIER_2 and "hitung" in d.matched


def test_keyword_berat_menang_atas_sapaan():
    # "halo" (tier1) + "buatkan kode" (tier3) -> harus TIER_3
    router = TierRouter()
    d = router.classify("halo, tolong buatkan kode scraper ya")
    assert d.tier == Tier.TIER_3
    assert "buatkan kode" in d.matched


def test_override_keyword_kustom():
    router = TierRouter(keywords_tier3=("kereta malam",))
    assert router.classify("naik kereta malam tgl 20").tier == Tier.TIER_3
    # router default tidak mengenal keyword itu
    assert TierRouter().classify("naik kereta malam tgl 20").tier != Tier.TIER_3


def test_match_keyword_dengan_regex_khusus():
    # karakter regex harus aman (di-escape)
    assert _match_keyword("cari hasil +1", ["hasil +1"]) == ["hasil +1"]
    assert _match_keyword("hai semuanya", ["hai"]) == ["hai"]
    assert _match_keyword("semuanya", ["hai"]) == []


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
