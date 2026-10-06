# ============================================================
# AssistantAI — core/tools.py
# ------------------------------------------------------------
# TOOLKIT BAWAAN + REGISTRY untuk agent tool-calling (FASE 2).
#
# Tools yang tersedia:
#   get_time       tanggal & waktu sistem (Bahasa Indonesia)
#   hitung         kalkulator aman berbasis AST — BUKAN eval() mentah!
#   baca_file      baca file teks dari folder workspace
#   tulis_file     tulis file (BUTUH KONFIRMASI user dulu)
#   daftar_folder  lihat isi folder workspace
#   todo           kelola daftar tugas (tambah/daftar/selesai/hapus)
#
# KEAMANAN (penting!):
#   1. `hitung` hanya mengizinkan angka + operator aritmetika lewat
#      whitelist node AST. "__import__('os')" dsb. ditolak. Ada proteksi
#      DoS: pangkat besar (mis. 9**9**9) ditolak sebelum dihitung.
#   2. Semua tool file TERKURUNG di folder WORKSPACE
#      (default: %APPDATA%/AssistantAI/workspace). Path dengan ".."
#      atau path absolut di luar workspace ditolak.
#   3. Tool yang MENULIS/mengubah (tulis_file) ber-flag
#      butuh_konfirmasi=True -> agent harus minta izin user dulu.
#   4. Tool TIDAK PERNAH dijalankan hanya dari teks model — hanya
#      lewat ToolRegistry.eksekusi() yang membungkus semua error.
#
# Skema tools mengikuti format "tools" OpenAI (function calling),
# sehingga kompatibel dengan endpoint Ollama /v1/chat/completions.
# ============================================================

"""Registry tools bawaan + skema OpenAI untuk agent tool-calling.

Cara test cepat:
    python core/tools.py
"""

from __future__ import annotations

import ast
import json
import logging
import operator
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung: python core/tools.py
    import config as cfg  # type: ignore

logger = logging.getLogger("assistant.tools")


# ------------------------------------------------------------
# Tipe dasar
# ------------------------------------------------------------
@dataclass
class Tool:
    """Definisi satu tool (mirip "function" pada skema OpenAI tools)."""
    nama: str
    deskripsi: str                      # dibaca MODEL utk memutuskan kapan dipakai
    parameter: dict                     # JSON Schema {"type": "object", "properties": ...}
    fungsi: Callable[..., str]          # menerima dict argumen -> str hasil
    butuh_konfirmasi: bool = False      # True = tindakan menulis/mengubah data


@dataclass
class ToolResult:
    """Hasil eksekusi tool — error TIDAK pernah dilempar keluar registry."""
    ok: bool
    keluaran: str

    @property
    def teks_model(self) -> str:
        """Bentuk yang dikirim balik ke model sebagai hasil tool."""
        return f"OK: {self.keluaran}" if self.ok else f"ERROR: {self.keluaran}"


# ------------------------------------------------------------
# Kalkulator aman (whitelist node AST)
# ------------------------------------------------------------
# Pemetaan node operator -> fungsi stdlib (tanpa eval/exec sama sekali)
_OPERATOR_BINER: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Batas proteksi DoS komputasi
_BATAS_PANGKAT = 10_000        # |eksponen| maksimum
_BATAS_DIGIT = 1_000           # jumlah digit angka dasar maksimum utk pangkat


def _eval_aman(node: ast.AST) -> float:
    """Evaluasi node AST dengan whitelist ketat.

    Dibolehkan: angka (int/float), + - * / // % **, tanda kurung,
    unary +/- (mis. -5). SELAIN ITU -> ValueError.
    """
    # Pembungkus hasil ast.parse(mode="eval") -> evaluasi isinya
    if isinstance(node, ast.Expression):
        return _eval_aman(node.body)

    # angka literal (bool sengaja ditolak: True seharusnya bukan ekspresi matematika)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATOR_BINER:
        kiri = _eval_aman(node.left)
        kanan = _eval_aman(node.right)
        if isinstance(node.op, ast.Pow):
            # Proteksi DoS: 9**9**9 dihitung kanan-dulu -> eksponen raksasa.
            if abs(kanan) > _BATAS_PANGKAT or len(str(abs(kiri))) > _BATAS_DIGIT:
                raise ValueError("pangkat/angka terlalu besar (proteksi komputasi)")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and kanan == 0:
            raise ZeroDivisionError("pembagian dengan nol")
        return _OPERATOR_BINER[type(node.op)](kiri, kanan)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        nilai = _eval_aman(node.operand)
        return -nilai if isinstance(node.op, ast.USub) else nilai

    raise ValueError(f"ekspresi tidak diizinkan: {type(node).__name__}")


