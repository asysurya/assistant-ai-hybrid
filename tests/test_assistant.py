# ============================================================
# AssistantAI — tests/test_assistant.py
# ------------------------------------------------------------
# Test ORKESTRASI 3-TIER + ESCALATION — memakai FAKE client,
# jadi TIDAK butuh Ollama maupun koneksi internet.
#
# Skenario yang diuji:
#   - tier1 sukses langsung
#   - tier1 gagal -> escalate tier2
#   - tier1+2 gagal -> escalate tier3 (cloud)
#   - tier3 (cloud) gagal -> fallback TURUN ke tier2
#   - semua gagal -> pesan ramah, ok=False
#   - gambar -> bypass vision (model vision LOKAL qwen3.5:2b)
#   - escalation_enabled=False -> berhenti di tier awal
#
# Jalankan:
#   python -m pytest tests/test_assistant.py -v
#   python tests/test_assistant.py
# ============================================================

"""Test suite Assistant: chain 1→2→3, fallback 3→2, vision bypass."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as cfg  # noqa: E402
from core.assistant import Assistant  # noqa: E402
from core.cloud_client import CloudRateLimited  # noqa: E402
from core.local_client import LocalClientError  # noqa: E402
from core.tier_router import Tier, TierRouter  # noqa: E402


# ------------------------------------------------------------
# Fake client (meniru antarmuka LocalClient / CloudClient)
# ------------------------------------------------------------
class KlienLokalPalsu:
    """Fake LocalClient: perilaku per nama model ATAU per urutan panggilan.

    - perilaku: dict {model: jawaban/Exception} — dipakai berulang.
    - urutan:   list hasil per panggilan berurutan — DIPERLUKAN sejak
      konsolidasi model: tier 1 & 2 sama-sama qwen3.5:2b sehingga dict
      per-model bertabrakan (satu key untuk dua tier).
    """

    def __init__(self, perilaku: dict | None = None, urutan: list | None = None):
        self.perilaku = perilaku or {}
        self.urutan = list(urutan) if urutan else None
        self.dipanggil: list[str] = []

    def chat(self, messages, model=None, max_tokens=None, temperature=None) -> str:
        self.dipanggil.append(model)
        if self.urutan:
            hasil = self.urutan.pop(0)      # habis -> StopIteration (test gagal jelas)
        else:
            hasil = self.perilaku.get(model, "")
        if isinstance(hasil, Exception):
            raise hasil
        return hasil


class KlienCloudPalsu:
    """Fake CloudClient: perilaku per nama model — str jawaban / Exception."""

    def __init__(self, perilaku: dict | None = None):
        self.perilaku = perilaku or {}
        self.dipanggil: list[str] = []

    def chat(self, messages, model=None, max_tokens=None, temperature=None) -> str:
        self.dipanggil.append(model)
        hasil = self.perilaku.get(model, "Jawaban dari cloud.")
        if isinstance(hasil, Exception):
            raise hasil
        return hasil


class ConfigPalsu:
    """Fake ConfigManager: cuma punya get(path, default) — untuk test flag."""

    def __init__(self, nilai: dict | None = None):
        self.nilai = nilai or {}

    def get(self, path, default=None):
        return self.nilai.get(path, default)


def buat(lokal, cloud, config=None) -> Assistant:
    return Assistant(config=config, local=lokal, cloud=cloud, router=TierRouter())


# ------------------------------------------------------------
# Test
# ------------------------------------------------------------
def test_tier1_sukses_langsung():
    lokal = KlienLokalPalsu({cfg.TIER1_MODEL: "Hai juga!"})
    a = buat(lokal, KlienCloudPalsu())
    reply = a.chat("halo, apa kabar?")
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_1
    assert reply.text == "Hai juga!"
    assert reply.decision is not None and reply.decision.tier == Tier.TIER_1
    assert lokal.dipanggil == [cfg.TIER1_MODEL]      # cloud TIDAK dipanggil


def test_escalasi_tier1_ke_tier2():
    # Tier 1 & 2 kini model SAMA (qwen3.5:2b) — pakai urutan panggilan.
    lokal = KlienLokalPalsu(urutan=[
        LocalClientError("ollama mati"),      # panggilan 1 (tier 1): gagal
        "Jawaban dari tier 2",                # panggilan 2 (tier 2): sukses
    ])
    a = buat(lokal, KlienCloudPalsu())
    reply = a.chat("halo")                            # sapaan -> mulai Tier 1
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_2
    assert len(reply.escalation) == 2                 # gagal lalu sukses
    assert "gagal" in reply.escalation[0]
    assert "sukses" in reply.escalation[1]


def test_escalasi_hingga_tier3():
    lokal = KlienLokalPalsu(urutan=[
        LocalClientError("mati"),             # panggilan 1 (tier 1): gagal
        LocalClientError("mati juga"),        # panggilan 2 (tier 2): gagal
    ])
    cloud = KlienCloudPalsu()                          # default: jawaban cloud
    a = buat(lokal, cloud)
    reply = a.chat("halo")
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_3
    assert cloud.dipanggil == [cfg.TIER3_MODEL]
    assert len(reply.escalation) == 3                  # 1 gagal, 2 gagal, 3 sukses


def test_tier_menyerah_jawaban_kosong_juga_escalate():
    lokal = KlienLokalPalsu(urutan=[
        "   ",                                # panggilan 1: kosong = menyerah
        "Jawaban tier 2",                     # panggilan 2: sukses
    ])
    a = buat(lokal, KlienCloudPalsu())
    reply = a.chat("halo")
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_2
    assert "menyerah" in reply.escalation[0]


def test_tier3_gagal_fallback_turun_ke_tier2():
    lokal = KlienLokalPalsu({cfg.TIER2_MODEL: "Jawaban lokal terbaik"})
    cloud = KlienCloudPalsu({
        cfg.TIER3_MODEL: CloudRateLimited("semua key cooldown"),
    })
    a = buat(lokal, cloud)
    reply = a.chat("buatkan kode webhook flask")      # keyword berat -> TIER_3
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_2             # turun, bukan naik
    assert reply.escalation[0].startswith("tier3")
    assert "sukses" in reply.escalation[1]


def test_semua_tier_gagal_pesan_ramah():
    lokal = KlienLokalPalsu(urutan=[
        LocalClientError("mati"),             # tier 1 gagal
        LocalClientError("mati"),             # tier 2 gagal (model sama)
    ])
    cloud = KlienCloudPalsu({
        cfg.TIER3_MODEL: CloudRateLimited("429 semua"),
    })
    a = buat(lokal, cloud)
    reply = a.chat("halo")
    assert reply.ok is False
    assert "Maaf" in reply.text
    assert "ollama" in reply.text.lower()             # ada petunjuk perbaikan
    assert len(reply.escalation) == 3                 # tiga tier dicoba semua


def test_vision_bypass_ke_model_lokal():
    # Vision kini LOKAL (qwen3.5:2b multimodal) — cloud TIDAK dipanggil.
    lokal = KlienLokalPalsu({cfg.VISION_MODEL: "Ini gambar kucing."})
    cloud = KlienCloudPalsu()
    a = buat(lokal, cloud)
    reply = a.chat("gambar ini apa?", image_url="data:image/png;base64,XXX")
    assert reply.ok is True
    assert reply.tier_used == Tier.VISION
    assert reply.text == "Ini gambar kucing."
    assert lokal.dipanggil == [cfg.VISION_MODEL]      # vision lewat klien lokal
    assert cloud.dipanggil == []                      # cloud dilewati total


def test_escalation_disabled_berhenti_di_tier_awal():
    lokal = KlienLokalPalsu({cfg.TIER1_MODEL: LocalClientError("mati")})
    config = ConfigPalsu({"tiers.escalation_enabled": False})
    a = buat(lokal, KlienCloudPalsu(), config=config)
    reply = a.chat("halo")
    assert reply.ok is False                          # tidak ada kesempatan naik tier
    assert len(reply.escalation) == 1                 # hanya Tier 1 dicoba


def test_prompt_kosong_raise_value_error():
    a = buat(KlienLokalPalsu(), KlienCloudPalsu())
    try:
        a.chat("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("Prompt kosong harusnya raise ValueError")


def test_statistik_tercatat():
    lokal = KlienLokalPalsu({cfg.TIER1_MODEL: "Hai!"})
    a = buat(lokal, KlienCloudPalsu())
    a.chat("halo")
    s = a.stats()
    assert s["tier1"] == 1 and s["gagal"] == 0


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
