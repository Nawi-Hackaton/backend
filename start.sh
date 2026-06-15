#!/usr/bin/env bash
set -e

# Llena ChromaDB con los documentos del GORE (el RAG). Es idempotente; con volumen
# persistente no re-ingesta. Sin volumen, se regenera en cada arranque (~1 min).
echo "[start] Ingestando documentos para el RAG..."
python scripts/ingest.py || echo "[start] ingest terminó con avisos; continúo."

echo "[start] Levantando el backend..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
