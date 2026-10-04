# ============================================================
# AssistantAI — core/agent.py
# ------------------------------------------------------------
# LOOP TOOL-CALLING (FASE 2) — otak di balik "model bisa bertindak".
#
# Alur:
#   1. Kirim messages + skema tools ke model (Tier 2 lokal / Tier 3 cloud)
#   2. Model menjawab TEKS            -> selesai, jawaban dikembalikan
#      Model menjawab TOOL_CALLS      -> eksekusi tiap tool via registry,
#                                        hasil dikirim balik, ulangi (maks
#                                        AGENT_MAX_ITERATIONS, default 5)
#   3. Argumen JSON rusak?            -> error dikirim balik ke model
#                                        (model berkesempatan mengoreksi)
#   4. Tool menulis (tulis_file)      -> minta izin user via callback
#                                        konfirmasi; tanpa izin = ditolak
#   5. Batas iterasi habis            -> AgentMaxIterations
#                                        (assistant memperlakukan ini
#                                        seperti tier gagal -> escalation)
#
# CATATAN DESAIN:
#   - Agent TIDAK punya klien sendiri — klien (lokal/cloud) dioper dari
#     luar (dependency injection), sehingga test mudah & pola pemakaian
#     tetap fleksibel.
#   - Semua pesan assistant + tool dikirim ulang dalam format OpenAI
#     standar (role "assistant" + "tool_calls", role "tool" + tool_call_id)
#     — kompatibel dengan endpoint /v1 milik Ollama.
# ============================================================

