# ============================================================
# AssistantAI — voice package
# Voice input (STT: Vosk) + voice output (TTS: Piper / pyttsx3)
# — diisi mulai FASE 8. Model di-download on-demand (FASE 9) ke
# %APPDATA%/AssistantAI/models/ dan di-unload setelah idle 5 menit.
# ============================================================

"""Paket voice AssistantAI.

Rencana modul (FASE 8):
    stt.py — speech-to-text via Vosk (vosk-model-small-id-0.22, ~50 MB)
    tts.py — text-to-speech via Piper (id_ID-news_tts-medium, ~80 MB)
             dengan fallback pyttsx3 (Windows SAPI) yang selalu tersedia.
"""

__version__ = "0.1.0"
