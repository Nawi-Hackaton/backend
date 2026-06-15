from pathlib import Path

from dotenv import load_dotenv

# Cargar variables de entorno del .env ANTES de importar routers/servicios.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from backend.routes.webhook import router as webhook_router  # noqa: E402
from backend.routes.health import router as health_router  # noqa: E402
from backend.routes.api import router as api_router  # noqa: E402

app = FastAPI(
    title="Ñawi API",
    version="1.0.0",
    description="Backend del asistente conversacional Ñawi — GORE Cusco",
    docs_url="/docs",      # Swagger UI
    redoc_url="/redoc",    # ReDoc alternativo
)

# CORS abierto para la demo (el chat web llama a /api/tts y /api/debug/status desde otra URL).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(health_router)
app.include_router(api_router)
