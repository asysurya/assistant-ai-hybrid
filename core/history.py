# ============================================================
# AssistantAI — core/history.py
# ------------------------------------------------------------
# RIWAYAT CHAT berbasis SQLite (FASE 2).
#
# Fitur:
#   - SESI percakapan: tiap sesi punya judul otomatis dari pesan
#     user pertama (dapat diganti kapan saja)
#   - PESAN: user / assistant / system / tool, lengkap dengan
#     metadata tier + model + durasi untuk audit routing 3-tier
#   - CARI: pencarian keyword dengan LIKE + ESCAPE (aman dari
#     karakter % dan _ milik user)
#   - EKSPOR: satu sesi -> Markdown / JSON / TXT
#   - STATISTIK: jumlah sesi, pesan, distribusi per tier
#
# Keputusan teknis:
#   - SQLite mode WAL (Write-Ahead Logging): aman dibaca bersamaan
#     oleh UI thread + worker, tahan crash tanpa korup
#   - check_same_thread=False + RLock: satu koneksi dipakai lintas
#     thread dengan penguncian manual (lebih hemat daripada banyak
#     koneksi di laptop RAM 8GB)
#   - foreign_keys=ON: hapus sesi -> pesan ikut terhapus (CASCADE)
#   - Timestamp lokal format "YYYY-MM-DD HH:MM:SS" (mudah dibaca
#     manusia & diurutkan alfabetis = kronologis)
# ============================================================

