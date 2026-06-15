"""
Ñawi — Servicio TTS (texto a audio), con backend seleccionable.

Convierte la respuesta de texto de Ñawi en audio (OGG/Opus) para el chat web y las notas de
voz de WhatsApp. Backend configurable con TTS_BACKEND en el .env:
  - "openai" (recomendado): usa la API de TTS de OpenAI (la MISMA API key del LLM). No tiene
    el límite de créditos del free tier de ElevenLabs.
  - "elevenlabs": usa ElevenLabs (voz muy natural, pero el free tier tiene cuota mensual).

Ambos backends devuelven MP3 y se convierte a OGG/Opus con pydub (ffmpeg), porque WhatsApp
solo reproduce notas de voz en OGG/Opus.

Prueba directa:
    python backend/services/tts.py
"""

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydub import AudioSegment

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.tts")

TTS_BACKEND = os.getenv("TTS_BACKEND", "elevenlabs").strip().lower()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TTS_OPENAI_MODEL = os.getenv("TTS_OPENAI_MODEL", "tts-1").strip()
TTS_OPENAI_VOICE = os.getenv("TTS_OPENAI_VOICE", "nova").strip()

# ElevenLabs
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL = "eleven_turbo_v2_5"
_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"

_openai_client = None


def _mp3_to_ogg(mp3_bytes: bytes) -> bytes:
    """Convierte MP3 a OGG con códec OPUS (lo que reproduce WhatsApp y el navegador)."""
    audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    out = io.BytesIO()
    audio.export(out, format="ogg", codec="libopus")
    return out.getvalue()


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def _synthesize_openai(text: str) -> bytes:
    client = _get_openai()
    resp = await client.audio.speech.create(
        model=TTS_OPENAI_MODEL, voice=TTS_OPENAI_VOICE, input=text, response_format="mp3"
    )
    mp3_bytes = resp.content if hasattr(resp, "content") else resp.read()
    return _mp3_to_ogg(mp3_bytes)


async def _synthesize_elevenlabs(text: str) -> bytes:
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        raise RuntimeError("Faltan ELEVENLABS_API_KEY y/o ELEVENLABS_VOICE_ID en el .env.")
    url = f"{_API_BASE}/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "accept": "audio/mpeg", "content-type": "application/json"}
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.08},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        mp3_bytes = response.content
    try:
        return _mp3_to_ogg(mp3_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.error("Falló la conversión MP3→OGG (%s). Devuelvo MP3 como fallback.", exc)
        return mp3_bytes


async def _synth_by(backend: str, text: str) -> bytes:
    if backend == "openai":
        return await _synthesize_openai(text)
    return await _synthesize_elevenlabs(text)


async def synthesize(text: str) -> bytes:
    """
    Sintetiza `text` a audio (OGG/Opus) con el backend configurado y, si ese falla (p. ej.
    ElevenLabs sin créditos), intenta AUTOMÁTICAMENTE el otro backend. Así, igual que en la
    web (que cae a la voz del navegador), WhatsApp casi siempre obtiene audio.
    Solo si ambos fallan se propaga el error (y el orquestador envía solo texto).
    """
    primary = "openai" if TTS_BACKEND == "openai" else "elevenlabs"
    secondary = "elevenlabs" if primary == "openai" else "openai"
    try:
        return await _synth_by(primary, text)
    except Exception as exc:  # noqa: BLE001
        logger.error("TTS '%s' falló (%s); intento con '%s'.", primary, type(exc).__name__, secondary)
        return await _synth_by(secondary, text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("Backend TTS:", TTS_BACKEND)
    texto = "Hola, soy Ñawi, tu asistente del Gobierno Regional de Cusco."
    try:
        audio = asyncio.run(synthesize(texto))
        with open("test_tts.ogg", "wb") as f:
            f.write(audio)
        print(f"Audio generado ({len(audio)} bytes) -> test_tts.ogg")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] No se pudo sintetizar: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
