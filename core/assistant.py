# ============================================================
# AssistantAI — core/assistant.py
# ------------------------------------------------------------
# ORKESTRATOR ROUTING 3-TIER (fitur baru).
#
# Alur satu kali chat():
#   1. Ada gambar di messages?  -> bypass ke model VISION di cloud
#   2. TierRouter klasifikasi prompt -> TIER_1 / TIER_2 / TIER_3
#   3. Eksekusi chain sesuai tier awal + ESCALATION otomatis:
#        TIER_1 -> TIER_2 -> TIER_3   (tier rendah gagal/menyerah)
#        TIER_3 -> TIER_2             (cloud gagal -> fallback lokal terbaik)
#   4. Semua gagal -> pesan error ramah Bahasa Indonesia + petunjuk perbaikan
#
# Definisi "menyerah": jawaban kosong/whitespace.
# Definisi "gagal": LocalClientError / CloudClientError (koneksi, 429 habis,
# model belum di-pull, dll.).
#
# Dependency injection: `local`, `cloud`, `router` bisa diganti FAKE
# sehingga seluruh logika escalation diuji TANPA Ollama dan tanpa internet.
# ============================================================

"""Orkestrator chat 3-tier AssistantAI.

Cara test cepat (aman walau Ollama/key cloud tidak tersedia —
demo ini memperlihatkan graceful degradation):
    python core/assistant.py
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
    from core.local_client import LocalClient, LocalClientError
    from core.cloud_client import CloudClient, CloudClientError
    from core.tier_router import RoutingDecision, Tier, TierRouter
    from core.vision import punya_gambar
    from core.agent import Agent
except ImportError:                     # dijalankan langsung dari folder core/
    import config as cfg  # type: ignore
    from local_client import LocalClient, LocalClientError
    from cloud_client import CloudClient, CloudClientError
    from tier_router import RoutingDecision, Tier, TierRouter
    from vision import punya_gambar
    from agent import Agent  # type: ignore

logger = logging.getLogger("assistant.assistant")

# Regex pembuang blok penalaran model Qwen3 ("<think>...</think>").
# Hanya pasangan LENGKAP yang dibuang — tag terbuka (mis. jawaban terpotong
# di tengah berpikir) dibiarkan apa adanya agar masalahnya terlihat.
_RE_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _buang_tag_think(teks: str) -> str:
    """Buang blok <think>...</think> milik model reasoning (Qwen3).

    Model tier 2 (qwen3:1.7b) sering menyisipkan proses berpikir di
    dalam jawaban — tidak enak ditampilkan ke user. Fungsi ini membersihkannya
    TANPA menyentuh jawaban model lain (tanpa tag = tanpa perubahan).
    """
    if not teks or "<think>" not in teks:
        return teks
    bersih = _RE_THINK.sub("", teks)
    return bersih.strip()


# ------------------------------------------------------------
# Hasil chat
# ------------------------------------------------------------
@dataclass
class AssistantReply:
    """Hasil satu kali chat — lengkap dengan jejak routing untuk UI/log.

    Atribut:
        text: jawaban akhir (atau pesan gagal yang ramah).
        tier_used: tier yang benar-benar menghasilkan jawaban.
        decision: hasil klasifikasi router (None untuk jalur vision).
        escalation: jejak percobaan berurutan, mis.
            ["tier1: gagal — ...", "tier2: sukses"].
        duration_ms: total waktu (klasifikasi + semua percobaan).
        ok: False bila semua tier gagal.
    """
    text: str
    tier_used: Tier
    decision: Optional[RoutingDecision] = None
    escalation: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    ok: bool = True


# ------------------------------------------------------------
# Assistant
# ------------------------------------------------------------
class Assistant:
    """Orkestrator chat 3-tier.

    Contoh pemakaian nyata (main.py):
        manajer = ConfigManager()
        asisten = Assistant(config=manajer)
        reply = asisten.chat("halo, apa kabar?")
        print(reply.text)

    Contoh test dengan fake (tanpa Ollama/internet):
        asisten = Assistant(local=fake_lokal, cloud=fake_cloud)
    """

    def __init__(
        self,
        config: Optional[Any] = None,     # ConfigManager (opsional)
        local: Optional[Any] = None,      # LocalClient / fake
        cloud: Optional[Any] = None,      # CloudClient / fake
        router: Optional[TierRouter] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[Any] = None,      # ToolRegistry / None (FASE 2)
        agent: Optional[Any] = None,      # Agent custom utk test (opsional)
    ):
        """
        Args:
            config: ConfigManager — bila diberikan, model/token per-tier
                dibaca dari config.json (bagian "tiers"), dan flag
                tools.enabled menentukan apakah agent tool-calling aktif.
            local/cloud/router: dependency injection (utk test & penggantian).
            system_prompt: instruksi awal untuk model (opsional).
            tools: ToolRegistry — bila diberikan, Tier 2 & Tier 3 diproses
                lewat Agent (tool calling). Tier 1 tetap chat polos.
            agent: pengganti Agent bawaan (injection utk test).
        """
        self._config = config
        self.router = router or TierRouter()
        self.system_prompt = system_prompt
        if local is None:
            local = LocalClient()          # error jelas bila openai belum ada
        if cloud is None:
            cloud = CloudClient()
        self.local = local
        self.cloud = cloud
        # --- Tool calling (FASE 2) ---
        self.tools = tools
        self.agent = agent or (Agent(tools) if tools is not None else None)
        # Jejak tools yang dipakai PADA chat() terakhir (dibaca CLI/UI;
        # kosong = chat terakhir murni teks tanpa tool).
        self.tools_terakhir: list[str] = []
        # Statistik pemakaian (dipakai CLI /stats dan monitoring nanti)
        self._statistik: dict[str, int] = {
            "tier1": 0, "tier2": 0, "tier3": 0, "vision": 0,
            "escalasi": 0, "gagal": 0,
        }

    # =========================================================
    # API utama
    # =========================================================
    def chat(
        self,
        prompt: str,
        history: Optional[list[dict]] = None,
        image_url: Optional[str] = None,
    ) -> AssistantReply:
        """Proses satu prompt user melalui routing 3-tier.

        Args:
            prompt: teks pertanyaan/perintah.
            history: riwayat percakapan [{"role": "user"|"assistant", "content": "..."}].
            image_url: URL atau data-URI base64 gambar (opsional) -> jalur vision.

        Raises:
            ValueError: prompt kosong dan tidak ada gambar.
        """
        mulai = time.perf_counter()
        prompt = (prompt or "").strip()
        if not prompt and not image_url:
            raise ValueError("Prompt kosong — tulis pertanyaan dulu.")

        self.tools_terakhir = []          # reset jejak tool utk chat ini
        messages = self._bangun_messages(prompt, history, image_url)

        # --- 1. Bypass vision: ada gambar -> langsung model vision (cloud) ---
        if punya_gambar(messages):
            reply = self._coba_vision(messages)
            reply.duration_ms = (time.perf_counter() - mulai) * 1000
            return reply

        # --- 2. Klasifikasi tier ---
        decision = self.router.classify(prompt)
        logger.info("Router: %s (%s)", decision.tier.value, decision.reason)

        # --- 3. Eksekusi chain + escalation ---
        jalur: list[str] = []
        for tier in self._bangun_chain(decision.tier):
            self._statistik[tier.value] += 1
            try:
                teks = self._panggil_tier(tier, messages)
            except (LocalClientError, CloudClientError) as exc:
                jalur.append(f"{tier.value}: gagal — {exc}")
                logger.warning("Tier %s gagal: %s", tier.value, exc)
                continue
            except Exception as exc:            # jaring error lain agar chain tetap jalan
                jalur.append(f"{tier.value}: gagal — {exc}")
                logger.exception("Tier %s error tak terduga", tier.value)
                continue

            if not teks or not teks.strip():
                # "MENYERAH": model menjawab tapi isinya kosong
                jalur.append(f"{tier.value}: menyerah — jawaban kosong")
                logger.warning("Tier %s menyerah (jawaban kosong)", tier.value)
                continue

            jalur.append(f"{tier.value}: sukses")
            return AssistantReply(
                text=_buang_tag_think(teks).strip(),   # buang <think> Qwen3
                tier_used=tier,
                decision=decision,
                escalation=jalur,
                duration_ms=(time.perf_counter() - mulai) * 1000,
                ok=True,
            )

        # --- 4. Semua tier gagal ---
        self._statistik["gagal"] += 1
        return AssistantReply(
            text=self._pesan_gagal(jalur),
            tier_used=decision.tier,
            decision=decision,
            escalation=jalur,
            duration_ms=(time.perf_counter() - mulai) * 1000,
            ok=False,
        )

    def stats(self) -> dict[str, int]:
        """Statistik pemakaian tier (salinan — aman diubah pemanggil)."""
        return dict(self._statistik)

    # =========================================================
    # Internal
    # =========================================================
    def _bangun_chain(self, tier_awal: Tier) -> list[Tier]:
        """Susun urutan percobaan tier.

        - TIER_1 -> [1, 2, 3]   (escalation naik, bila aktif)
        - TIER_2 -> [2, 3]
        - TIER_3 -> [3, 2]      (cloud gagal -> fallback TURUN ke lokal terbaik;
                                 fallback turun ini SELALU aktif sesuai spec:
                                 "cloud rate limited -> fallback lokal")
        - escalation_enabled=False -> hanya tier awal saja.
        """
        urutan = [Tier.TIER_1, Tier.TIER_2, Tier.TIER_3]
        if tier_awal == Tier.TIER_3:
            return [Tier.TIER_3, Tier.TIER_2]
        chain = urutan[urutan.index(tier_awal):]
        if not self._escalation_aktif():
            chain = chain[:1]
        return chain

    def _panggil_tier(self, tier: Tier, messages: list[dict]) -> str:
        """Panggil klien yang sesuai untuk satu tier (model dari settings).

        Tier 2 & 3 bila tools aktif -> lewat Agent (tool calling loop).
        Error klien (LocalClientError/CloudClientError/AgentMaxIterations)
        dibiarkan naik — chat() yang menangkap utk escalation.
        """
        s = self._tier_settings()
        pakai_agent = (self.agent is not None and self._tools_aktif()
                       and tier != Tier.TIER_1)      # tier 1 selalu chat polos
        if tier == Tier.TIER_1:
            return self.local.chat(messages, model=s["tier1_model"],
                                   max_tokens=s["tier1_max_tokens"])
        if tier == Tier.TIER_2:
            if pakai_agent:
                hasil = self.agent.run(self.local, messages,
                                       model=s["tier2_model"],
                                       max_tokens=s["tier2_max_tokens"])
                self.tools_terakhir.extend(hasil.tools_dipakai)
                return hasil.jawaban
            return self.local.chat(messages, model=s["tier2_model"],
                                   max_tokens=s["tier2_max_tokens"])
        # --- Tier 3 (cloud) ---
        if pakai_agent:
            hasil = self.agent.run(self.cloud, messages, model=s["tier3_model"])
            self.tools_terakhir.extend(hasil.tools_dipakai)
            return hasil.jawaban
        return self.cloud.chat(messages, model=s["tier3_model"])

    def _tools_aktif(self) -> bool:
        """Flag tools.enabled dari config (default cfg.TOOLS_ENABLED=True)."""
        if self._config is not None:
            return bool(self._config.get("tools.enabled", cfg.TOOLS_ENABLED))
        return bool(cfg.TOOLS_ENABLED)

    def _coba_vision(self, messages: list[dict]) -> AssistantReply:
        """Jalur vision: model vision di cloud (fallback lokal tidak ada yang
        mampu vision → bila gagal, kembalikan pesan gagal yang ramah)."""
        jalur: list[str] = []
        self._statistik["vision"] += 1
        model = cfg.CLOUD_MODELS["vision"]
        try:
            teks = self.cloud.chat(messages, model=model)
        except CloudClientError as exc:
            jalur.append(f"vision: gagal — {exc}")
            self._statistik["gagal"] += 1
            logger.warning("Vision gagal: %s", exc)
            return AssistantReply(text=self._pesan_gagal(jalur), tier_used=Tier.VISION,
                                  decision=None, escalation=jalur, ok=False)
        if not teks or not teks.strip():
            jalur.append("vision: menyerah — jawaban kosong")
            self._statistik["gagal"] += 1
            return AssistantReply(text=self._pesan_gagal(jalur), tier_used=Tier.VISION,
                                  decision=None, escalation=jalur, ok=False)
        jalur.append("vision: sukses")
        return AssistantReply(text=_buang_tag_think(teks).strip(), tier_used=Tier.VISION,
                              decision=None, escalation=jalur, ok=True)

    def _bangun_messages(
        self,
        prompt: str,
        history: Optional[list[dict]],
        image_url: Optional[str],
    ) -> list[dict]:
        """Rakit list messages format OpenAI: system + history + prompt/gambar."""
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        for pesan in history or []:
            messages.append({
                "role": pesan.get("role", "user"),
                "content": pesan.get("content", ""),
            })
        if image_url:
            # Format multimodal OpenAI — didukung endpoint vision Ollama
            messages.append({"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _tier_settings(self) -> dict[str, Any]:
        """Baca model & batas token per-tier dari config (atau default cfg)."""
        default = {
            "tier1_model": cfg.TIER1_MODEL,
            "tier1_max_tokens": cfg.TIER1_MAX_TOKENS,
            "tier2_model": cfg.TIER2_MODEL,
            "tier2_max_tokens": cfg.TIER2_MAX_TOKENS,
            "tier3_model": cfg.TIER3_MODEL,
        }
        if self._config is None:
            return default
        return {kunci: self._config.get(f"tiers.{kunci}", nilai)
                for kunci, nilai in default.items()}

    def _escalation_aktif(self) -> bool:
        return bool(self._config.get("tiers.escalation_enabled", cfg.ESCALATION_ENABLED)
                    if self._config is not None else cfg.ESCALATION_ENABLED)

    def _pesan_gagal(self, jalur: list[str]) -> str:
        """Pesan akhir yang ramah + petunjuk perbaikan (Bahasa Indonesia)."""
        rincian = "\n".join(f"  • {j}" for j in jalur) or "  • tidak ada percobaan"
        return (
            "Maaf, belum bisa menjawab — semua tier gagal.\n"
            f"{rincian}\n\n"
            "Cek cepat:\n"
            f"  1. Ollama lokal jalan?  -> `ollama serve`, lalu `ollama pull {cfg.TIER1_MODEL}`\n"
            "  2. API key cloud terpasang? -> set OLLAMA_CLOUD_KEYS (wizard di FASE 4)\n"
            "  3. Jalankan `python main.py --cek` untuk diagnosis lengkap"
        )


# ------------------------------------------------------------
# Demo / test mandiri — memperlihatkan graceful degradation:
# tanpa Ollama & tanpa key cloud, aplikasi tetap "hidup"
# (semua tier gagal -> pesan ramah), TIDAK crash.
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/assistant.py — routing 3-tier (graceful degradation)")
    print("=" * 60)

    asisten = Assistant()
    print(f"\nModel tier 1 : {cfg.TIER1_MODEL}")
    print(f"Model tier 2 : {cfg.TIER2_MODEL}")
    print(f"Model tier 3 : {cfg.TIER3_MODEL}")
    print(f"Key cloud    : {len(asisten.cloud.pool)} key terdaftar")
    print(f"Ollama hidup : {asisten.local.is_available()}")

    print("\n--- Uji 1: prompt ringan (harusnya Tier 1, fallback bila gagal) ---")
    reply = asisten.chat("halo, apa kabar?")
    print(f"Tier dipakai : {reply.tier_used.value} | ok={reply.ok}")
    for langkah in reply.escalation:
        print(f"  jalur: {langkah[:100]}")
    print(f"Jawaban      : {reply.text[:200]}")

    print("\n--- Uji 2: prompt kompleks (harusnya Tier 3) ---")
    reply = asisten.chat("buatkan kode webhook flask lengkap")
    print(f"Tier dipakai : {reply.tier_used.value} | ok={reply.ok}")
    for langkah in reply.escalation:
        print(f"  jalur: {langkah[:100]}")

    print("\n--- Statistik ---")
    print(", ".join(f"{k}={v}" for k, v in asisten.stats().items()))
    print("\nDemo selesai — tanpa crash walau layanan tidak tersedia (by design).")
