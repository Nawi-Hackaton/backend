"""
Ñawi — Script de ingestión del RAG (knowledge base del GORE Cusco).

Procesa los documentos de data/documentos/, los divide en chunks de 500 palabras
con overlap de 50, genera embeddings con OpenAI (text-embedding-3-small) y los guarda
en ChromaDB (colección "gore_cusco_docs", distancia coseno).

Se corre una sola vez (o cuando cambien los documentos):

    conda activate nawi
    python scripts/ingest.py

==============================================================================
PRINCIPIO DE ARQUITECTURA — PROTECCIÓN DE DATOS PERSONALES (Ley 29733)
==============================================================================
Los documentos que se ingestan al RAG y los chunks resultantes deben contener
ÚNICAMENTE información PÚBLICA del TUPA del GORE Cusco: requisitos de trámites,
plazos, costos, oficinas y procedimientos.

NUNCA deben incluirse datos personales del ciudadano (nombre completo, DNI,
número de teléfono, dirección, etc.) en:
  - los documentos que se ingestan a ChromaDB, ni
  - los chunks almacenados, ni
  - los prompts que luego se envían al LLM con esos chunks como contexto.

Motivo: la base vectorial es el *conocimiento* compartido y reutilizable de Ñawi;
mezclar ahí datos personales los expondría en cada búsqueda y en cada prompt al
LLM. Los datos personales viven exclusivamente en Supabase (el *estado*), con su
propio control de acceso, y jamás se vectorizan.

Esto es un requisito de cumplimiento de la Ley N° 29733 (Ley de Protección de
Datos Personales del Perú): minimización de datos y separación entre información
pública (RAG) e información personal (estado transaccional).
==============================================================================
"""

import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con los
# acentos y emojis de los mensajes. Esto garantiza impresión limpia.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Desactiva la telemetría de ChromaDB (evita un warning ruidoso por un bug de
# telemetría en la 0.5.0). El env var debe definirse ANTES de importar chromadb;
# además silenciamos el logger de telemetría, que es lo único 100% fiable en 0.5.0.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)

# --- Rutas resueltas desde la raíz del proyecto (no dependen del CWD) ---------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")

DOCS_DIR = PROJECT_ROOT / "data" / "documentos"

_chroma_raw = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
CHROMA_DB_PATH = (
    Path(_chroma_raw)
    if os.path.isabs(_chroma_raw)
    else (PROJECT_ROOT / _chroma_raw).resolve()
)

# --- Configuración ------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
COLLECTION_NAME = "gore_cusco_docs"
EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 500       # palabras por chunk
CHUNK_OVERLAP = 50     # palabras compartidas entre chunks consecutivos


def fail_clean(message: str, exit_code: int = 0) -> None:
    """Imprime un mensaje claro y termina sin traceback."""
    print(message)
    sys.exit(exit_code)


def check_prerequisites() -> list[Path]:
    """Valida API key y documentos antes de tocar nada. Devuelve la lista de archivos."""
    if not OPENAI_API_KEY:
        fail_clean(
            "\n[AVISO] Falta OPENAI_API_KEY.\n"
            "   El script necesita una clave de OpenAI para generar embeddings.\n\n"
            "   Cómo solucionarlo:\n"
            "     1. Entra a https://platform.openai.com → API Keys → Create new secret key.\n"
            "     2. Abre el archivo .env en la raíz del proyecto.\n"
            "     3. Pega la clave en la línea:  OPENAI_API_KEY=tu_clave_aqui\n"
            "     4. Vuelve a correr:  python scripts/ingest.py\n"
        )

    if not DOCS_DIR.exists():
        fail_clean(
            f"\n[AVISO] No existe la carpeta de documentos: {DOCS_DIR}\n"
            "   Créala y agrega los PDFs del GORE Cusco antes de continuar.\n"
        )

    files = sorted(
        p for p in DOCS_DIR.iterdir()
        if p.suffix.lower() in (".pdf", ".txt")
    )
    if not files:
        fail_clean(
            f"\n[AVISO] No hay documentos en {DOCS_DIR}\n"
            "   Agrega los documentos PÚBLICOS del GORE Cusco (PDF o .txt), por ejemplo:\n"
            "     - tupa.pdf\n"
            "     - manual_sgd_v3.pdf\n"
            "     - lineamiento_accesibilidad.pdf\n"
            "     - flujo_mesa_de_partes.txt\n\n"
            "   Recuerda: solo información pública (requisitos, plazos, oficinas).\n"
            "   NUNCA datos personales de ciudadanos (Ley 29733).\n\n"
            "   Luego vuelve a correr:  python scripts/ingest.py\n"
        )

    return files


