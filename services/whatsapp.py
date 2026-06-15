"""
Ñawi — Servicio de canal WhatsApp (Meta Cloud API).

Es la entrada y salida del canal principal. Recibe (vía webhook) y envía mensajes de texto
y audio. Cumple el principio "un solo motor para todos los canales": aquí solo vive el
transporte WhatsApp; la lógica de flujos/intención está en el orquestador.

Regla de Ñawi: cada respuesta al usuario va SIEMPRE con texto + audio (nunca uno solo),
por eso el método principal de los flujos es send_text_and_audio.

Prueba directa:
    python backend/services/whatsapp.py 51999999999
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --- Configuración a nivel de módulo (se ejecuta UNA sola vez al importar) -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.whatsapp")

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
GRAPH = "https://graph.facebook.com/v19.0"


def _require_config() -> None:
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError(
            "Faltan WHATSAPP_ACCESS_TOKEN y/o WHATSAPP_PHONE_NUMBER_ID en el .env. "
            "Tómalos de la cuenta de Meta for Developers > App > WhatsApp > API Setup."
        )


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}


def _raise_for_status(resp: httpx.Response, label: str) -> None:
    """Si la respuesta no es 2xx, imprime el cuerpo de error (debugging) y relanza."""
    if resp.status_code // 100 != 2:
        logger.error("WhatsApp %s → HTTP %s: %s", label, resp.status_code, resp.text)
        print(f"[ERROR] WhatsApp {label}: HTTP {resp.status_code}: {resp.text}")
        resp.raise_for_status()


async def send_text(numero: str, texto: str) -> dict:
    """Envía un mensaje de texto al número dado. Devuelve la respuesta JSON de Meta."""
    _require_config()
    url = f"{GRAPH}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=_auth_headers(), json=payload)
    _raise_for_status(resp, "send_text")
    return resp.json()


async def send_audio(numero: str, audio_bytes: bytes) -> dict:
    """
    Envía una nota de voz: primero sube el audio a la Media API, luego envía el mensaje
    referenciando el media_id. Devuelve la respuesta JSON del envío.
    """
    _require_config()

    # Paso 1 — subir el audio (multipart/form-data).
    media_url = f"{GRAPH}/{WHATSAPP_PHONE_NUMBER_ID}/media"
    files = {"file": ("audio.ogg", audio_bytes, "audio/ogg")}
    data = {"messaging_product": "whatsapp", "type": "audio/ogg"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        up = await client.post(media_url, headers=_auth_headers(), files=files, data=data)
    _raise_for_status(up, "send_audio.upload")
    media_id = up.json()["id"]

    # Paso 2 — enviar el mensaje de audio con el media_id.
    msg_url = f"{GRAPH}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "audio",
        "audio": {"id": media_id},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(msg_url, headers=_auth_headers(), json=payload)
    _raise_for_status(resp, "send_audio.send")
    return resp.json()


async def send_text_and_audio(numero: str, texto: str, audio_bytes: bytes) -> None:
    """
    Método principal de los flujos: envía SIEMPRE texto + audio (primero el texto, luego
    el audio). Ñawi nunca responde con uno solo de los dos.
    """
    await send_text(numero, texto)
    await send_audio(numero, audio_bytes)


async def download_media(media_id: str) -> bytes:
    """Descarga los bytes de cualquier archivo entrante (documento PDF, etc.) por su media_id."""
    return await download_audio(media_id)


async def download_audio(media_id: str) -> bytes:
    """Descarga los bytes de una nota de voz entrante a partir de su media_id."""
    _require_config()
    async with httpx.AsyncClient(timeout=60.0) as client:
        meta = await client.get(f"{GRAPH}/{media_id}", headers=_auth_headers())
        _raise_for_status(meta, "download_audio.lookup")
        media_url = meta.json()["url"]

        # La descarga del binario también requiere el header de autorización.
        audio = await client.get(media_url, headers=_auth_headers())
        _raise_for_status(audio, "download_audio.fetch")
        return audio.content


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python backend/services/whatsapp.py <numero_destino>")
        raise SystemExit(1)

    numero_destino = sys.argv[1]
    try:
        resultado = asyncio.run(
            send_text(numero_destino, "Mensaje de prueba de Ñawi.")
        )
        print(f"[OK] Mensaje enviado a {numero_destino}: {resultado}")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[ERROR] No se pudo enviar: {type(exc).__name__}: {exc}\n\n"
            "   Revisa WHATSAPP_ACCESS_TOKEN y WHATSAPP_PHONE_NUMBER_ID en .env, y que el\n"
            "   número destino esté entre los de prueba verificados en Meta for Developers.\n"
        )
        raise SystemExit(1)
