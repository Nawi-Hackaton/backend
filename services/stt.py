"""
Ñawi — Servicio STT (voz a texto), con backend seleccionable.

Cuando el usuario manda una nota de voz por WhatsApp, Ñawi la descarga, la transcribe a
texto y pasa ese texto al enrutador de intención (Flujo 2 / Flujo 5).

Backend configurable con STT_BACKEND en el .env:
  - "openai" (por defecto): usa la API de transcripción de OpenAI (la MISMA API key del
    LLM/embeddings). No necesita GPU ni descargar modelos; rápido y preciso en español.
  - "local": usa Whisper en local. Usa la GPU (CUDA) automáticamente si está disponible
    (p. ej. una RTX 4070), y si no, CPU. Para cambiar a GPU más tarde:
        1) instalar PyTorch con CUDA:
           pip install torch --index-url https://download.pytorch.org/whl/cu121
        2) poner STT_BACKEND=local en el .env (y opcional WHISPER_MODEL=medium).

Carga perezosa: ni el cliente de OpenAI ni el modelo de Whisper se inicializan al importar;
solo cuando se transcribe por primera vez. Así, con STT_BACKEND=openai no se carga Whisper.

Prueba directa:
    python backend/services/stt.py audio.ogg
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.stt")

STT_BACKEND = os.getenv("STT_BACKEND", "openai").strip().lower()
STT_OPENAI_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1").strip()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small").strip()  # solo para backend local
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Backend OpenAI (API) — carga perezosa del cliente
# ---------------------------------------------------------------------------

_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def _transcribe_openai(audio_bytes: bytes) -> str:
    """Transcribe con la API de OpenAI. El audio entrante es OGG/Opus (de WhatsApp)."""
    client = _get_openai()
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    path = tmp.name
    tmp.write(audio_bytes)
    tmp.close()
    try:
        with open(path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model=STT_OPENAI_MODEL, file=f, language="es"
            )
        return (getattr(resp, "text", "") or "").strip()
    finally:
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Backend local (Whisper) — carga perezosa del modelo, GPU si está disponible
# ---------------------------------------------------------------------------

_local_model = None
_local_device = None


def _get_local_model():
    global _local_model, _local_device
    if _local_model is None:
        import torch  # noqa: PLC0415
        import whisper  # noqa: PLC0415

        _local_device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("Whisper local en %s (modelo %s).", _local_device, WHISPER_MODEL)
        _local_model = whisper.load_model(WHISPER_MODEL, device=_local_device)
    return _local_model


async def _transcribe_local(audio_bytes: bytes) -> str:
    def _op() -> str:
        model = _get_local_model()
        tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        path = tmp.name
        try:
            tmp.write(audio_bytes)
            tmp.close()  # cerrar antes de que Whisper/ffmpeg lo abra (necesario en Windows)
            result = model.transcribe(path, language="es")
            return (result.get("text") or "").strip()
        finally:
            if os.path.exists(path):
                os.remove(path)

    return await asyncio.to_thread(_op)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe los bytes de un audio (nota de voz) a texto en español."""
    if STT_BACKEND == "local":
        return await _transcribe_local(audio_bytes)
    return await _transcribe_openai(audio_bytes)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python backend/services/stt.py <ruta_audio>")
        raise SystemExit(1)

    print(f"Backend STT: {STT_BACKEND}" + (f" (modelo {STT_OPENAI_MODEL})" if STT_BACKEND != "local" else f" (Whisper {WHISPER_MODEL})"))
    ruta = sys.argv[1]
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
        texto = asyncio.run(transcribe(datos))
        print(f"\nTranscripción:\n{texto}\n")
    except FileNotFoundError:
        print(f"[ERROR] No se encontró el archivo: {ruta}")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] No se pudo transcribir: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
