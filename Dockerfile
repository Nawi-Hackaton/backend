# Imagen del backend de Ñawi — para el repo "backend" autocontenido.
# El código se coloca bajo /app/backend para que "import backend.*" funcione,
# y scripts/ + data/ quedan en /app (el código espera PROJECT_ROOT = /app).
FROM python:3.11-slim

# ffmpeg lo necesita pydub (convertir el audio de ElevenLabs a OGG/Opus).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app PORT=8000

COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt

# Paquete del backend (main.py, routes/, services/, models/) bajo /app/backend
COPY __init__.py main.py /app/backend/
COPY routes  /app/backend/routes
COPY services /app/backend/services
COPY models  /app/backend/models
# Datos del RAG y script de ingesta en /app
COPY scripts /app/scripts
COPY data    /app/data
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
