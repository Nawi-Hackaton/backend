"""
Ñawi — Servicio RAG (búsqueda semántica sobre los documentos del GORE Cusco).

Expone `search(query)`, usada por los flujos que necesitan información pública del
TUPA:
  - Flujo 4 (Consultar requisitos de un trámite): busca los chunks del trámite y
    se los pasa al LLM como contexto para responder SIN inventar.
  - Flujo 5 (Iniciar un trámite): identifica el trámite y sus campos requeridos.

Coherencia con la ingestión (IMPORTANTE):
  Los embeddings de la query deben generarse con EL MISMO modelo que usó
  scripts/ingest.py — OpenAI "text-embedding-3-small" (1536 dims). Mezclar modelos
  produciría vectores de otro espacio e incomparables con la colección, devolviendo
  resultados sin sentido.

  Nota: OpenAI NO usa `task_type` (el distintivo retrieval_query / retrieval_document
  es un concepto de Gemini). OpenAI emplea el mismo modelo para query y documento.

Ley 29733: esta búsqueda opera solo sobre información PÚBLICA (requisitos, plazos,
oficinas). Nunca se vectorizan ni se consultan datos personales del ciudadano.

Prueba directa:
    python backend/services/rag.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con los
# acentos y emojis de los mensajes de prueba.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --- Configuración a nivel de módulo (se ejecuta UNA sola vez al importar) -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# Silencia la telemetría de ChromaDB (bug ruidoso en la 0.5.0). El env var debe
# definirse antes de importar chromadb; el logger es lo 100% fiable en 0.5.0.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)

import chromadb  # noqa: E402  (debe importarse tras fijar la env var de telemetría)
from chromadb.config import Settings  # noqa: E402
from openai import OpenAI  # noqa: E402

logger = logging.getLogger("nawi.rag")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

_chroma_raw = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
CHROMA_DB_PATH = (
    Path(_chroma_raw)
    if os.path.isabs(_chroma_raw)
    else (PROJECT_ROOT / _chroma_raw).resolve()
)

COLLECTION_NAME = "gore_cusco_docs"
EMBEDDING_MODEL = "text-embedding-3-small"  # debe coincidir con ingest.py
SCORE_THRESHOLD = 0.30  # umbral ajustado para text-embedding-3-small (similitudes coseno
# más bajas que otros modelos). El LLM solo responde con lo que aparezca en los chunks; si
# no hay info suficiente, lo dice. Para mayor precisión: re-ingestar con chunks más pequeños.

# Clientes inicializados una sola vez (no por llamada).
_openai_client = OpenAI(api_key=OPENAI_API_KEY)
_chroma_client = chromadb.PersistentClient(
    path=str(CHROMA_DB_PATH),
    settings=Settings(anonymized_telemetry=False),
)
_collection = _chroma_client.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"},  # misma métrica que la ingestión
)


def _embed_query(text: str) -> list[float]:
    """Genera el embedding de la query con OpenAI (mismo modelo que la ingestión)."""
    response = _openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


async def search(query: str, n_results: int = 5) -> list[dict]:
    """
    Busca los chunks más relevantes para `query` en la colección del GORE Cusco.

    Devuelve una lista de dicts ordenada por relevancia descendente:
        {"text": str, "source": str, "score": float}
    donde score = 1 - distancia_coseno, redondeado a 3 decimales, y solo se
    incluyen chunks con score >= SCORE_THRESHOLD.

    Si la colección está vacía, loguea un warning y devuelve [].
    """
    if _collection.count() == 0:
        logger.warning(
            "La colección '%s' está vacía. Corre `python scripts/ingest.py` "
            "para llenar ChromaDB antes de buscar.",
            COLLECTION_NAME,
        )
        return []

    # La librería de OpenAI y el query de Chroma son síncronos; los movemos a un
    # hilo para no bloquear el event loop de FastAPI.
    query_embedding = await asyncio.to_thread(_embed_query, query)
    raw = await asyncio.to_thread(
        _collection.query,
        query_embeddings=[query_embedding],
        n_results=n_results,
    )

    # Chroma devuelve listas anidadas (una por query); tomamos la primera.
    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]
    distances = raw.get("distances", [[]])[0]

    results: list[dict] = []
    for text, metadata, distance in zip(documents, metadatas, distances):
        score = 1 - distance  # distancia coseno -> similitud
        if score < SCORE_THRESHOLD:
            continue
        results.append(
            {
                "text": text,
                "source": (metadata or {}).get("source", "desconocido"),
                "score": round(score, 3),
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    test_query = "certificado de trabajo"
    print(f"Buscando: {test_query!r}\n")

    try:
        hits = asyncio.run(search(test_query))
    except Exception as exc:  # noqa: BLE001
        print(
            f"[ERROR] No se pudo ejecutar la búsqueda:\n"
            f"   {type(exc).__name__}: {exc}\n\n"
            "   Causas comunes: OPENAI_API_KEY ausente/ inválida en .env, sin saldo,\n"
            "   o ChromaDB sin datos (corre `python scripts/ingest.py`).\n"
        )
        raise SystemExit(1)

    if not hits:
        print(
            "Sin resultados con score >= "
            f"{SCORE_THRESHOLD}. ¿Está ChromaDB lleno? "
            "Corre `python scripts/ingest.py`."
        )
    else:
        for i, hit in enumerate(hits, 1):
            print(f"[{i}] score={hit['score']}  source={hit['source']}")
            preview = hit["text"][:200].replace("\n", " ")
            print(f"    {preview}...\n")