def hitung_ekspresi(ekspresi: str) -> str:
    """Hitung ekspresi aritmetika dengan aman -> str hasil (angka)."""
    if not ekspresi or not ekspresi.strip():
        raise ValueError("Ekspresi kosong.")
    try:
        pohon = ast.parse(ekspresi.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Ekspresi tidak valid secara sintaks: {exc.msg}") from exc
    hasil = _eval_aman(pohon)
    # tampilkan integer tanpa .0 agar ramah dibaca
    if isinstance(hasil, float) and hasil.is_integer():
        return str(int(hasil))
    return str(hasil)


# ------------------------------------------------------------
# Nama hari Bahasa Indonesia (locale sistem tak selalu tersedia)
# ------------------------------------------------------------
NAMA_HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")


# ------------------------------------------------------------
# Fungsi tools (menerima dict argumen dari model)
# ------------------------------------------------------------
def tool_waktu(args: dict) -> str:
    """Tool get_time: tanggal & waktu lokal sekarang."""
    now = datetime.now()
    return f"{NAMA_HARI[now.weekday()]}, {now.strftime('%d-%m-%Y %H:%M:%S')}"


def tool_hitung(args: dict) -> str:
    """Tool hitung: kalkulator aman. Argumen: {ekspresi: str}."""
    ekspresi = str(args.get("ekspresi", ""))
    return f"{ekspresi.strip()} = {hitung_ekspresi(ekspresi)}"


def _path_aman(jalur: str, workspace: Path) -> Path:
    """Terjemahkan jalur user -> path ABSOLUT di dalam workspace.

    Aturan:
      - jalur relatif -> relatif terhadap workspace
      - path absolut / berisi '..' yang meloloskan keluar workspace -> ditolak
      - karakter khas sistem lain ('\\' pemisah Windows, ':' penanda drive,
        '~' folder rumah) ditolak juga — konsisten & aman lintas platform
    """
    if not jalur or not str(jalur).strip():
        raise ValueError("Jalur file/folder kosong.")
    mentah = str(jalur)
    if "\\" in mentah or ":" in mentah or mentah.startswith("~") \
            or Path(mentah).is_absolute():
        raise ValueError(
            f"Jalur tidak diizinkan (pakai jalur relatif sederhana): {mentah}"
        )
    kandidat = (workspace / mentah).resolve()
    ws = workspace.resolve()
    if kandidat == ws or ws in kandidat.parents:
        return kandidat
    raise ValueError(
        f"Akses di luar workspace tidak diizinkan: {jalur} "
        f"(workspace: {ws})"
    )


def tool_baca_file(args: dict, workspace: Optional[Path] = None) -> str:
    """Tool baca_file: baca file teks dari workspace (terpotong bila raksasa)."""
    ws = Path(workspace) if workspace else cfg.TOOLS_WORKSPACE
    p = _path_aman(str(args.get("jalur", "")), ws)
    if not p.exists():
        raise FileNotFoundError(f"File tidak ada: {args.get('jalur')}")
    if p.is_dir():
        raise ValueError(f"'{args.get('jalur')}' adalah folder — pakai daftar_folder.")
    maks = int(args.get("maks_karakter") or cfg.TOOL_MAX_FILE_BYTES)
    data = p.read_bytes()[:maks]
    teks = data.decode("utf-8", errors="replace")
    if p.stat().st_size > maks:
        teks += f"\n... [DIPOTONG — file {p.stat().st_size} byte, dibaca {maks} byte pertama]"
    return teks


def tool_tulis_file(args: dict, workspace: Optional[Path] = None) -> str:
    """Tool tulis_file: tulis/replace file di workspace (butuh konfirmasi)."""
    ws = Path(workspace) if workspace else cfg.TOOLS_WORKSPACE
    p = _path_aman(str(args.get("jalur", "")), ws)
    isi = args.get("isi", "")
    if not isinstance(isi, str):
        raise ValueError("Argumen 'isi' harus berupa teks.")
    p.parent.mkdir(parents=True, exist_ok=True)   # subfolder dibuat otomatis
    p.write_text(isi, encoding="utf-8")
    logger.info("Tool tulis_file: %s (%d karakter)", p, len(isi))
    return f"Tersimpan: {p.name} ({len(isi)} karakter) di {p.parent}"


def tool_daftar_folder(args: dict, workspace: Optional[Path] = None) -> str:
    """Tool daftar_folder: isi folder workspace (maks 200 entri)."""
    ws = Path(workspace) if workspace else cfg.TOOLS_WORKSPACE
    rel = str(args.get("jalur", "") or ".")
    target = _path_aman(rel, ws)
    if not target.exists():
        raise FileNotFoundError(f"Folder tidak ada: {rel}")
    if not target.is_dir():
        raise ValueError(f"'{rel}' adalah file — pakai baca_file.")
    entri = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    if not entri:
        return f"(folder kosong: {rel})"
    baris: list[str] = []
    for e in entri[:200]:
        if e.is_dir():
            baris.append(f"{e.name}/")
        else:
            baris.append(f"{e.name} ({e.stat().st_size} B)")
    if len(entri) > 200:
        baris.append(f"... dan {len(entri) - 200} entri lainnya")
    return "\n".join(baris)


# ------------------------------------------------------------
# Tool todo — state disimpan sebagai JSON di dalam workspace
# ------------------------------------------------------------
def _berkas_todo(workspace: Path) -> Path:
    return workspace / "todos.json"


def _muat_todo(workspace: Path) -> list[dict]:
    berkas = _berkas_todo(workspace)
    if not berkas.exists():
        return []
    try:
        data = json.loads(berkas.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        logger.warning("todos.json rusak — dianggap kosong")
        return []


def _simpan_todo(workspace: Path, todos: list[dict]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)   # folder workspace dibuat otomatis
    _berkas_todo(workspace).write_text(
        json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _format_todo(todos: list[dict]) -> str:
    if not todos:
        return "(daftar todo kosong)"
    return "\n".join(
        f"{i}. [{'x' if t.get('selesai') else ' '}] {t['teks']}"
        for i, t in enumerate(todos, start=1)
    )


def tool_todo(args: dict, workspace: Optional[Path] = None) -> str:
    """Tool todo: aksi = daftar | tambah | selesai | hapus.

    Argumen: {aksi: str, teks?: str, indeks?: int}  (indeks mulai dari 1)
    """
    ws = Path(workspace) if workspace else cfg.TOOLS_WORKSPACE
    aksi = str(args.get("aksi", "daftar")).strip().lower()
    todos = _muat_todo(ws)

    if aksi == "daftar":
        return _format_todo(todos)

    if aksi == "tambah":
        teks = str(args.get("teks", "")).strip()
        if not teks:
            raise ValueError("Argumen 'teks' wajib untuk aksi tambah.")
        todos.append({"teks": teks, "selesai": False,
                      "dibuat": time.strftime("%Y-%m-%d %H:%M")})
        _simpan_todo(ws, todos)
        return f"Todo ditambahkan. Total: {len(todos)}."

    if aksi in ("selesai", "hapus"):
        indeks = int(args.get("indeks", 0))
        if indeks < 1 or indeks > len(todos):
            raise ValueError(f"Indeks {indeks} di luar rentang 1..{len(todos)}.")
        item = todos.pop(indeks - 1) if aksi == "hapus" else todos[indeks - 1]
        if aksi == "selesai":
            item["selesai"] = True
        _simpan_todo(ws, todos)
        verb = "ditandai selesai" if aksi == "selesai" else "dihapus"
        return f"Todo '{item['teks'][:40]}' {verb}.\n{_format_todo(todos)}"

    raise ValueError(f"Aksi todo '{aksi}' tidak dikenal (daftar/tambah/selesai/hapus).")


# ------------------------------------------------------------
# Skema parameter (JSON Schema) per tool
# ------------------------------------------------------------
def _skema_hitung() -> dict:
    return {
        "type": "object",
        "properties": {
            "ekspresi": {
                "type": "string",
                "description": "Ekspresi aritmetika, mis. (2+3)*4 atau 2**10",
            },
        },
        "required": ["ekspresi"],
    }


def _skema_file(baca: bool) -> dict:
    if baca:
        return {
            "type": "object",
            "properties": {
                "jalur": {"type": "string", "description": "Jalur file relatif workspace"},
                "maks_karakter": {"type": "integer",
                                  "description": "Opsional: batas karakter dibaca"},
            },
            "required": ["jalur"],
        }
    return {
        "type": "object",
        "properties": {
            "jalur": {"type": "string", "description": "Jalur file relatif workspace"},
            "isi": {"type": "string", "description": "Konten lengkap yang akan ditulis"},
        },
        "required": ["jalur", "isi"],
    }


def _skema_todo() -> dict:
    return {
        "type": "object",
        "properties": {
            "aksi": {"type": "string", "enum": ["daftar", "tambah", "selesai", "hapus"]},
            "teks": {"type": "string", "description": "Isi todo (untuk aksi tambah)"},
            "indeks": {"type": "integer",
                       "description": "Nomor todo mulai 1 (untuk selesai/hapus)"},
        },
        "required": ["aksi"],
    }


# ------------------------------------------------------------
# Daftar tools default
# ------------------------------------------------------------
def _tools_default(workspace: Path) -> list[Tool]:
    """Bangun 6 tools bawaan, terikat ke folder workspace tertentu.

    functools.partial dipakai supaya fungsi murni di atas tetap bisa
    diuji dengan workspace sementara (tmp) tanpa menyentuh config.
    """
    import functools

    return [
        Tool(
            nama="get_time",
            deskripsi="Ambil tanggal dan waktu sistem saat ini. "
                      "Gunakan bila user menanyakan waktu/tanggal sekarang.",
            parameter={"type": "object", "properties": {}},
            fungsi=tool_waktu,
        ),
        Tool(
            nama="hitung",
            deskripsi="Kalkulator aman untuk perhitungan angka: + - * / // % ** "
                      "dan tanda kurung. Selalu gunakan tool ini (bukan menghitung "
                      "sendiri) agar hasil akurat.",
            parameter=_skema_hitung(),
            fungsi=tool_hitung,
        ),
        Tool(
            nama="baca_file",
            deskripsi="Baca isi file teks di folder workspace user "
                      "(kode, catatan, config). Tidak bisa membaca di luar workspace.",
            parameter=_skema_file(baca=True),
            fungsi=functools.partial(tool_baca_file, workspace=workspace),
        ),
        Tool(
            nama="tulis_file",
            deskripsi="Tulis (atau timpa) sebuah file di folder workspace user. "
                      "Gunakan hanya setelah user sepakat isinya.",
            parameter=_skema_file(baca=False),
            fungsi=functools.partial(tool_tulis_file, workspace=workspace),
            butuh_konfirmasi=True,          # menulis data -> wajib izin user
        ),
        Tool(
            nama="daftar_folder",
            deskripsi="Lihat daftar file & folder di dalam workspace user.",
            parameter={
                "type": "object",
                "properties": {
                    "jalur": {"type": "string",
                              "description": "Subfolder relatif (kosongkan = root)"},
                },
            },
            fungsi=functools.partial(tool_daftar_folder, workspace=workspace),
        ),
        Tool(
            nama="todo",
            deskripsi="Kelola daftar tugas (todo list) user: "
                      "aksi 'daftar', 'tambah' (butuh teks), "
                      "'selesai'/'hapus' (butuh indeks mulai dari 1).",
            parameter=_skema_todo(),
            fungsi=functools.partial(tool_todo, workspace=workspace),
        ),
    ]


# ------------------------------------------------------------
# ToolRegistry
# ------------------------------------------------------------
class ToolRegistry:
    """Kumpulan tool yang bisa dipakai agent + eksekusi yang aman.

    Contoh:
        reg = ToolRegistry()
        hasil = reg.eksekusi("hitung", {"ekspresi": "(2+3)*4"})
        print(hasil.ok, hasil.keluaran)      # True, '(2+3)*4 = 20'

        skema = reg.skema_openai()           # utk parameter `tools` API
    """

    def __init__(
        self,
        tools: Optional[Iterable[Tool]] = None,
        workspace: Optional[Path] = None,
    ):
        """
        Args:
            tools: daftar Tool. None -> pakai 6 tools bawaan.
            workspace: folder kerja utk tools file/todo (default dari cfg).
        """
        self.workspace = Path(workspace) if workspace else cfg.TOOLS_WORKSPACE
        self._tools: dict[str, Tool] = {}
        for t in (tools if tools is not None else _tools_default(self.workspace)):
            self.daftarkan(t)

    def daftarkan(self, tool: Tool) -> None:
        """Daftarkan tool baru; nama duplikat ditolak agar tidak tertimpa diam-diam."""
        if tool.nama in self._tools:
            raise ValueError(f"Tool '{tool.nama}' sudah terdaftar.")
        self._tools[tool.nama] = tool

    def dapatkan(self, nama: str) -> Optional[Tool]:
        """Ambil tool berdasar nama (None bila tidak ada)."""
        return self._tools.get((nama or "").strip())

    def nama_tools(self) -> list[str]:
        """Daftar nama semua tool terdaftar (urut abjad)."""
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    # ---------- eksekusi ----------
    def eksekusi(self, nama: str, argumen: Optional[dict] = None,
                 konfirmasi: bool = False) -> ToolResult:
        """Jalankan tool dengan pengaman penuh — TIDAK pernah melempar exception.

        Aturan:
          - tool tak dikenal      -> ToolResult(ok=False) + daftar tool tersedia
          - butuh_konfirmasi=True dan konfirmasi=False -> DITOLAK dengan pesan
            yang jelas (model akan melaporkannya ke user, bukan diam-diam)
          - error apa pun di dalam fungsi -> ToolResult(ok=False, pesan error)
        """
        tool = self.dapatkan(nama)
        if tool is None:
            return ToolResult(
                False,
                f"Tool '{nama}' tidak dikenal. Tool tersedia: "
                f"{', '.join(self.nama_tools())}.",
            )
        if tool.butuh_konfirmasi and not konfirmasi:
            return ToolResult(
                False,
                f"Tool '{tool.nama}' menulis/mengubah data sehingga butuh "
                f"konfirmasi user terlebih dulu. Tanyakan pada user, lalu "
                f"ulangi hanya setelah izin diberikan.",
            )
        try:
            keluaran = str(tool.fungsi(argumen or {}))
            return ToolResult(True, keluaran)
        except Exception as exc:            # sengaja luas: error = hasil utk model
            logger.warning("Tool %s gagal: %s: %s", nama, type(exc).__name__, exc)
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    # ---------- skema OpenAI ----------
    def skema_openai(self) -> list[dict]:
        """Bentuk parameter `tools` utk API OpenAI-compatible (Ollama /v1)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.nama,
                    "description": t.deskripsi,
                    "parameters": t.parameter,
                },
            }
            for t in self._tools.values()
        ]

    # ---------- tampilan ----------
    def ringkas(self) -> str:
        """Tabel teks daftar tools (untuk CLI /alat dan debugging)."""
        header = f"{'NAMA':<14} {'KONFIRMASI':<11} DESKRIPSI"
        baris = [header, "-" * len(header)]
        for nama in self.nama_tools():
            t = self._tools[nama]
            flag = "YA" if t.butuh_konfirmasi else "-"
            baris.append(f"{t.nama:<14} {flag:<11} {t.deskripsi[:60]}")
        return "\n".join(baris)


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("DEMO core/tools.py — registry + tools bawaan")
    print("=" * 60)

    reg = ToolRegistry()
    print(f"\nTools terdaftar: {', '.join(reg.nama_tools())}")

    print("\n--- Uji kalkulator aman ---")
    for eks in ["(2+3)*4", "2**10", "10/4", "-5 + 3"]:
        r = reg.eksekusi("hitung", {"ekspresi": eks})
        print(f"  {eks:<10} -> ok={r.ok}  {r.keluaran}")
    print("\n--- Uji penolakan ekspresi berbahaya ---")
    for eks in ["__import__('os').system('dir')", "open('/etc/passwd')", "9**9**9", "1/0"]:
        r = reg.eksekusi("hitung", {"ekspresi": eks})
        print(f"  {eks:<34} -> ok={r.ok}  {r.keluaran[:60]}")

    print("\n--- Uji tool waktu & todo ---")
    print(f"  waktu : {reg.eksekusi('get_time', {}).keluaran}")
    reg.eksekusi("todo", {"aksi": "tambah", "teks": "Belajar FASE 2"})
    reg.eksekusi("todo", {"aksi": "tambah", "teks": "Rakit PC"})
    print(f"  todo  :\n{reg.eksekusi('todo', {'aksi': 'daftar'}).keluaran}")

    print("\n--- Uji konfirmasi tulis_file (di folder sementara) ---")
    with tempfile.TemporaryDirectory() as tmp:
        reg_tmp = ToolRegistry(workspace=Path(tmp))
        ditolak = reg_tmp.eksekusi("tulis_file", {"jalur": "catatan.txt", "isi": "halo"})
        print(f"  tanpa izin : ok={ditolak.ok} -> {ditolak.keluaran[:50]}")
        dgn_izin = reg_tmp.eksekusi("tulis_file", {"jalur": "catatan.txt", "isi": "halo"},
                                    konfirmasi=True)
        print(f"  dengan izin: ok={dgn_izin.ok} -> {dgn_izin.keluaran[:50]}")
        baca = reg_tmp.eksekusi("baca_file", {"jalur": "catatan.txt"})
        print(f"  baca ulang : {baca.keluaran!r}")
        luar = reg_tmp.eksekusi("baca_file", {"jalur": "../rahasia.txt"})
        print(f"  di luar ws : ok={luar.ok} -> {luar.keluaran[:60]}")

    print("\nDemo selesai.")
