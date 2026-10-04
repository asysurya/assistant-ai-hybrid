# ============================================================
# AssistantAI — tests/test_agent.py
# ------------------------------------------------------------
# Test LOOP TOOL-CALLING — klien palsu + tool sungguhan,
# jadi TIDAK butuh Ollama maupun internet.
#
# Skenario:
#   - tanpa tool_calls -> jawaban final 1 iterasi
#   - tool call -> eksekusi nyata -> hasil role "tool" dikirim balik
#     -> jawaban final
#   - argumen JSON rusak -> pesan ERROR dikirim balik (model koreksi diri)
#   - batas iterasi -> AgentMaxIterations
#   - jejak & tools_dipakai terisi
#   - konfirmasi callback: izin=True -> tool jalan; izin=False -> ditolak
#   - skema tools diteruskan ke klien
#   - INTEGRASI: Assistant tier 2 memakai agent; tier 1 tetap chat polos
#
# Jalankan:
#   python -m pytest tests/test_agent.py -v
#   python tests/test_agent.py
# ============================================================

"""Test suite Agent: loop tool-calling, konfirmasi, integrasi Assistant."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config as cfg  # noqa: E402
from core.agent import Agent, AgentMaxIterations  # noqa: E402
from core.assistant import Assistant  # noqa: E402
from core.local_client import ReplyDetail  # noqa: E402
from core.tools import ToolRegistry  # noqa: E402
from core.tier_router import Tier, TierRouter  # noqa: E402


# ------------------------------------------------------------
# Fake klien dengan antarmuka chat_detail (meniru LocalClient)
# ------------------------------------------------------------
class KlienDetailPalsu:
    """Klien palsu: skenario ReplyDetail dieksekusi berurutan per panggilan."""

    def __init__(self, skenario: list[ReplyDetail]):
        self.skrip = list(skenario)
        self.dipanggil: list[dict] = []

    def chat_detail(self, messages, model=None, max_tokens=None,
                    temperature=None, tools=None):
        self.dipanggil.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
            "model": model,
        })
        return self.skrip.pop(0)

    def chat(self, messages, model=None, max_tokens=None, temperature=None) -> str:
        return self.chat_detail(messages, model=model,
                                max_tokens=max_tokens).konten


class KlienChatPalsu:
    """Fake LocalClient polos (tanpa chat_detail) — untuk test tier 1."""

    def __init__(self, jawaban: str = "Hai!"):
        self.jawaban = jawaban

    def chat(self, messages, model=None, max_tokens=None, temperature=None) -> str:
        return self.jawaban


class KlienCloudPalsu:
    def chat(self, messages, model=None, max_tokens=None, temperature=None) -> str:
        return "Jawaban cloud."


def tool_call(id: str, nama: str, argumen: str) -> ReplyDetail:
    """Pembangun singkat satu ReplyDetail berisi satu tool_calls."""
    return ReplyDetail(konten="", tool_calls=[
        {"id": id, "nama": nama, "argumen_mentah": argumen},
    ])


def buat_registry() -> tuple[ToolRegistry, Path]:
    tmp = tempfile.mkdtemp(prefix="assistantai-agent-")
    return ToolRegistry(workspace=Path(tmp)), Path(tmp)


# ------------------------------------------------------------
# Agent dasar
# ------------------------------------------------------------
def test_tanpa_tool_call_jawaban_langsung():
    reg, _ = buat_registry()
    agen = Agent(reg)
    klien = KlienDetailPalsu([ReplyDetail(konten="Halo, saya bisa membantu!")])
    hasil = agen.run(klien, [{"role": "user", "content": "hai"}], model="qwen3.5:2b")
    assert hasil.jawaban == "Halo, saya bisa membantu!"
    assert hasil.iterasi == 1
    assert hasil.tools_dipakai == []
    # skema tetap dikirim ke model (model diberi kesempatan pakai tool)
    assert klien.dipanggil[0]["tools"] is not None


def test_tool_call_dieksekusi_dan_hasil_dikirim_balik():
    reg, _ = buat_registry()
    agen = Agent(reg)
    klien = KlienDetailPalsu([
        tool_call("call_1", "hitung", '{"ekspresi": "(2+3)*4"}'),
        ReplyDetail(konten="Hasilnya 20."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "hitung (2+3)*4"}],
                     model="qwen3.5:2b")
    assert hasil.jawaban == "Hasilnya 20."
    assert hasil.iterasi == 2
    assert len(hasil.tools_dipakai) == 1 and "hitung" in hasil.tools_dipakai[0]
    # panggilan kedua harus membawa pesan assistant+tool_calls lalu pesan "tool"
    panggil2 = klien.dipanggil[1]
    peran = [m["role"] for m in panggil2["messages"]]
    assert peran == ["user", "assistant", "tool"]
    isi_tool = panggil2["messages"][-1]
    assert isi_tool["tool_call_id"] == "call_1"
    assert isi_tool["content"].startswith("OK:")
    assert "20" in isi_tool["content"]
    # pesan assistant menyimpan tool_calls format OpenAI
    asisten_msg = panggil2["messages"][1]
    assert asisten_msg["tool_calls"][0]["function"]["name"] == "hitung"
    assert asisten_msg["tool_calls"][0]["type"] == "function"


def test_argumen_json_rusak_dikirim_balik_sebagai_error():
    reg, _ = buat_registry()
    agen = Agent(reg)
    klien = KlienDetailPalsu([
        tool_call("call_2", "hitung", "{ekspresi tidak valid"),   # JSON rusak
        ReplyDetail(konten="Maaf, sekarang benar."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "hitung 2+2"}])
    assert hasil.jawaban == "Maaf, sekarang benar."
    isi_tool = klien.dipanggil[1]["messages"][-1]
    assert isi_tool["content"].startswith("ERROR:")
    assert "JSON" in isi_tool["content"]


def test_tool_error_terbungkus_bukan_exception():
    reg, _ = buat_registry()
    agen = Agent(reg)
    # 1/0 -> ZeroDivisionError DI DALAM tool -> dikirim sebagai teks ERROR
    klien = KlienDetailPalsu([
        tool_call("call_3", "hitung", '{"ekspresi": "1/0"}'),
        ReplyDetail(konten="Tidak bisa dibagi nol."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "x"}])
    assert hasil.jawaban == "Tidak bisa dibagi nol."
    assert klien.dipanggil[1]["messages"][-1]["content"].startswith("ERROR:")
    assert "ZeroDivisionError" in klien.dipanggil[1]["messages"][-1]["content"]


def test_batas_iterasi_raise_agent_max_iterations():
    reg, _ = buat_registry()
    agen = Agent(reg, max_iterations=3)
    loop = tool_call("call_x", "get_time", "{}")
    klien = KlienDetailPalsu([loop, loop, loop, loop])   # selalu minta tool
    try:
        agen.run(klien, [{"role": "user", "content": "jam?"}])
    except AgentMaxIterations as exc:
        assert "3" in str(exc)
    else:
        raise AssertionError("harusnya raise AgentMaxIterations")
    assert len(klien.dipanggil) == 3                     # berhenti tepat di batas


def test_jejak_terisi_rapi():
    reg, _ = buat_registry()
    agen = Agent(reg)
    klien = KlienDetailPalsu([
        tool_call("c1", "get_time", "{}"),
        ReplyDetail(konten="sekarang jam 10"),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "jam?"}])
    assert any("get_time" in j and "OK" in j for j in hasil.jejak)


# ------------------------------------------------------------
# Konfirmasi
# ------------------------------------------------------------
def test_konfirmasi_izin_true_mengeksekusi_file():
    reg, ws = buat_registry()
    agen = Agent(reg, konfirmasi=lambda tool, args: True)
    klien = KlienDetailPalsu([
        tool_call("c1", "tulis_file",
                  '{"jalur": "hasil.txt", "isi": "dicetak agent"}'),
        ReplyDetail(konten="File tersimpan."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "simpan"}])
    assert hasil.jawaban == "File tersimpan."
    assert (ws / "hasil.txt").read_text(encoding="utf-8") == "dicetak agent"
    assert klien.dipanggil[1]["messages"][-1]["content"].startswith("OK:")


def test_konfirmasi_izin_false_ditolak_dan_dikirim_balik():
    reg, _ = buat_registry()
    agen = Agent(reg, konfirmasi=lambda tool, args: False)   # user menolak
    klien = KlienDetailPalsu([
        tool_call("c1", "tulis_file", '{"jalur": "x.txt", "isi": "data"}'),
        ReplyDetail(konten="Baik, saya batal menulis file."),
    ])
    hasil = agen.run(klien, [{"role": "user", "content": "simpan"}])
    assert hasil.jawaban == "Baik, saya batal menulis file."
    isi = klien.dipanggil[1]["messages"][-1]["content"]
    assert isi.startswith("ERROR:") and "konfirmasi" in isi.lower()


def test_tanpa_callback_konfirmasi_ditolak_aman():
    reg, _ = buat_registry()
    agen = Agent(reg)                       # TANPA callback -> default aman
    klien = KlienDetailPalsu([
        tool_call("c1", "tulis_file", '{"jalur": "x.txt", "isi": "data"}'),
        ReplyDetail(konten="ok"),
    ])
    agen.run(klien, [{"role": "user", "content": "simpan"}])
    assert klien.dipanggil[1]["messages"][-1]["content"].startswith("ERROR:")


def test_callback_konfirmasi_yang_crash_dianggap_menolak():
    reg, _ = buat_registry()
    def callback_rusak(tool, args):
        raise RuntimeError("UI hilang")
    agen = Agent(reg, konfirmasi=callback_rusak)
    klien = KlienDetailPalsu([
        tool_call("c1", "tulis_file", '{"jalur": "x.txt", "isi": "data"}'),
        ReplyDetail(konten="ok"),
    ])
    agen.run(klien, [{"role": "user", "content": "simpan"}])   # tidak boleh raise
    assert klien.dipanggil[1]["messages"][-1]["content"].startswith("ERROR:")


# ------------------------------------------------------------
# Integrasi dengan Assistant (routing + agent)
# ------------------------------------------------------------
def test_assistant_tier2_memakai_agent_dengan_tools():
    reg, _ = buat_registry()
    klien_lokal = KlienDetailPalsu([
        tool_call("c1", "hitung", '{"ekspresi": "23*4"}'),
        ReplyDetail(konten="Hasil 23*4 adalah 92."),
        tool_call("c2", "hitung", '{"ekspresi": "7-3"}'),
        ReplyDetail(konten="Hasil 7-3 adalah 4."),
    ])
    asisten = Assistant(local=klien_lokal, cloud=KlienCloudPalsu(),
                        router=TierRouter(), tools=reg)
    reply = asisten.chat("hitung 23*4 berapa hasil")     # keyword hitung -> tier2
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_2
    assert reply.text == "Hasil 23*4 adalah 92."
    assert len(asisten.tools_terakhir) == 1
    assert "hitung" in asisten.tools_terakhir[0]
    assert "23*4" in asisten.tools_terakhir[0]           # jejak memuat argumen tool

    # chat() KEDUA: jejak harus di-reset lalu terisi ulang (tidak menumpuk)
    reply2 = asisten.chat("hitung 7-3 berapa hasil")
    assert reply2.ok is True
    assert reply2.text == "Hasil 7-3 adalah 4."
    assert len(asisten.tools_terakhir) == 1              # bukan 2 (ter-reset)
    assert "7-3" in asisten.tools_terakhir[0]


def test_assistant_tier1_tetap_chat_polos():
    reg, _ = buat_registry()
    klien_lokal = KlienChatPalsu("Hai juga!")
    asisten = Assistant(local=klien_lokal, cloud=KlienCloudPalsu(),
                        router=TierRouter(), tools=reg)
    reply = asisten.chat("halo, apa kabar?")             # sapaan -> tier1
    assert reply.ok is True
    assert reply.tier_used == Tier.TIER_1
    assert reply.text == "Hai juga!"
    assert asisten.tools_terakhir == []                  # tier 1 tak pakai tools


def test_assistant_tanpa_tools_perilaku_lama():
    asisten = Assistant(local=KlienChatPalsu("Halo!"),
                        cloud=KlienCloudPalsu(), router=TierRouter())
    assert asisten.agent is None                         # tanpa registry -> tanpa agent
    reply = asisten.chat("halo")
    assert reply.ok and reply.text == "Halo!"


def test_assistant_tools_dinonaktifkan_dari_config():
    class ConfigPalsu:
        def __init__(self, nilai):
            self.nilai = nilai

        def get(self, path, default=None):
            return self.nilai.get(path, default)

    reg, _ = buat_registry()
    config = ConfigPalsu({"tools.enabled": False})
    asisten = Assistant(config=config, local=KlienChatPalsu("Halo!"),
                        cloud=KlienCloudPalsu(), router=TierRouter(), tools=reg)
    reply = asisten.chat("halo")                         # tier1
    assert reply.ok and reply.text == "Halo!"
    # tier2 dengan tools dinonaktifkan -> lewat local.chat() biasa, bukan agent
    class LokalPaksaT2:
        def chat(self, messages, model=None, max_tokens=None, temperature=None):
            assert model == cfg.TIER2_MODEL
            return "jawab tanpa tools"

    asisten2 = Assistant(config=config, local=LokalPaksaT2(),
                         cloud=KlienCloudPalsu(), router=TierRouter(), tools=reg)
    reply2 = asisten2.chat("tolong hitung 5*5 ya")       # keyword hitung -> tier2
    assert reply2.ok and reply2.text == "jawab tanpa tools"


def test_tag_think_dibuang_dari_jawaban():
    asisten = Assistant(local=KlienChatPalsu(
        "<think>user menyapa, balas ramah</think>Halo! Ada yang bisa saya bantu?"
    ), cloud=KlienCloudPalsu(), router=TierRouter())
    reply = asisten.chat("halo")
    assert reply.text == "Halo! Ada yang bisa saya bantu?"


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
