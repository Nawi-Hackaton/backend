"""
Ñawi — Servicio TTS (texto a audio) con ElevenLabs.

Convierte la respuesta de texto de Ñawi en audio para enviarlo por WhatsApp junto con el
texto. ElevenLabs da una voz neural muy natural en español, clave para un usuario que solo
escucha.

Detalles:
  - Se llama por HTTP con httpx.AsyncClient (no el SDK), para mantener consistencia async.
  - Modelo "eleven_multilingual_v2" + ELEVENLABS_VOICE_ID del .env.
  - ElevenLabs devuelve MP3; se convierte a OGG con pydub (ffmpeg) porque WhatsApp espera
    OGG para notas de voz. Si la conversión falla, se devuelve el MP3 como fallback.

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

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --- Configuración a nivel de módulo (se ejecuta UNA sola vez al importar) -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.tts")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL = "eleven_turbo_v2_5"  # free tier + multilingüe (buen español).
# eleven_multilingual_v2 requiere plan de pago. Usa voces "premade"; las voces de la
# librería/"professional" están bloqueadas por API en el free tier.
_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"


def _mp3_to_ogg(mp3_bytes: bytes) -> bytes:
    """
    Convierte MP3 a OGG con códec OPUS (no Vorbis).

    WhatsApp solo reproduce notas de voz en OGG/Opus; el OGG/Vorbis que pydub genera por
    defecto se sube pero no se reproduce. El navegador (chat web) también reproduce OGG/Opus.
    """
    audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    out = io.BytesIO()
    audio.export(out, format="ogg", codec="libopus")
    return out.getvalue()


async def synthesize(text: str) -> bytes:
    """
    Sintetiza `text` a audio con ElevenLabs y devuelve los bytes en OGG (o MP3 si la
    conversión falla).
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        raise RuntimeError(
            "Faltan ELEVENLABS_API_KEY y/o ELEVENLABS_VOICE_ID en el archivo .env. "
            "Crea la cuenta en elevenlabs.io, copia tu API key (Profile > API Key) y el "
            "Voice ID de una voz en español (Voices)."
        )

    url = f"{_API_BASE}/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        # speed: 1.0 = normal; 1.08 = un poco más rápida (turbo_v2_5 admite 0.7–1.2).
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.08},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        mp3_bytes = response.content

    # WhatsApp necesita OGG; si la conversión falla, devolvemos el MP3 como fallback.
    try:
        return _mp3_to_ogg(mp3_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.error("Falló la conversión MP3→OGG (%s). Devuelvo MP3 como fallback.", exc)
        return mp3_bytes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    texto = "Hola, soy Ñawi, tu asistente del Gobierno Regional de Cusco."
    salida = "test_tts.ogg"
    try:
        audio = asyncio.run(synthesize(texto))
        with open(salida, "wb") as f:
            f.write(audio)
        print(f"Audio generado y guardado en {salida} ({len(audio)} bytes). Ábrelo y escúchalo.")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[ERROR] No se pudo sintetizar: {type(exc).__name__}: {exc}\n\n"
            "   Revisa ELEVENLABS_API_KEY y ELEVENLABS_VOICE_ID en .env, y que ffmpeg esté\n"
            "   disponible en el entorno (ya viene en el conda env 'nawi').\n"
        )
        raise SystemExit(1)
