#!/usr/bin/env python3
# ============================================================
# AssistantAI — main.py
# ------------------------------------------------------------
# Entry point CLI untuk MENGUJI fitur inti dari terminal
# (GUI CustomTkinter menyusul di FASE 5).
#
# Fitur FASE 2 yang terlihat di sini:
#   - Setiap sesi chat otomatis TERSIMPAN ke SQLite (history.db)
#   - Chat punya KONTEKS: N pesan terakhir dikirim ulang ke model
#   - TOOLS aktif: model bisa memanggil waktu/kalkulator/file/todo
#     (tool tulis_file selalu minta konfirmasi dulu)
#
# Pemakaian:
#   python main.py                      -> chat interaktif
#   python main.py --route "teks"       -> lihat keputusan router SAJA
#   python main.py --cek                -> diagnosis kesiapan sistem
#   python main.py --riwayat            -> daftar sesi chat tersimpan
#
# Perintah dalam mode chat:
#   /bantuan                  daftar perintah
#   /baru                     mulai sesi baru
#   /riwayat                  daftar sesi terakhir
#   /sesi [id]                tampilkan isi sesi (default: sesi aktif)
#   /cari <kata>              cari pesan di semua sesi
#   /export <id> [md|json|txt] ekspor sesi ke file
#   /alat                     daftar tools yang tersedia
#   /route <teks>             klasifikasi teks tanpa memanggil model
#   /stats                    statistik pemakaian tier
#   /keluar                   keluar
# ============================================================

"""CLI AssistantAI — chat 3-tier + riwayat SQLite + tools."""

from __future__ import annotations

import argparse
import json
import sys

from core import config as cfg
from core.assistant import Assistant
from core.cloud_client import CloudClient
from core.config_manager import ConfigManager
from core.history import ChatHistory
from core.local_client import LocalClient, LocalClientError
from core.tools import ToolRegistry
from core.tier_router import TierRouter

GARIS = "-" * 60
LABEL_PERAN = {"user": "Anda", "assistant": "AI", "system": "Sistem", "tool": "Alat"}


# ------------------------------------------------------------
# Mode 1: inspeksi keputusan router (tanpa call model)
# ------------------------------------------------------------
def mode_route(teks: str) -> None:
    print(TierRouter().explain(teks))


# ------------------------------------------------------------
# Mode 2: diagnosis kesiapan sistem
# ------------------------------------------------------------
def mode_cek() -> None:
    print("CEK KESIAPAN ASSISTANTAI")
    print(GARIS)

    manajer = ConfigManager()
    print(f"Folder data : {manajer.config_file.parent}")
    print(f"First run   : {'YA (wizard menyusul di FASE 4)' if manajer.is_first_run else 'tidak'}")
    print(f"Tier 1      : {manajer.get('tiers.tier1_model')}")
    print(f"Tier 2      : {manajer.get('tiers.tier2_model')}")
    print(f"Tier 3      : {manajer.get('tiers.tier3_model')}")
    print(f"Escalation  : {manajer.get('tiers.escalation_enabled')}")
    print(f"Tools       : {'AKTIF' if manajer.get('tools.enabled') else 'nonaktif'}"
          f" (workspace: {manajer.get('tools.workspace')})")
    print(GARIS)

    # --- Router ---
    print("Router (heuristik keyword):")
    router = TierRouter()
    contoh = [
        ("halo, apa kabar?",                       "harusnya tier1"),
        ("tambah todo beli susu",                  "harusnya tier2 (tools)"),
        ("buatkan kode scraper python lengkap",    "harusnya tier3 (berat)"),
    ]
    for teks, ekspektasi in contoh:
        d = router.classify(teks)
        print(f"  \"{teks}\" -> {d.tier.value}  ({ekspektasi})")
    print(GARIS)

    # --- Ollama lokal ---
    try:
        lokal = LocalClient()
        if lokal.is_available():
            print("Ollama lokal : TERSEDIA")
            print(f"             -> {lokal.base_url}")
        else:
            print("Ollama lokal : TIDAK JALAN  (mulai: ollama serve)")
    except LocalClientError as exc:
        print(f"Ollama lokal : GAGAL — {exc}")
    print(GARIS)

    # --- Ollama Cloud ---
    cloud = CloudClient()
    if cloud.is_available():
        print(f"Cloud        : {len(cloud.pool)} key terdaftar ({', '.join(cloud.pool.masked_keys)})")
        print("             -> kesehatan key dicek saat dipakai (429 -> rotasi)")
    else:
        print("Cloud        : BELUM ADA API KEY")
        print("             -> set environment OLLAMA_CLOUD_KEYS (key1,key2)")
    print(GARIS)

    print("Bila keduanya tidak siap, chat tetap bisa dicoba —")
    print("semua tier gagal akan menghasilkan pesan ramah, bukan crash.")