"""Agent loop tool-calling: model -> tool_calls -> eksekusi -> jawaban akhir.

Cara test cepat (tanpa Ollama — memakai klien palsu):
    python core/agent.py
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
    from core.tools import Tool, ToolRegistry, ToolResult
except ImportError:                     # dijalankan langsung dari folder core/
    import config as cfg  # type: ignore
    from tools import Tool, ToolRegistry, ToolResult  # type: ignore

logger = logging.getLogger("assistant.agent")

# Tipe callback konfirmasi: (tool, argumen) -> bool izin user
KonfirmasiCallback = Callable[[Tool, dict], bool]


# ------------------------------------------------------------
# Exception khusus
# ------------------------------------------------------------
class AgentError(Exception):
    """Dasar error agent."""


class AgentMaxIterations(AgentError):
    """Model terjebak memanggil tool melebihi batas iterasi tanpa jawaban final.

    Assistant memperlakukan error ini seperti tier gagal -> escalation
    ke tier berikutnya (model lebih besar biasanya berhenti lebih cepat).
    """


# ------------------------------------------------------------
# Hasil agent
# ------------------------------------------------------------
@dataclass
class AgentResult:
    """Hasil akhir satu kali run agent.

    Atribut:
        jawaban: teks jawaban final model (bisa kosong bila model aneh).
        iterasi: berapa kali model dipanggil sampai jawaban final.
        jejak: log ringkas per langkah (untuk CLI /stats dan debugging).
        tools_dipakai: daftar "nama(argumen)" yang benar-benar dieksekusi.
    """
    jawaban: str
    iterasi: int = 0
    jejak: list[str] = field(default_factory=list)
    tools_dipakai: list[str] = field(default_factory=list)


# ------------------------------------------------------------
# Agent
# ------------------------------------------------------------
class Agent:
    """Loop tool-calling antara model dan ToolRegistry.

    Contoh:
        agent = Agent(ToolRegistry())
        hasil = agent.run(klien_lokal, messages, model="qwen3:1.7b")
        print(hasil.jawaban)
        print(hasil.jejak)        # jejak tool yang dipakai

    Konfirmasi tool berbahaya:
        agent.konfirmasi = lambda tool, args: tanya_user(tool.nama, args)
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        max_iterations: Optional[int] = None,
        konfirmasi: Optional[KonfirmasiCallback] = None,
    ):
        """
        Args:
            registry: kumpulan tool. None -> agent berperilaku sebagai
                chat biasa (tanpa tools) — tetap berguna bila tools
                dinonaktifkan dari config.
            max_iterations: batas loop (default cfg.AGENT_MAX_ITERATIONS = 5).
            konfirmasi: callback (tool, argumen) -> bool. Dipanggil HANYA
                untuk tool ber-flag butuh_konfirmasi. None -> tindakan
                tertulis otomatis DITOLAK (aman secara default).
        """
        self.registry = registry
        self.max_iterations = max(1, int(max_iterations or cfg.AGENT_MAX_ITERATIONS))
        self.konfirmasi = konfirmasi

    # =========================================================
    # API utama
    # =========================================================
    def run(
        self,
        client: Any,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> AgentResult:
        """Jalankan loop sampai jawaban final / batas iterasi.

        Args:
            client: objek klien yang punya chat_detail() — LocalClient,
                CloudClient, atau fake test dengan antarmuka sama.
            messages: riwayat+prompt format OpenAI (dari Assistant).
            model/max_tokens/temperature: diteruskan ke klien.

        Raises:
            AgentMaxIterations: batas iterasi habis tanpa jawaban final.
            (Error klien — LocalClientError/CloudClientError — TIDAK
            dibungkus, dibiarkan naik agar escalation Assistant tetap bekerja.)
        """
        skema = self.registry.skema_openai() if (self.registry and len(self.registry)) else []
        pesan = list(messages)              # salin — jangan mutasi milik pemanggil
        jejak: list[str] = []
        dipakai: list[str] = []

        # Tanpa skema = tanpa loop: satu panggilan chat biasa saja.
        if not skema:
            detail = client.chat_detail(pesan, model=model,
                                        max_tokens=max_tokens, temperature=temperature)
            return AgentResult(jawaban=(detail.konten or "").strip(), iterasi=1,
                               jejak=jejak, tools_dipakai=dipakai)

        for iterasi in range(1, self.max_iterations + 1):
            detail = client.chat_detail(
                pesan, model=model, max_tokens=max_tokens,
                temperature=temperature, tools=skema,
            )

            # --- jawaban final (tanpa tool_calls) -> selesai ---
            if not detail.tool_calls:
                logger.debug("Agent selesai di iterasi %d", iterasi)
                return AgentResult(jawaban=(detail.konten or "").strip(),
                                   iterasi=iterasi, jejak=jejak, tools_dipakai=dipakai)

            # --- teruskan pesan assistant + tool_calls (format OpenAI) ---
            pesan.append({
                "role": "assistant",
                "content": detail.konten or "",
                "tool_calls": [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("nama", ""),
                            "arguments": tc.get("argumen_mentah", "{}"),
                        },
                    }
                    for tc in detail.tool_calls
                ],
            })

            # --- eksekusi tiap tool call, kirim hasilnya balik ---
            for tc in detail.tool_calls:
                hasil_teks = self._eksekusi_satu(tc, iterasi, jejak, dipakai)
                pesan.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": hasil_teks,
                })

        # Batas tercapai tanpa jawaban final
        raise AgentMaxIterations(
            f"Agent berhenti: batas {self.max_iterations} iterasi tool-calling "
            f"tercapai tanpa jawaban final (tools: {', '.join(dipakai) or '—'})."
        )

    # =========================================================
    # Internal
    # =========================================================
    def _eksekusi_satu(
        self,
        tc: dict,
        iterasi: int,
        jejak: list[str],
        dipakai: list[str],
    ) -> str:
        """Eksekusi SATU tool call -> teks hasil untuk role "tool".

        Semua kegagalan (JSON rusak, tool tak dikenal, izin ditolak,
        error di dalam tool) diubah menjadi TEKS ERROR — bukan exception —
        supaya model berkesempatan mengoreksi diri di iterasi berikutnya.
        """
        nama = str(tc.get("nama", "")).strip()
        mentah = tc.get("argumen_mentah", "{}")

        # --- 1. parsing argumen JSON (dikirim balik bila rusak) ---
        try:
            argumen = json.loads(mentah) if str(mentah).strip() else {}
            if not isinstance(argumen, dict):
                raise ValueError("argumen bukan objek JSON (dict)")
        except (json.JSONDecodeError, ValueError) as exc:
            jejak.append(f"iter {iterasi}: {nama} -> argumen JSON tidak valid")
            logger.warning("Argumen tool %s tidak valid: %r (%s)", nama, mentah, exc)
            return (f"ERROR: argumen bukan JSON valid ({exc}). "
                    f"Kirim ulang tool call dengan argumen JSON yang benar.")

        # --- 2. konfirmasi utk tool yang menulis/mengubah data ---
        izin = False
        tool = self.registry.dapatkan(nama) if self.registry else None
        if tool is not None and tool.butuh_konfirmasi:
            if self.konfirmasi is not None:
                try:
                    izin = bool(self.konfirmasi(tool, argumen))
                except Exception as exc:        # callback user tidak boleh crash
                    logger.warning("Callback konfirmasi error: %s", exc)
                    izin = False
            # tanpa callback -> izin tetap False (ditolak, aman default)

        # --- 3. eksekusi via registry (tak pernah melempar exception) ---
        hasil = self.registry.eksekusi(nama, argumen, konfirmasi=izin) \
            if self.registry else ToolResult(False, "Registry tools tidak tersedia.")

        ringkas_arg = json.dumps(argumen, ensure_ascii=False)[:80]
        jejak.append(
            f"iter {iterasi}: {nama}({ringkas_arg}) -> {'OK' if hasil.ok else 'GAGAL'}"
        )
        dipakai.append(f"{nama}({ringkas_arg})")
        logger.info("Agent %s: %s(%s) -> %s",
                    "eksekusi" if hasil.ok else "gagal", nama, ringkas_arg,
                    hasil.keluaran[:80])
        return hasil.teks_model


