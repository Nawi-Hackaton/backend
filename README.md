# Ñawi — Backend (API + orquestador)

API en **Python + FastAPI**. Es un solo motor para todos los canales (WhatsApp y web):
recibe mensajes, decide el flujo, consulta el RAG y las APIs reales del GORE (QELLQA),
genera texto + audio y persiste el estado en Supabase.

## Cómo correrlo

```bash
conda activate nawi            # entorno con Python 3.11
python scripts/ingest.py       # llena ChromaDB (RAG) la primera vez
uvicorn backend.main:app --reload --port 8000
# Swagger: http://localhost:8000/docs
```

Necesita las variables del `.env` (ver `.env.example`).

## Estructura

```
backend/
  main.py        → app FastAPI, CORS y registro de routers
  routes/        → endpoints HTTP (la "puerta de entrada")
  services/      → lógica por servicio (funciones async, sin estado propio)
  models/        → modelos de datos (placeholder)
```

### `routes/` — endpoints
| Archivo | Qué expone |
|---|---|
| `webhook.py` | **WhatsApp**: `GET/POST /webhook` (recibe mensajes y **orquesta todo el flujo**: intención → requisitos/iniciar/estado), y `POST /test/notify` (notificación proactiva). |
| `health.py` | `GET /health` y `GET /api/debug/qellqa` (diagnóstico de la API del GORE). |
| `api.py` | Endpoints para el chat web y diagnóstico: `POST /api/tts`, `POST /api/rag`, `POST /api/session`, `POST /api/registro`, `POST /api/expediente`, los proxies `/api/qellqa/*` (dependencias, persona/RENIEC, expediente, tramite) y `GET /api/debug/status`. |

### `services/` — lógica (un archivo por servicio)
| Archivo | Qué hace |
|---|---|
| `llm.py` | **LLM** (gpt-4o-mini): clasifica la intención y genera la respuesta anclada al RAG. |
| `rag.py` | **RAG**: búsqueda semántica en ChromaDB (embeddings de OpenAI). Solo info pública. |
| `qellqa.py` | **APIs reales del GORE** (QELLQA): dependencias, tipos de documento, **RENIEC** (persona), **estado** de expediente, **subir** PDF y **emitir** trámite. |
| `database.py` | **Supabase**: usuarios y expedientes. Datos personales **cifrados** (Ley 29733). |
| `session.py` | Estado de cada conversación (tabla `sesiones`); cifra las claves sensibles. |
| `identity.py` | Validación de identidad (DNI → RENIEC vía QELLQA) y OTP (no conectado al flujo). |
| `crypto.py` | Cifrado **Fernet** de datos personales. |
| `stt.py` | **Voz a texto**: API de OpenAI (`STT_BACKEND=openai`) o Whisper local (GPU). |
| `tts.py` | **Texto a voz**: ElevenLabs, salida **OGG/Opus** (web y notas de voz de WhatsApp). |
| `whatsapp.py` | Transporte WhatsApp (Meta Cloud API): enviar texto/audio, descargar media. |
| `pdf_generator.py` | Genera el PDF de solicitud demo (reportlab) cuando el ciudadano no sube uno. |

## Qué es real vs maqueta
- **Real (QELLQA):** dependencias, tipos de documento, RENIEC, estado de expediente, subir y
  emitir trámite (crea un expediente real en el GORE).
- **Maqueta:** validación facial (representa el RENIEC biométrico); el OTP existe pero no está
  conectado; el DNI `12345678` es ficticio para la demo (`DEMO_MODE=true`).

## Privacidad (Ley 29733)
- ChromaDB guarda solo **información pública** (requisitos). Nunca datos personales.
- Supabase guarda el **estado**; nombre, DNI y otros datos personales van **cifrados** (Fernet)
  y no se escriben en logs.

## Nota sobre archivos legacy
En `routes/` hay archivos duplicados antiguos (`database.py`, `llm.py`, `rag.py`, `session.py`,
`stt.py`, `tts.py`, `whatsapp.py`) que **no se usan** (la versión vigente está en `services/`).
Pueden eliminarse sin afectar nada.
"# backend" 