# ------------------------------------------------------------
# Mode 3: daftar sesi tersimpan (dari terminal, tanpa chat)
# ------------------------------------------------------------
def mode_riwayat(limit: int = 15) -> None:
    riwayat = ChatHistory()
    sesi_list = riwayat.daftar_sesi(limit)
    if not sesi_list:
        print("Belum ada sesi chat tersimpan. Mulai dengan: python main.py")
        return
    print(f"SESI CHAT TERAKHIR (maks. {limit})")
    print(GARIS)
    _tampilkan_daftar_sesi(sesi_list)
    riwayat.tutup()


def _tampilkan_daftar_sesi(sesi_list: list[dict]) -> None:
    """Cetak tabel sesi rapi (dipakai mode_riwayat & perintah /riwayat)."""
    for s in sesi_list:
        judul = s["judul"] if len(s["judul"]) <= 46 else s["judul"][:46] + "..."
        print(f"  #{s['id']:<4} {s['jumlah_pesan']:>3} pesan | {s['diperbarui_pada']} | {judul}")


def _tampilkan_isi_sesi(riwayat: ChatHistory, sesi_id: int) -> None:
    """Cetak seluruh pesan satu sesi (dipakai /sesi)."""
    sesi = riwayat.get_sesi(sesi_id)
    if sesi is None:
        print(f"Sesi #{sesi_id} tidak ditemukan. Lihat /riwayat.")
        return
    print(f"SESI #{sesi['id']}: {sesi['judul']}")
    print(f"(dibuat {sesi['dibuat_pada']}, diperbarui {sesi['diperbarui_pada']})")
    print(GARIS)
    pesan = riwayat.pesan_sesi(sesi_id)
    if not pesan:
        print("  (belum ada pesan)")
        return
    for p in pesan:
        label = LABEL_PERAN.get(p["peran"], p["peran"])
        meta = []
        if p.get("tier"):
            meta.append(f"tier: {p['tier']}")
        if p.get("durasi_ms"):
            meta.append(f"{p['durasi_ms']:.0f} ms")
        suffix = f"  [{', '.join(meta)}]" if meta else ""
        print(f"{label}{suffix} >")
        for baris in p["isi"].splitlines() or [""]:
            print(f"  {baris}")
        print()