"""Riwayat chat SQLite: sesi, pesan, pencarian, ekspor, statistik.

Cara test cepat:
    python core/history.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

try:                                    # dipakai sebagai paket proyek
    from core import config as cfg
except ImportError:                     # dijalankan langsung: python core/history.py
    import config as cfg  # type: ignore

logger = logging.getLogger("assistant.history")

# Judul sementara sesi sebelum pesan user pertama masuk
JUDUL_DEFAULT = "Sesi baru"

# Peran pesan yang diizinkan (format OpenAI + peran "tool" internal)
PERAN_VALID = ("user", "assistant", "system", "tool")

# Label ramah untuk ekspor
LABEL_PERAN = {"user": "Anda", "assistant": "AI", "system": "Sistem", "tool": "Alat"}

# ------------------------------------------------------------
# Skema database
# ------------------------------------------------------------
_SKEMA_SQL = """
CREATE TABLE IF NOT EXISTS sesi (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    judul            TEXT NOT NULL DEFAULT 'Sesi baru',
    dibuat_pada      TEXT NOT NULL,
    diperbarui_pada  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pesan (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sesi_id    INTEGER NOT NULL REFERENCES sesi(id) ON DELETE CASCADE,
    peran      TEXT NOT NULL,             -- user | assistant | system | tool
    isi        TEXT NOT NULL,
    tier       TEXT,                      -- tier yang menjawab (pesan assistant)
    model      TEXT,                      -- nama model (opsional, audit)
    durasi_ms  REAL,                      -- lama pemrosesan (pesan assistant)
    waktu      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pesan_sesi  ON pesan(sesi_id);
CREATE INDEX IF NOT EXISTS idx_pesan_waktu ON pesan(waktu);
"""


def _waktu_sekarang() -> str:
    """Timestamp lokal 'YYYY-MM-DD HH:MM:SS' (urut alfabetis = kronologis)."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _escape_like(teks: str) -> str:
    """Loloskan karakter khusus LIKE (% _ \\) agar dicari apa adanya.

    Tanpa ini, keyword user berisi '%' akan cocok dengan SEMUA baris.
    Dipasangkan dengan klausa SQL `ESCAPE '\\'`.
    """
    return (teks or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------
# ChatHistory
# ------------------------------------------------------------
class ChatHistory:
    """Manajer riwayat chat SQLite (satu koneksi + lock, mode WAL).

    Contoh:
        riwayat = ChatHistory()
        sesi_id = riwayat.buat_sesi()
        riwayat.tambah_pesan(sesi_id, "user", "halo")
        riwayat.tambah_pesan(sesi_id, "assistant", "Hai!", tier="tier1")
        for p in riwayat.pesan_sesi(sesi_id):
            print(p["peran"], p["isi"])

    Dipakai sebagai context manager bila memungkinkan:
        with ChatHistory() as riwayat: ...
    """

    def __init__(self, db_file: Optional[Path] = None):
        """
        Args:
            db_file: lokasi berkas .db. Default: cfg.HISTORY_DB
                (%APPDATA%/AssistantAI/history.db). Parameter ini memudahkan
                test (pakai berkas sementara di folder tmp).
        """
        self.db_file = Path(db_file) if db_file else cfg.HISTORY_DB
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._pasang_pragma()
        self._conn.executescript(_SKEMA_SQL)
        self._conn.commit()
        logger.debug("Riwayat chat siap: %s", self.db_file)

    def _pasang_pragma(self) -> None:
        """PRAGMA per koneksi: WAL + foreign key + latency tulis rendah.

        synchronous=NORMAL pada WAL aman untuk crash aplikasi (bukan crash
        listrik) dan jauh lebih cepat di SSD laptop.
        """
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # =========================================================
    # Sesi
    # =========================================================
    def buat_sesi(self, judul: Optional[str] = None) -> int:
        """Buat sesi baru -> id sesi (judul otomatis menyusul dari pesan)."""
        now = _waktu_sekarang()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sesi (judul, dibuat_pada, diperbarui_pada) VALUES (?,?,?)",
                ((judul or JUDUL_DEFAULT).strip() or JUDUL_DEFAULT, now, now),
            )
            self._conn.commit()
            logger.debug("Sesi baru #%s dibuat", cur.lastrowid)
            return int(cur.lastrowid)

    def daftar_sesi(self, limit: int = 20) -> list[dict]:
        """Sesi terbaru (urut diperbarui_pada) + jumlah pesan + cuplikan akhir."""
        limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM pesan p WHERE p.sesi_id = s.id) AS jumlah_pesan,
                       (SELECT p.isi FROM pesan p WHERE p.sesi_id = s.id
                         ORDER BY p.id DESC LIMIT 1)                        AS pesan_terakhir
                FROM sesi s
                ORDER BY s.diperbarui_pada DESC, s.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_sesi(self, sesi_id: int) -> Optional[dict]:
        """Detail satu sesi (None bila tidak ada)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sesi WHERE id = ?", (int(sesi_id),)
            ).fetchone()
            return dict(row) if row else None

    def ganti_judul(self, sesi_id: int, judul: str) -> bool:
        """Ganti judul sesi secara manual. False bila sesi tidak ada."""
        judul = (judul or "").strip()
        if not judul:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sesi SET judul = ?, diperbarui_pada = ? WHERE id = ?",
                (judul[:120], _waktu_sekarang(), int(sesi_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def hapus_sesi(self, sesi_id: int) -> bool:
        """Hapus sesi + seluruh pesannya (ON DELETE CASCADE). False bila tak ada."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sesi WHERE id = ?", (int(sesi_id),)
            )
            self._conn.commit()
            if cur.rowcount:
                logger.info("Sesi #%s dihapus (beserta pesan-pesannya)", sesi_id)
            return cur.rowcount > 0

    # =========================================================
    # Pesan
    # =========================================================
    def tambah_pesan(
        self,
        sesi_id: int,
        peran: str,
        isi: str,
        tier: Optional[str] = None,
        model: Optional[str] = None,
        durasi_ms: Optional[float] = None,
    ) -> int:
        """Simpan satu pesan -> id pesan.

        Efek samping yang diinginkan:
          - pesan user PERTAMA pada sesi tanpa judul -> judul sesi otomatis
          - diperbarui_pada sesi di-refresh (urutan 'daftar_sesi' akurat)

        Raises:
            ValueError: peran tidak valid / sesi tidak ditemukan / isi kosong.
        """
        peran = (peran or "").strip().lower()
        if peran not in PERAN_VALID:
            raise ValueError(f"Peran '{peran}' tidak valid. Pilihan: {', '.join(PERAN_VALID)}")
        isi = (isi or "").strip()
        if not isi:
            raise ValueError("Isi pesan tidak boleh kosong.")

        now = _waktu_sekarang()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO pesan (sesi_id, peran, isi, tier, model, durasi_ms, waktu)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (int(sesi_id), peran, isi, tier, model, durasi_ms, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Sesi #{sesi_id} tidak ditemukan (FK gagal): {exc}"
                ) from exc

            if peran == "user":
                self._judul_otomatis(sesi_id, isi)
            self._conn.execute(
                "UPDATE sesi SET diperbarui_pada = ? WHERE id = ?", (now, int(sesi_id))
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _judul_otomatis(self, sesi_id: int, isi_user: str) -> None:
        """Set judul sesi dari pesan user pertama (hanya bila masih default)."""
        row = self._conn.execute(
            "SELECT judul FROM sesi WHERE id = ?", (int(sesi_id),)
        ).fetchone()
        if row is None or row["judul"] != JUDUL_DEFAULT:
            return                          # judul sudah ditentukan user / pesan lain
        bersih = " ".join(isi_user.split())     # rapikan newline & spasi ganda
        judul = bersih[:60] + ("..." if len(bersih) > 60 else "")
        self._conn.execute(
            "UPDATE sesi SET judul = ? WHERE id = ?", (judul or JUDUL_DEFAULT, int(sesi_id))
        )

    def pesan_sesi(self, sesi_id: int, limit: Optional[int] = None) -> list[dict]:
        """Semua pesan sesi (urut lama -> baru); `limit` = N pesan TERAKHIR."""
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    "SELECT * FROM pesan WHERE sesi_id = ? ORDER BY id ASC",
                    (int(sesi_id),),
                ).fetchall()
                return [dict(r) for r in rows]
            rows = self._conn.execute(
                "SELECT * FROM pesan WHERE sesi_id = ? ORDER BY id DESC LIMIT ?",
                (int(sesi_id), max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def riwayat_chat(self, sesi_id: int, maks: Optional[int] = None) -> list[dict]:
        """N pesan terakhir format OpenAI [{"role", "content"}] untuk model.

        Peran "tool" dan "system" SENGAJA tidak disertakan: pesan tool
        tanpa pasangan tool_calls aslinya akan ditolak API OpenAI-compatible.
        """
        maks = maks or cfg.HISTORY_MAX_CONTEXT
        dengan_peran = self.pesan_sesi(sesi_id, limit=maks)
        return [
            {"role": p["peran"], "content": p["isi"]}
            for p in dengan_peran
            if p["peran"] in ("user", "assistant")
        ]

    def cari(self, kata_kunci: str, limit: int = 20) -> list[dict]:
        """Cari pesan berisi keyword (case-insensitive, LIKE %kw% aman).

        Returns:
            Pesan terbaru yang cocok, masing-masing diberi field `judul_sesi`
            supaya CLI/UI bisa menampilkan konteksnya.
        """
        kata_kunci = (kata_kunci or "").strip()
        if not kata_kunci:
            return []
        limit = max(1, int(limit))
        pola = f"%{_escape_like(kata_kunci.lower())}%"
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.*, s.judul AS judul_sesi
                FROM pesan p JOIN sesi s ON s.id = p.sesi_id
                WHERE lower(p.isi) LIKE ? ESCAPE '\\'
                ORDER BY p.id DESC
                LIMIT ?
                """,
                (pola, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # =========================================================
    # Ekspor
    # =========================================================
    def export_sesi(self, sesi_id: int, format: str = "md") -> str:
        """Ekspor satu sesi ke teks: 'md' (Markdown), 'json', atau 'txt'.

        Raises:
            ValueError: sesi tidak ada / format tidak dikenal.
        """
        sesi = self.get_sesi(sesi_id)
        if sesi is None:
            raise ValueError(f"Sesi #{sesi_id} tidak ditemukan.")
        pesan = self.pesan_sesi(sesi_id)
        fmt = (format or "md").strip().lower()

        if fmt == "json":
            return json.dumps({"sesi": sesi, "pesan": pesan},
                              indent=2, ensure_ascii=False)

        if fmt == "txt":
            baris = [
                f"Sesi #{sesi['id']}: {sesi['judul']}",
                f"Dibuat: {sesi['dibuat_pada']} | Pesan: {len(pesan)}",
                "-" * 50,
            ]
            for p in pesan:
                baris.append(f"{LABEL_PERAN.get(p['peran'], p['peran'])} > {p['isi']}")
            return "\n".join(baris)

        if fmt == "md":
            baris = [
                f"# Sesi #{sesi['id']}: {sesi['judul']}",
                "",
                f"- Dibuat: {sesi['dibuat_pada']}",
                f"- Diperbarui: {sesi['diperbarui_pada']}",
                f"- Jumlah pesan: {len(pesan)}",
                "",
            ]
            for p in pesan:
                label = LABEL_PERAN.get(p["peran"], p["peran"])
                meta = []
                if p.get("tier"):
                    meta.append(f"tier: {p['tier']}")
                if p.get("durasi_ms"):
                    meta.append(f"{p['durasi_ms']:.0f} ms")
                suffix = f" *({', '.join(meta)})*" if meta else ""
                baris += [f"**{label}**{suffix}:", "", p["isi"], ""]
            return "\n".join(baris)

        raise ValueError(f"Format ekspor '{format}' tidak dikenal (pilihan: md, json, txt).")

    # =========================================================
    # Statistik & siklus hidup
    # =========================================================
    def statistik(self) -> dict:
        """Ringkasan pemakaian: jumlah sesi/pesan + distribusi jawaban per tier."""
        with self._lock:
            n_sesi = self._conn.execute("SELECT COUNT(*) c FROM sesi").fetchone()["c"]
            n_pesan = self._conn.execute("SELECT COUNT(*) c FROM pesan").fetchone()["c"]
            rows = self._conn.execute(
                """
                SELECT tier, COUNT(*) n FROM pesan
                WHERE tier IS NOT NULL AND peran = 'assistant'
                GROUP BY tier ORDER BY n DESC
                """
            ).fetchall()
        return {
            "sesi": n_sesi,
            "pesan": n_pesan,
            "per_tier": {r["tier"] or "?": r["n"] for r in rows},
        }

    def tutup(self) -> None:
        """Tutup koneksi SQLite (aman dipanggil lebih dari sekali)."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass  # sudah pernah ditutup — tidak masalah
            logger.debug("Riwayat chat ditutup")

    def __enter__(self) -> "ChatHistory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.tutup()


# ------------------------------------------------------------
# Demo / test mandiri
# ------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("DEMO core/history.py — riwayat chat SQLite")
    print("=" * 60)

    # DB sementara supaya demo tidak meninggalkan sampah di folder data
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo-history.db"

        with ChatHistory(db) as riwayat:
            sesi = riwayat.buat_sesi()
            print(f"\n1) Sesi baru #{sesi} dibuat (judul: {JUDUL_DEFAULT})")

            riwayat.tambah_pesan(sesi, "user", "Halo, jam berapa sekarang?")
            riwayat.tambah_pesan(sesi, "assistant", "Sekarang pukul 10:15.",
                                 tier="tier2", durasi_ms=812.4)
            riwayat.tambah_pesan(sesi, "user", "Terima kasih!")
            riwayat.tambah_pesan(sesi, "assistant", "Sama-sama!", tier="tier1", durasi_ms=201.0)

            info = riwayat.get_sesi(sesi)
            print(f"2) Judul otomatis: {info['judul']!r}")

            print("\n3) Isi sesi:")
            for p in riwayat.pesan_sesi(sesi):
                print(f"   [{p['peran']:<9}] {p['isi'][:45]}")

            print("\n4) Cari keyword 'jam':")
            for p in riwayat.cari("jam"):
                print(f"   #{p['id']} [{p['peran']}] {p['isi'][:45]}")

            print("\n5) Format context untuk model:")
            print(f"   {riwayat.riwayat_chat(sesi, maks=2)}")

            print("\n6) Statistik:", riwayat.statistik())

            print("\n7) Ekspor Markdown (potongan):")
            for baris in riwayat.export_sesi(sesi, "md").splitlines()[:8]:
                print(f"   {baris}")

        # buka ulang -> data harus persist
        with ChatHistory(db) as lagi:
            print(f"\n8) Setelah dibuka ulang: {lagi.statistik()}")

    print("\nDemo selesai — DB sementara otomatis terhapus.")
