"""
Ñawi — Endpoints auxiliares para el frontend y diagnóstico.

  - POST /api/tts            → genera audio con ElevenLabs (para el chat web).
  - POST /api/rag            → respuesta real desde los documentos del GORE (RAG + LLM).
  - POST /api/session        → guarda/actualiza la sesión del chat web en Supabase.
  - POST /api/registro       → registra al ciudadano del chat web (tabla usuarios).
  - POST /api/expediente     → crea un expediente del chat web (tabla expedientes).
  - GET  /api/debug/status   → estado de ChromaDB, Supabase, ElevenLabs y OpenAI.

Todos los endpoints de persistencia usan el prefijo "web-" en numero_whatsapp para
distinguir las sesiones del chat web de las de WhatsApp.
"""

import base64
import os
from pathlib import Path

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from backend.services import database, llm, pdf_generator, qellqa, rag, session, tts

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _web_numero(session_id: str) -> str:
    """Normaliza el id de sesión web a la forma 'web-<id>' (clave en Supabase)."""
    sid = (session_id or "").strip()
    return sid if sid.startswith("web-") else "web-" + sid


@router.post("/api/tts")
async def api_tts(request: Request):
    """Convierte texto a audio usando ElevenLabs (devuelve base64)."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Falta el campo 'text'.")
    try:
        audio = await tts.synthesize(text)  # bytes OGG (o MP3 de fallback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="TTS error: " + str(exc)[:160])
    return {"audio_base64": base64.b64encode(audio).decode("ascii"), "mime": "audio/ogg"}


@router.post("/api/rag")
async def api_rag(request: Request):
    """Responde una consulta de requisitos con el RAG real (documentos del GORE) + LLM."""
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Falta el campo 'query'.")
    try:
        chunks = await rag.search(query)
        # Cada trámite del TUPA es ahora un chunk autocontenido (requisitos, costo, plazo).
        # Para una consulta de requisitos pasamos solo el mejor chunk al LLM, así la
        # respuesta se enfoca en el trámite preguntado y no mezcla varios.
        top = chunks[:1] if chunks else []
        answer = await llm.generate_response("CONSULTAR_REQUISITOS", top, {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="RAG error: " + str(exc)[:160])
    return {
        "answer": answer,
        "found": len(chunks),
        "sources": [c.get("source") for c in chunks],
        "scores": [c.get("score") for c in chunks],
    }


@router.post("/api/session")
async def api_session(request: Request):
    """Guarda o actualiza la sesión del chat web en Supabase (tabla sesiones)."""
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Falta el campo 'session_id'.")
    numero = _web_numero(session_id)
    datos = {
        "estado_flujo": (body.get("estado_flujo") or "INICIO"),
        "datos_recolectados": body.get("datos") or {},
        "historial": body.get("historial") or [],
        "contador_no_entendi": int(body.get("contador_no_entendi") or 0),
    }
    try:
        saved = await session.save_session(numero, datos)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Session error: " + str(exc)[:160])
    return {"ok": True, "id": saved.get("id"), "numero_whatsapp": numero}


@router.post("/api/registro")
async def api_registro(request: Request):
    """Registra al ciudadano del chat web (tabla usuarios); idempotente por session_id."""
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    nombre = (body.get("nombre") or "").strip()
    dni = (body.get("dni") or "").strip()
    if not (session_id and nombre and dni):
        raise HTTPException(status_code=400, detail="Faltan campos: session_id, nombre, dni.")
    numero = _web_numero(session_id)
    try:
        existing = await database.get_user(numero)
        if existing:
            return {"ok": True, "usuario_id": existing["id"], "nuevo": False}
        user = await database.create_user(nombre, dni, numero, verified=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Registro error: " + str(exc)[:160])
    return {"ok": True, "usuario_id": user["id"], "nuevo": True}


@router.post("/api/expediente")
async def api_expediente(request: Request):
    """Crea un expediente del chat web (tabla expedientes)."""
    body = await request.json()
    usuario_id = (body.get("usuario_id") or "").strip()
    nombre_tramite = (body.get("nombre_tramite") or "Trámite web").strip()
    numero_exp = (body.get("numero_expediente") or "EXP-WEB").strip()
    if not usuario_id:
        raise HTTPException(status_code=400, detail="Falta el campo 'usuario_id'.")
    try:
        exp = await database.create_expediente(
            usuario_id=usuario_id,
            nombre_tramite=nombre_tramite,
            numero_expediente=numero_exp,
            estado="Recibido",
            oficina_actual="Mesa de Partes",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Expediente error: " + str(exc)[:160])
    return {"ok": True, "expediente_id": exp["id"], "numero_expediente": exp.get("numero_expediente")}


# ---------------------------------------------------------------------------
# Proxies de QELLQA (para que el chat web no tenga problemas de CORS)
# ---------------------------------------------------------------------------

@router.get("/api/qellqa/dependencias")
async def qellqa_dependencias():
    """Proxy: lista de dependencias reales del GORE Cusco (QELLQA)."""
    return {"dependencias": await qellqa.get_dependencias()}


@router.get("/api/qellqa/tipo-documentos")
async def qellqa_tipo_documentos(iddependencia: int):
    """Proxy: tipos de documento reales por dependencia (QELLQA)."""
    return {"tipos": await qellqa.get_tipo_documentos(iddependencia)}


@router.get("/api/qellqa/persona")
async def qellqa_persona(tipo: str = "DNI", nro: str = ""):
    """Proxy: valida un documento contra RENIEC vía QELLQA. Devuelve datos reales o 404."""
    if not nro.strip():
        raise HTTPException(status_code=400, detail="Falta el parámetro 'nro'.")
    datos = await qellqa.consultar_persona(tipo, nro)
    if not datos:
        raise HTTPException(status_code=404, detail="No se encontró la persona / documento.")
    return {"persona": datos}


@router.get("/api/qellqa/expediente")
async def qellqa_expediente(nro: int, dep: int, anio: int):
    """Proxy: estado real (historial de movimientos) de un expediente (QELLQA)."""
    movimientos = await qellqa.consultar_estado_expediente(nro, dep, anio)
    if movimientos is None:
        raise HTTPException(status_code=404, detail="No se encontró el expediente.")
    return {"movimientos": movimientos}


@router.post("/api/qellqa/tramite")
async def qellqa_tramite(request: Request):
    """
    Inicia un trámite REAL en QELLQA desde el chat web.

    ATENCIÓN: si todo va bien, esto crea un expediente real en el sistema del GORE Cusco.
    Pasos: validar DNI → generar PDF de solicitud → subir PDF → emitir trámite → guardar
    en Supabase. Devuelve el idtramite real.
    """
    body = await request.json()
    dni = (body.get("dni") or "").strip()
    iddependencia = body.get("iddependencia")
    idtipodocumento = body.get("idtipodocumento")
    asunto = (body.get("asunto") or "").strip()
    nrofolios = int(body.get("nrofolios") or 1)
    celular = (body.get("celular") or "").strip()
    correo = (body.get("correo") or "").strip()
    session_id = (body.get("session_id") or "").strip()
    dependencia_nombre = (body.get("dependencia_nombre") or "").strip()

    if not (dni and iddependencia and asunto):
        raise HTTPException(status_code=400, detail="Faltan: dni, iddependencia, asunto.")

    # Resolver el id real del tipo de documento desde el nombre elegido por el ciudadano
    # (vía el GET de QELLQA). Si no se indicó o no calza, se usa SOLICITUD.
    if not idtipodocumento:
        nombre_tipo = (body.get("tipodoc_nombre") or "SOLICITUD").strip().upper()
        tipos = await qellqa.get_tipo_documentos(int(iddependencia))
        elegido = next((t for t in tipos if (t.get("nombre") or "").upper() == nombre_tipo), None)
        if not elegido:
            elegido = next((t for t in tipos if nombre_tipo in (t.get("nombre") or "").upper()), None)
        if not elegido:
            elegido = next((t for t in tipos if (t.get("nombre") or "").upper() == "SOLICITUD"), None)
        if not elegido and tipos:
            elegido = tipos[0]
        if not elegido:
            raise HTTPException(status_code=502, detail="No se pudieron obtener los tipos de documento de QELLQA.")
        idtipodocumento = elegido["idtipodocumento"]

    # 1) Validar identidad (RENIEC vía QELLQA).
    persona = await qellqa.consultar_persona("DNI", dni)
    if not persona:
        raise HTTPException(status_code=404, detail="DNI no validado en RENIEC.")

    # 2) Documento: si el ciudadano subió un PDF, lo usamos; si no, Ñawi genera uno demo.
    archivo_b64 = body.get("archivo_base64")
    archivo_nombre = (body.get("archivo_nombre") or "solicitud_nawi.pdf").strip()
    if archivo_b64:
        try:
            pdf_bytes = base64.b64decode(archivo_b64)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="archivo_base64 inválido.")
    else:
        pdf_bytes = pdf_generator.generar_solicitud_pdf({
            "nombre": persona.get("razonSocial") or persona.get("nombres"),
            "dni": dni, "celular": celular, "correo": correo,
            "tipo_documento": "SOLICITUD", "dependencia": dependencia_nombre,
            "asunto": asunto, "nrofolios": nrofolios,
        })
        archivo_nombre = "solicitud_nawi.pdf"

    # 3) Subir el PDF.
    archivo = await qellqa.subir_archivo(pdf_bytes, archivo_nombre, int(iddependencia))
    if not archivo:
        raise HTTPException(status_code=502, detail="No se pudo subir el documento a QELLQA.")

    # 4) Emitir el trámite real.
    datos_emitir = {
        "tipoDocumentoPersona": "DNI",
        "iddependencia": int(iddependencia),
        "nroDocumentoPersona": dni,
        "nombres": persona.get("nombres"),
        "apellidoPaterno": persona.get("apellidoPaterno"),
        "apellidoMaterno": persona.get("apellidoMaterno"),
        "celular": celular,
        "correo": correo,
        "idtipodocumento": int(idtipodocumento),
        "nrodocumento": 0,
        "asunto": asunto,
        "nrofolios": nrofolios,
        "adjunto": archivo.get("url"),
        "idFiles": [archivo.get("idfile")] if archivo.get("idfile") else [],
        "idFilesAnexos": [],
        "idtupa": body.get("idtupa"),
        "linkAnexo": body.get("linkAnexo") or "",
    }
    emitido = await qellqa.emitir_tramite(datos_emitir)
    if not emitido:
        raise HTTPException(status_code=502, detail="No se pudo emitir el trámite en QELLQA.")
    idtramite = emitido.get("idtramite")

    # 5) Guardar en Supabase (usuario + expediente con el idtramite real).
    anio = datetime.now(timezone.utc).year
    try:
        numero = ("web-" + session_id) if session_id and not session_id.startswith("web-") else (session_id or "web-anon")
        user = await database.get_user(numero)
        if not user:
            nombre = (persona.get("razonSocial") or persona.get("nombres") or "").strip()
            user = await database.create_user(nombre, dni, numero, verified=True)
        await database.create_expediente(
            usuario_id=user["id"], nombre_tramite=asunto,
            numero_expediente=str(idtramite), estado="Recibido", oficina_actual="Mesa de Partes",
            id_dependencia=int(iddependencia), anio=anio, idtramite=str(idtramite),
        )
    except Exception as exc:  # noqa: BLE001
        # El trámite real ya se emitió; no fallamos por un problema de persistencia.
        print(f"[WARN] tramite emitido {idtramite} pero no se guardó en Supabase: {type(exc).__name__}")

    return {
        "ok": True,
        "idtramite": idtramite,
        "mensaje_confirmacion": f"Tu trámite fue registrado con el número {idtramite} en el sistema del GORE Cusco.",
    }


@router.get("/api/debug/status")
async def debug_status():
    """Estado de todos los servicios (ChromaDB, Supabase, ElevenLabs, OpenAI)."""
    out = {}

    # ChromaDB
    try:
        import chromadb
        from chromadb.config import Settings
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        raw = os.getenv("CHROMA_DB_PATH", "./data/chroma_db")
        path = raw if os.path.isabs(raw) else str(PROJECT_ROOT / raw)
        col = chromadb.PersistentClient(
            path=path, settings=Settings(anonymized_telemetry=False)
        ).get_collection("gore_cusco_docs")
        cnt = col.count()
        out["chromadb"] = {"chunks": cnt, "collection": "gore_cusco_docs",
                           "status": "ready" if cnt > 0 else "empty"}
    except Exception as exc:  # noqa: BLE001
        out["chromadb"] = {"chunks": 0, "collection": "gore_cusco_docs",
                           "status": "error", "error": str(exc)[:140]}

    # Supabase
    try:
        client = database._get_client()
        present, counts = [], {}
        for tname in ["usuarios", "expedientes", "sesiones", "notificaciones", "otp_sessions"]:
            try:
                r = client.table(tname).select("*", count="exact").limit(0).execute()
                present.append(tname)
                if tname in ("usuarios", "expedientes", "sesiones"):
                    counts[tname] = r.count if getattr(r, "count", None) is not None else 0
            except Exception:
                pass
        out["supabase"] = {"connected": True, "tables": present, "counts": counts}
    except Exception as exc:  # noqa: BLE001
        out["supabase"] = {"connected": False, "tables": [], "counts": {}, "error": str(exc)[:140]}

    # ElevenLabs / OpenAI (solo presencia de credenciales)
    out["elevenlabs"] = {
        "configured": bool(os.getenv("ELEVENLABS_API_KEY", "").strip()),
        "voice_id": os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
    }
    out["openai"] = {"configured": bool(os.getenv("OPENAI_API_KEY", "").strip())}
    return out
