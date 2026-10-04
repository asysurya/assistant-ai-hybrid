#!/usr/bin/env python3
# ============================================================
# AssistantAI — main.py
# ------------------------------------------------------------
# Entry point CLI untuk MENGUJI routing 3-tier dari terminal
# (GUI CustomTkinter menyusul di FASE 5).
#
# Pemakaian:
#   python main.py                      -> chat interaktif
#   python main.py --route "teks"       -> lihat keputusan router SAJA
#                                          (tanpa memanggil model apa pun)
#   python main.py --cek                -> diagnosis kesiapan sistem:
#                                          Ollama lokal, key cloud, router
#
# Perintah dalam mode chat:
#   /bantuan  daftar perintah
#   /route <teks>   klasifikasi teks tanpa memanggil model
#   /stats    statistik pemakaian tier
#   /keluar   keluar
# ============================================================

"""CLI AssistantAI — uji routing 3-tier langsung dari terminal."""

from __future__ import annotations

import argparse
import sys

from core.assistant import Assistant
from core.cloud_client import CloudClient
from core.config_manager import ConfigManager
from core.local_client import LocalClient, LocalClientError
from core.tier_router import TierRouter

GARIS = "-" * 60


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
# Mode 3: chat interaktif
# ------------------------------------------------------------
def mode_chat() -> int:
    manajer = ConfigManager()
    manajer.setup_logging()          # log rotasi ke %APPDATA%/AssistantAI/logs/
    try:
        asisten = Assistant(config=manajer)
    except Exception as exc:         # mis. paket openai belum terinstall
        print(f"Gagal menyiapkan assistant: {exc}")
        print("Solusi: pip install -r requirements.txt")
        return 1

    if manajer.is_first_run:
        print("Info: config.json belum ada (first-run). Wizard grafis menyusul di FASE 4.")
        print("      Untuk uji cloud sementara: set OLLAMA_CLOUD_KEYS=\"key1,key2\"")
    print(GARIS)
    print("CHAT INTERAKTIF — routing 3-tier")
    print("Perintah: /bantuan  /route <teks>  /stats  /keluar")
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
        if teks == "/bantuan":
            print("  /route <teks>  klasifikasi teks tanpa memanggil model")
            print("  /stats         statistik pemakaian tier")
            print("  /keluar        keluar")
            continue
        if teks.startswith("/route "):
            mode_route(teks[len("/route "):])
            continue
        if teks == "/stats":
            statistik = asisten.stats()
            print("  " + ", ".join(f"{k}={v}" for k, v in statistik.items()))
            continue

        try:
            reply = asisten.chat(teks)
        except ValueError as exc:
            print(exc)
            continue
        except Exception as exc:     # jaga loop tetap hidup dari error tak terduga
            print(f"Error tak terduga: {exc}")
            continue

        print(f"AI [{reply.tier_used.value}] > {reply.text}")
        if len(reply.escalation) > 1:
            jejak = " -> ".join(langkah.split(":")[0] for langkah in reply.escalation)
            print(f"   (jalur tier: {jejak})")
        print(f"   ({reply.duration_ms:.0f} ms)")
    return 0


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="AssistantAI — CLI uji routing 3-tier (GUI menyusul di FASE 5)")
    parser.add_argument("--route", metavar="TEKS",
                        help="klasifikasikan TEKS tanpa memanggil model")
    parser.add_argument("--cek", action="store_true",
                        help="diagnosis kesiapan: Ollama lokal, key cloud, router")
    args = parser.parse_args()

    if args.route is not None:
        mode_route(args.route)
        return 0
    if args.cek:
        mode_cek()
        return 0
    return mode_chat()


if __name__ == "__main__":
    sys.exit(main())