# ------------------------------------------------------------
# Konfirmasi tool yang menulis/mengubah data (dipakai agent)
# ------------------------------------------------------------
def _konfirmasi_cli(tool, argumen: dict) -> bool:
    """Tanya izin user di terminal sebelum tool menulis (mis. tulis_file)."""
    try:
        arg_str = json.dumps(argumen, ensure_ascii=False)[:200]
    except Exception:
        arg_str = str(argumen)[:200]
    print(f"\n  [IZIN DIBUTUHKAN] model meminta '{tool.nama}':")
    for baris in arg_str.splitlines():
        print(f"    {baris}")
    try:
        jawab = input("  Izinkan? [y/N] > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return jawab in ("y", "ya", "yes")


# ------------------------------------------------------------
# Mode 4: chat interaktif (inti CLI)
# ------------------------------------------------------------
def mode_chat() -> int:
    manajer = ConfigManager()
    log_root = manajer.setup_logging()   # log rotasi ke %APPDATA%/AssistantAI/logs/
    # Terminal cukup tampilkan ERROR saja — INFO/WARNING tetap lengkap di file
    # log, supaya percakapan chat tidak berantakan dengan baris log internal.
    import logging
    for h in log_root.handlers:
        if type(h) is logging.StreamHandler:     # persis handler konsol (bukan file)
            h.setLevel(logging.ERROR)

    # --- siapkan tools (bila diaktifkan di config) ---
    registry = None
    if manajer.get("tools.enabled", cfg.TOOLS_ENABLED):
        try:
            registry = ToolRegistry()
        except Exception as exc:     # tools gagal -> chat tetap jalan
            print(f"Peringatan: tools nonaktif ({exc})")

    try:
        asisten = Assistant(config=manajer, tools=registry)
    except Exception as exc:         # mis. paket openai belum terinstall
        print(f"Gagal menyiapkan assistant: {exc}")
        print("Solusi: pip install -r requirements.txt")
        return 1

    # agent minta izin user (di terminal) sebelum tool menulis data
    if asisten.agent is not None:
        asisten.agent.konfirmasi = _konfirmasi_cli

    # --- riwayat chat (FASE 2) ---
    riwayat = ChatHistory()
    sesi_id = riwayat.buat_sesi()
    maks_konteks = int(manajer.get("history.max_context_messages",
                                   cfg.HISTORY_MAX_CONTEXT))

    if manajer.is_first_run:
        print("Info: config.json belum ada (first-run). Wizard grafis menyusul di FASE 4.")
        print("      Untuk uji cloud sementara: set OLLAMA_CLOUD_KEYS=\"key1,key2\"")
    print(GARIS)
    print("CHAT INTERAKTIF — routing 3-tier + riwayat + tools")
    print(f"Sesi aktif  : #{sesi_id} (tersimpan otomatis di history.db)")
    print(f"Konteks     : {maks_konteks} pesan terakhir diingat dalam satu sesi")
    print(f"Tools       : {', '.join(registry.nama_tools()) if registry else '(nonaktif)'}")
    print("Perintah    : /bantuan untuk daftar lengkap")
    print(GARIS)

    while True:
        try:
            teks = input("Anda > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nSampai jumpa!")
            break

        if not teks:
            continue
        if teks in ("/keluar", "/exit", "/q"):
            print("Sampai jumpa!")
            break

        # ---------- perintah internal ----------
        if teks == "/bantuan":
            _cetak_bantuan()
            continue
        if teks == "/baru":
            sesi_id = riwayat.buat_sesi()
            print(f"Sesi baru #{sesi_id} dimulai.")
            continue
        if teks == "/riwayat":
            _tampilkan_daftar_sesi(riwayat.daftar_sesi(10))
            continue
        if teks == "/sesi" or teks.startswith("/sesi "):
            bagian = teks.split(maxsplit=1)
            target = sesi_id
            if len(bagian) > 1:
                if not bagian[1].isdigit():
                    print("Pemakaian: /sesi [id]  (contoh: /sesi 3)")
                    continue
                target = int(bagian[1])
            _tampilkan_isi_sesi(riwayat, target)
            continue
        if teks.startswith("/cari "):
            kata = teks[len("/cari "):].strip()
            hasil = riwayat.cari(kata, limit=10)
            if not hasil:
                print(f"Tidak ada pesan yang cocok dengan '{kata}'.")
                continue
            print(f"Hasil pencarian '{kata}' ({len(hasil)} pesan terbaru):")
            for p in hasil:
                label = LABEL_PERAN.get(p["peran"], p["peran"])
                print(f"  [sesi #{p['sesi_id']}] {label}: {p['isi'][:70]}")
            continue
        if teks.startswith("/export"):
            _eksekusi_export(riwayat, teks)
            continue
        if teks == "/alat":
            if registry is None:
                print("Tools nonaktif (config: tools.enabled=false).")
            else:
                print(registry.ringkas())
            continue
        if teks.startswith("/route "):
            mode_route(teks[len("/route "):])
            continue
        if teks == "/stats":
            statistik = asisten.stats()
            print("  Sesi ini : " + ", ".join(f"{k}={v}" for k, v in statistik.items()))
            print(f"  Database : {riwayat.statistik()}")
            continue

        # ---------- chat biasa (dengan konteks dari riwayat) ----------
        try:
            konteks = riwayat.riwayat_chat(sesi_id, maks=maks_konteks)
            reply = asisten.chat(teks, history=konteks)
        except ValueError as exc:
            print(exc)
            continue
        except Exception as exc:     # jaga loop tetap hidup dari error tak terduga
            print(f"Error tak terduga: {exc}")
            continue

        print(f"AI [{reply.tier_used.value}] > {reply.text}")
        # jejak tool yang dipakai model utk menjawab
        for alat in asisten.tools_terakhir:
            print(f"   (alat) {alat}")
        if len(reply.escalation) > 1:
            jejak = " -> ".join(langkah.split(":")[0] for langkah in reply.escalation)
            print(f"   (jalur tier: {jejak})")
        print(f"   ({reply.duration_ms:.0f} ms)")

        # ---------- simpan ke riwayat ----------
        try:
            riwayat.tambah_pesan(sesi_id, "user", teks)
            riwayat.tambah_pesan(
                sesi_id, "assistant", reply.text,
                tier=reply.tier_used.value, durasi_ms=reply.duration_ms,
            )
            if asisten.tools_terakhir:
                riwayat.tambah_pesan(sesi_id, "tool",
                                     " | ".join(asisten.tools_terakhir))
        except Exception as exc:     # gagal simpan TIDAK boleh memotong chat
            print(f"   (peringatan: gagal menyimpan riwayat: {exc})")

    riwayat.tutup()
    return 0


# ------------------------------------------------------------
# Helper CLI
# ------------------------------------------------------------
def _cetak_bantuan() -> None:
    print("  /baru                  mulai sesi chat baru")
    print("  /riwayat               daftar sesi tersimpan")
    print("  /sesi [id]             isi sesi (tanpa id = sesi aktif)")
    print("  /cari <kata>           cari pesan di semua sesi")
    print("  /export <id> [md|json|txt]  ekspor sesi ke file")
    print("  /alat                  daftar tools untuk model")
    print("  /route <teks>          klasifikasi teks tanpa memanggil model")
    print("  /stats                 statistik pemakaian tier + database")
    print("  /keluar                keluar")


def _eksekusi_export(riwayat: ChatHistory, perintah: str) -> None:
    """Parse & jalankan `/export <id> [md|json|txt]` (default md)."""
    bagian = perintah.split()
    if len(bagian) < 2 or not bagian[1].isdigit():
        print("Pemakaian: /export <id> [md|json|txt]  (contoh: /export 3 md)")
        return
    sesi_id = int(bagian[1])
    fmt = bagian[2].lower() if len(bagian) > 2 else "md"
    try:
        isi = riwayat.export_sesi(sesi_id, format=fmt)
    except ValueError as exc:
        print(f"Gagal ekspor: {exc}")
        return
    folder = cfg.APP_DIR / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    berkas = folder / f"sesi-{sesi_id}.{fmt}"
    try:
        berkas.write_text(isi, encoding="utf-8")
    except OSError as exc:
        print(f"Gagal menulis file: {exc}")
        return
    print(f"Terekspor ke: {berkas}")


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="AssistantAI — CLI chat 3-tier + riwayat + tools (GUI: FASE 5)")
    parser.add_argument("--route", metavar="TEKS",
                        help="klasifikasikan TEKS tanpa memanggil model")
    parser.add_argument("--cek", action="store_true",
                        help="diagnosis kesiapan: Ollama lokal, key cloud, router")
    parser.add_argument("--riwayat", action="store_true",
                        help="tampilkan daftar sesi chat tersimpan lalu keluar")
    args = parser.parse_args()

    if args.route is not None:
        mode_route(args.route)
        return 0
    if args.cek:
        mode_cek()
        return 0
    if args.riwayat:
        mode_riwayat()
        return 0
    return mode_chat()


if __name__ == "__main__":
    sys.exit(main())