def extract_text(path: Path) -> str:
    """Extrae el texto de un PDF (página por página) o de un .txt (UTF-8)."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages)

    # .txt
    return path.read_text(encoding="utf-8")


def _word_chunks(words: list[str], size: int, overlap: int) -> list[str]:
    """Divide una lista de palabras en chunks de `size` con `overlap` de solape."""
    if not words:
        return []
    step = size - overlap  # avance entre el inicio de un chunk y el siguiente
    chunks = []
    for start in range(0, len(words), step):
        piece = words[start:start + size]
        if piece:
            chunks.append(" ".join(piece))
        if start + size >= len(words):
            break  # ya cubrimos todas las palabras
    return chunks


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Divide el texto en chunks para el RAG.

    Si el documento está estructurado en secciones separadas por una línea de guiones
    (formato del TUPA: cada trámite es un bloque entre '---'), se hace UN chunk por
    sección. Así cada trámite —con sus requisitos, costo y plazo— queda en su propio
    chunk y el embedding no se diluye con el resto del documento (clave para que la
    búsqueda recupere el trámite correcto). Las secciones que excedan `size` palabras
    se subdividen por palabras.

    Si el documento no tiene esa estructura (p. ej. un PDF corrido), se usa el chunking
    clásico por palabras con solape.
    """
    sections = [s.strip() for s in re.split(r"\n\s*-{3,}\s*\n", text) if s.strip()]

    if len(sections) > 1:
        chunks: list[str] = []
        for section in sections:
            words = section.split()
            if len(words) <= size:
                chunks.append(section)
            else:
                chunks.extend(_word_chunks(words, size, overlap))
        return chunks

    return _word_chunks(text.split(), size, overlap)


def main() -> None:
    files = check_prerequisites()

    # Imports pesados solo después de validar prerrequisitos.
    import chromadb
    from chromadb.config import Settings
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)

    chroma_client = chromadb.PersistentClient(
        path=str(CHROMA_DB_PATH),
        settings=Settings(anonymized_telemetry=False),  # silencia el warning de telemetría
    )
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},  # distancia coseno
    )

    print(f"Documentos a procesar: {len(files)}")
    print(f"ChromaDB: {CHROMA_DB_PATH}")
    print(f"Modelo de embeddings: {EMBEDDING_MODEL}\n")

    added = 0
    skipped = 0

    for path in files:
        text = extract_text(path)
        chunks = chunk_text(text)
        if not chunks:
            print(f"   [AVISO] {path.name}: sin texto extraíble, se omite.")
            continue

        stem = path.stem  # nombre sin extensión
        print(f"   • {path.name}: {len(chunks)} chunks")

        for index, chunk in enumerate(chunks):
            chunk_id = f"{stem}_chunk_{index}"

            # Dedup: si el id ya existe, lo saltamos sin error.
            existing = collection.get(ids=[chunk_id])
            if existing["ids"]:
                skipped += 1
                continue

            try:
                response = client.embeddings.create(model=EMBEDDING_MODEL, input=chunk)
                embedding = response.data[0].embedding
            except Exception as exc:  # noqa: BLE001
                fail_clean(
                    f"\n[ERROR] No se pudo generar el embedding de {chunk_id}:\n"
                    f"   {type(exc).__name__}: {exc}\n\n"
                    "   Causas comunes: clave OPENAI_API_KEY inválida, sin saldo/cuota,\n"
                    "   o sin conexión a internet. Revisa tu cuenta en platform.openai.com.\n",
                    exit_code=1,
                )

            collection.add(
                ids=[chunk_id],
                embeddings=[embedding],
                documents=[chunk],
                metadatas=[{"source": path.name, "chunk_index": index}],
            )
            added += 1

    print("\nIngestión terminada.")
    print(f"   Chunks nuevos agregados: {added}")
    print(f"   Chunks saltados (ya existían): {skipped}")
    print(f"   Total en la colección '{COLLECTION_NAME}': {collection.count()}")


if __name__ == "__main__":
    main()
