# ============================================================
# AssistantAI — core package
# Modul inti (backend): konfigurasi, key pool, klien model,
# router, tools, agent loop, parser, dll. (bertambah per fase)
# ============================================================

"""Paket inti (backend) AssistantAI.

Modul saat ini (FASE 1 — batch pertama):
    config          — konstanta & path terpusat (single source of truth)
    config_manager  — config.json + first-run + logging rotasi
    key_pool        — rotasi API key Ollama Cloud + cooldown 429

Modul menyusul: local_client, cloud_client, router, vision,
tools, agent_loop, parser, downloader, model_registry, dll.
"""

__version__ = "0.1.0"
__all__ = ["config", "config_manager", "key_pool"]