# ------------------------------------------------------------
# Demo / test mandiri (klien palsu — tanpa Ollama & tanpa internet)
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("DEMO core/agent.py — loop tool-calling")
    print("=" * 60)

    try:
        from core.local_client import ReplyDetail
    except ImportError:
        from local_client import ReplyDetail  # type: ignore

    # --- klien palsu: panggilan ke-1 minta tool, ke-2 jawab final ---
    class KlienPalsu:
        def __init__(self, skenario: list[ReplyDetail]):
            self.skrip = list(skenario)
            self.pesan_terakhir: list[dict] = []

        def chat_detail(self, messages, model=None, max_tokens=None,
                        temperature=None, tools=None):
            self.pesan_terakhir = [dict(m) for m in messages]
            return self.skrip.pop(0)

    agen = Agent(ToolRegistry(), max_iterations=5)

    print("\n--- Skenario 1: model memanggil tool `hitung` lalu menjawab ---")
    klien = KlienPalsu([
        ReplyDetail(konten="", tool_calls=[{
            "id": "call_001", "nama": "hitung",
            "argumen_mentah": '{"ekspresi": "(2+3)*4"}',
        }]),
        ReplyDetail(konten="Hasil (2+3)*4 adalah 20."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "hitung (2+3)*4"}],
                     model="qwen3:1.7b")
    print(f"  Jawaban  : {hasil.jawaban}")
    print(f"  Iterasi  : {hasil.iterasi}")
    print(f"  Jejak    : {hasil.jejak}")
    print(f"  Tool     : {hasil.tools_dipakai}")
    # bukti hasil tool dikirim balik ke model:
    pesan_tool = [m for m in klien.pesan_terakhir if m.get("role") == "tool"]
    print(f"  Pesan tool ke model: {pesan_tool}")

    print("\n--- Skenario 2: argumen JSON rusak -> error dikirim balik ---")
    klien2 = KlienPalsu([
        ReplyDetail(konten="", tool_calls=[{
            "id": "call_002", "nama": "hitung", "argumen_mentah": "{ekspresi salah",
        }]),
        ReplyDetail(konten="Maaf, coba lagi: hasilnya 20."),
    ])
    hasil2 = agen.run(klien2, [{"role": "user", "content": "hitung (2+3)*4"}])
    print(f"  Jawaban  : {hasil2.jawaban}")
    print(f"  Jejak    : {hasil2.jejak}")

    print("\n--- Skenario 3: tulis_file tanpa izin -> ditolak ---")
    agen3 = Agent(ToolRegistry())            # tanpa callback konfirmasi
    klien3 = KlienPalsu([
        ReplyDetail(konten="", tool_calls=[{
            "id": "call_003", "nama": "tulis_file",
            "argumen_mentah": '{"jalur": "tes.txt", "isi": "hai"}',
        }]),
        ReplyDetail(konten="Baik, saya tidak menulis file karena izin ditolak."),
    ])
    hasil3 = agen3.run(klien3, [{"role": "user", "content": "simpan catatan"}])
    print(f"  Jawaban  : {hasil3.jawaban}")
    print(f"  Jejak    : {hasil3.jejak}")

    print("\n--- Skenario 4: batas iterasi -> AgentMaxIterations ---")
    loop = ReplyDetail(konten="", tool_calls=[{
        "id": "call_x", "nama": "get_time", "argumen_mentah": "{}",
    }])
    agen4 = Agent(ToolRegistry(), max_iterations=3)
    klien4 = KlienPalsu([loop, loop, loop, loop])
    try:
        agen4.run(klien4, [{"role": "user", "content": "jam?"}])
        print("  GAGAL: seharusnya raise AgentMaxIterations")
    except AgentMaxIterations as exc:
        print(f"  OK — {exc}")

    print("\nDemo selesai.")
