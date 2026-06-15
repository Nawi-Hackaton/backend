"""
Ñawi — Webhook y orquestador principal (corazón del sistema, Flujo 2).

Recibe los mensajes de WhatsApp, recupera la sesión, decide el flujo según el estado y la
intención, genera la respuesta (texto + audio) y la envía. Todo mensaje al usuario va
SIEMPRE con texto + audio.

Ruteo "estado primero": si la conversación está en medio de un flujo (registro, recolección),
el mensaje se trata como un dato, no como una intención nueva.

Ver docs/CONTEXT.md (9 flujos) y docs/USER_STORIES.md (US-06, US-09, US-10, US-11, US-14).
"""

import base64
import os
import random
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response

from backend.services import (
    database, identity, llm, pdf_generator, qellqa, rag, session, stt, tts, whatsapp,
)

router = APIRouter()

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
MESA_DE_PARTES_TELEFONO = "(084) 000-0000"
ANIO_POR_DEFECTO = 2026
# URL del micrositio web donde el ciudadano valida su identidad (con módulo facial).
WEB_URL = os.getenv("WEB_URL", "http://localhost:5500").strip()
MENU_CHOICES = {
    "2": "INICIAR_TRAMITE",
    "3": "CONSULTAR_ESTADO",
    "4": "HABLAR_CON_PERSONA",
}

TERMINOS_TEXTO = (
    "Antes de continuar, te informo las condiciones del trámite. "
    "La Mesa de Partes Virtual atiende de lunes a viernes de 8 de la mañana a 4 de la tarde. "
    "Los documentos deben estar en PDF, máximo 10 megabytes. "
    "Una vez validado, recibirás un correo con tu número de expediente. "
    "¿Aceptas los términos y condiciones? Responde sí o no."
)

# Modo demo: agrega pistas con datos de prueba a los mensajes de validación. Apágalo con
# DEMO_MODE=false en el .env para producción.
DEMO_MODE = os.getenv("DEMO_MODE", "true").strip().lower() in ("1", "true", "yes", "si", "sí")
DEMO_HINT_DNI = "Para la demostración, usa el DNI ficticio 12345678 y el nombre María."
DEMO_HINT_EXP = "Para la demostración: expediente 12, dependencia GERAGRI (Agricultura), año 2026."


def _demo(texto: str, hint: str) -> str:
    """Agrega una pista de datos de prueba al final del mensaje si DEMO_MODE está activo."""
    return texto + ("\n\n(" + hint + ")" if DEMO_MODE else "")


# ---------------------------------------------------------------------------
# Helpers de QELLQA (traducción a lenguaje simple, listas, búsqueda)
# ---------------------------------------------------------------------------

_SALUDOS = {
    "hola", "ola", "holi", "buenas", "buenos dias", "buenos días", "buen dia", "buen día",
    "buenas tardes", "buenas noches", "hi", "hello", "hey", "alo", "aló", "saludos",
    "que tal", "qué tal", "buenas tardes ñawi", "hola ñawi",
}


def _es_saludo(text: str) -> bool:
    t = (text or "").strip().lower().strip("¡!.,?¿ ")
    return t in _SALUDOS


def _email_valido(correo: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", (correo or "").strip()))


def _es_si(text: str) -> bool:
    return (text or "").strip().lower() in ("si", "sí", "acepto", "ok", "claro", "ya", "afirmativo")


def _es_no(text: str) -> bool:
    return (text or "").strip().lower() in ("no", "nel", "negativo", "rechazo")


def _estado_legible(estado: str) -> str:
    e = (estado or "").upper()
    return {
        "SIN RECIBIR": "pendiente de recepción",
        "RECIBIDO": "recibido",
        "DERIVADO": "derivado",
    }.get(e, (estado or "").lower())


async def _dependencias_texto() -> str:
    deps = await qellqa.get_dependencias()
    if not deps:
        return ""
    return "; ".join(f"{i + 1}. {d.get('nombre')}" for i, d in enumerate(deps))


async def _elegir_dependencia(text: str):
    """Devuelve la dependencia elegida por número de la lista o por coincidencia de texto."""
    deps = await qellqa.get_dependencias()
    if not deps:
        return None
    t = (text or "").strip()
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(deps):
            return deps[idx]
    tl = t.lower()
    for d in deps:
        nombre = (d.get("nombre") or "").lower()
        if tl and (tl in nombre or nombre.split(" - ")[0].strip() == tl):
            return d
    return None


def _fecha_corta(iso: str) -> str:
    s = (iso or "")[:10]
    if len(s) == 10 and s[4] == "-":
        return s[8:10] + "/" + s[5:7] + "/" + s[0:4]
    return s


def _seguimiento_a_texto(movs: list, nro, anio) -> str:
    """Traduce el historial técnico del expediente a una lista clara (numerada)."""
    lineas = [f"Tu expediente número {nro} del año {anio} tiene {len(movs)} movimientos:"]
    for i, m in enumerate(movs, 1):
        fecha = _fecha_corta(m.get("fecha"))
        accion = (m.get("accion") or "").strip() or "Movimiento registrado"
        destino = (m.get("destino") or "").strip()
        linea = f"{i}. {fecha}: {accion}"
        if destino:
            linea += f" (destino: {destino})"
        lineas.append(linea)
    ultimo = movs[-1]
    cola = (ultimo.get("destino") or "").strip()
    lineas.append(
        "Estado actual: "
        + _estado_legible(ultimo.get("estadorecepcion"))
        + (f", en {cola}" if cola else "")
        + "."
    )
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Webhook verification (GET) — Meta lo llama al configurar el webhook
# ---------------------------------------------------------------------------

@router.get("/webhook")
async def verify_webhook(request: Request):
    """Verificación del webhook de Meta."""
    params = dict(request.query_params)
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Invalid verify token")


# ---------------------------------------------------------------------------
# Incoming message (POST) — Meta envía cada mensaje aquí
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def receive_message(request: Request):
    """Recibe mensajes de WhatsApp (Meta Cloud API)."""
    body = await request.json()
    try:
        value = body["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            # Sin mensajes (p. ej. recibos de entrega / status updates): ignorar.
            return {"status": "no_message"}

        msg = messages[0]
        sender = msg["from"]
        msg_type = msg["type"]

        # Paso 1 — obtener el texto (transcribir si es audio) o el documento adjunto.
        documento_bytes = None
        documento_nombre = None
        if msg_type == "text":
            text = msg["text"]["body"]
        elif msg_type == "audio":
            audio_bytes = await whatsapp.download_audio(msg["audio"]["id"])
            text = await stt.transcribe(audio_bytes)
        elif msg_type == "document":
            doc = msg.get("document", {})
            documento_bytes = await whatsapp.download_media(doc.get("id"))
            documento_nombre = doc.get("filename") or "documento.pdf"
            text = "[documento adjunto]"
        else:
            print(f"[Ñawi] mensaje de {sender} ignorado: tipo no soportado '{msg_type}'")
            return {"status": "ignored"}

        # Diagnóstico: registrar qué recibió/transcribió Ñawi (sin datos sensibles extra).
        print(f"[Ñawi] mensaje de {sender} ({msg_type}): {text!r}")

        await process_message(sender, text, documento_bytes, documento_nombre)

    except Exception as exc:  # noqa: BLE001
        # Siempre devolvemos ok: si no, WhatsApp reintenta y puede crear bucles.
        print(f"Error processing message: {type(exc).__name__}: {exc}")

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Orquestador: ruteo "estado primero" + clasificación de intención
# ---------------------------------------------------------------------------

async def process_message(sender: str, text: str, documento: bytes = None, documento_nombre: str = None) -> None:
    sess = await session.get_session(sender)
    estado = (sess.get("estado_flujo") or "INICIO").upper()
    datos = sess.get("datos_recolectados") or {}
    sess["datos_recolectados"] = datos

    # Handoff desde la página web: el usuario llega con sus preferencias (idioma/audio) en el
    # mensaje prellenado. Arrancamos fresco mostrando el menú; la identidad se valida aquí.
    low = text.lower()
    if "vengo de la página web" in low or "vengo de la pagina web" in low:
        datos["idioma"] = "qu" if ("quechua" in low or "runa" in low) else "es"
        sess["contador_no_entendi"] = 0
        sess["estado_flujo"] = "MENU"
        user = await database.get_user(sender)
        saludo = "¡Hola de nuevo! " if user else (await llm.generate_message("bienvenida") + "\n\n")
        await _finish(sender, saludo + await llm.generate_message("menu"), sess)
        return

    # Un saludo SIEMPRE reinicia al menú, aun a mitad de un flujo (p. ej. atascado pidiendo
    # DNI de un intento anterior). Es coincidencia exacta, así que un nombre o asunto reales
    # no lo disparan. Sin esto, "Hola" caería en el manejador del estado y confundiría.
    if _es_saludo(text):
        sess["contador_no_entendi"] = 0
        sess["datos_recolectados"] = {}
        sess["estado_flujo"] = "MENU"
        user = await database.get_user(sender)
        saludo = "¡Hola de nuevo! " if user else (await llm.generate_message("bienvenida") + "\n\n")
        await _finish(sender, saludo + await llm.generate_message("menu"), sess)
        return

    # --- Estados en medio de un flujo: el mensaje es un dato, no una intención ---

    if estado == "INICIO":
        user = await database.get_user(sender)
        if user:
            estado = "MENU"            # usuario que regresa: no repetir bienvenida (CP-06.2)
            sess["estado_flujo"] = "MENU"
        else:
            sess["estado_flujo"] = "MENU"
            await _finish(sender, await llm.generate_message("bienvenida"), sess)
            return

    if estado == "REGISTRO_NOMBRE":
        datos["nombre"] = text.strip()
        sess["estado_flujo"] = "REGISTRO_DNI"
        await _finish(sender, _demo(await llm.generate_message("registro_dni"), DEMO_HINT_DNI), sess)
        return

    if estado == "REGISTRO_DNI":
        dni = "".join(ch for ch in text if ch.isdigit())
        nombre = datos.get("nombre", "").strip()
        if not dni:
            await _finish(sender, _demo("Necesito tu número de DNI. ¿Me lo repites, por favor?", DEMO_HINT_DNI), sess)
            return
        # Validación REAL contra RENIEC vía QELLQA.
        persona = await identity.validate_dni_reniec(dni, nombre)
        if not persona:
            await _finish(
                sender,
                _demo(
                    "No pude validar tu identidad. Verifica que tu DNI y tu nombre coincidan con "
                    "tu documento. ¿Cuál es tu número de DNI?",
                    DEMO_HINT_DNI,
                ),
                sess,
            )
            return
        nombre_real = (persona.get("razonSocial") or nombre).strip()
        try:
            await database.create_user(nombre_real, dni, sender, verified=True)
        except Exception as exc:  # noqa: BLE001
            print(f"create_user error: {type(exc).__name__}: {exc}")
        datos["nombre"] = nombre_real
        datos["dni"] = dni
        # Si venía de iniciar un trámite, seguir con los términos; si no, volver al menú.
        if datos.get("_accion") == "iniciar_tramite":
            sess["estado_flujo"] = "TRAMITE_TERMINOS"
            await _finish(sender, f"Validé tu identidad, {nombre_real}. {TERMINOS_TEXTO}", sess)
            return
        sess["datos_recolectados"] = {}
        sess["estado_flujo"] = "MENU"
        await _finish(
            sender, await llm.generate_message("registro_exitoso", {"nombre": nombre_real}), sess
        )
        return

    # --- Flujo 5: recolección del trámite real (QELLQA) ---

    if estado == "TRAMITE_TERMINOS":
        if _es_si(text):
            datos["terminos_aceptados"] = True
            datos["fecha_aceptacion"] = datetime.now().isoformat()
            sess["estado_flujo"] = "TRAMITE_DEP"
            lista = await _dependencias_texto()
            await _finish(sender, "Gracias. ¿A qué dependencia va dirigido tu trámite? Estas son: " + lista, sess)
            return
        sess["estado_flujo"] = "MENU"
        await _finish(sender, "Entiendo. Volvamos al menú. " + await llm.generate_message("menu"), sess)
        return

    if estado == "TRAMITE_DEP":
        dep = await _elegir_dependencia(text)
        if not dep:
            await _finish(sender, "No identifiqué la dependencia. Dime el número o el nombre de la lista, por favor.", sess)
            return
        datos["iddependencia"] = dep["iddependencia"]
        datos["dependencia_nombre"] = dep["nombre"]
        sess["estado_flujo"] = "TRAMITE_TIPODOC"
        await _finish(sender, f"Elegiste {dep['nombre']}. ¿Qué tipo de documento vas a presentar? Por ejemplo: SOLICITUD, CARTA o INFORME.", sess)
        return

    if estado == "TRAMITE_TIPODOC":
        tipo = await _elegir_tipo_documento(text, datos.get("iddependencia"))
        if not tipo:
            await _finish(sender, "No reconocí ese tipo de documento. Dime, por ejemplo, SOLICITUD.", sess)
            return
        datos["idtipodocumento"] = tipo["idtipodocumento"]
        datos["tipodoc_nombre"] = tipo["nombre"]
        sess["estado_flujo"] = "TRAMITE_ASUNTO"
        await _finish(sender, f"Bien, {tipo['nombre']}. Ahora dime brevemente el asunto de tu trámite.", sess)
        return

    if estado == "TRAMITE_ASUNTO":
        if len(text.strip()) < 3:
            await _finish(sender, "El asunto es obligatorio. Cuéntame brevemente para qué es tu solicitud.", sess)
            return
        datos["asunto"] = text.strip()
        sess["estado_flujo"] = "TRAMITE_FOLIOS"
        await _finish(sender, "¿Cuántas hojas o folios tiene tu documento? Dime un número.", sess)
        return

    if estado == "TRAMITE_FOLIOS":
        folios = "".join(ch for ch in text if ch.isdigit())
        if not folios:
            await _finish(sender, "Dime el número de folios (hojas) de tu documento, por ejemplo 1.", sess)
            return
        datos["nrofolios"] = int(folios)
        sess["estado_flujo"] = "TRAMITE_CELULAR"
        await _finish(sender, "¿Cuál es tu número de celular para contacto?", sess)
        return

    if estado == "TRAMITE_CELULAR":
        celular = "".join(ch for ch in text if ch.isdigit())
        if len(celular) < 6:
            await _finish(sender, "Necesito un número de celular válido (al menos 6 dígitos). ¿Me lo repites?", sess)
            return
        datos["celular"] = celular
        sess["estado_flujo"] = "TRAMITE_CORREO"
        await _finish(sender, "¿Cuál es tu correo electrónico? Ahí llegará tu número de expediente.", sess)
        return

    if estado == "TRAMITE_CORREO":
        correo = text.strip()
        if not _email_valido(correo):
            await _finish(sender, "Ese correo no parece válido. Escríbelo completo, por ejemplo nombre@correo.com.", sess)
            return
        datos["correo"] = correo
        sess["estado_flujo"] = "TRAMITE_ADJUNTO"
        await _finish(
            sender,
            "¿Quieres adjuntar tu documento en PDF? Envíamelo como documento (máximo 10 MB), "
            "o responde 'no' para que yo genere uno por ti.",
            sess,
        )
        return

    if estado == "TRAMITE_ADJUNTO":
        if documento:
            datos["adjunto_b64"] = base64.b64encode(documento).decode("ascii")
            datos["adjunto_nombre"] = documento_nombre or "documento.pdf"
            adjunto_txt = f"Recibí tu documento: {datos['adjunto_nombre']}. "
        elif _es_no(text) or "no" in text.lower():
            datos["adjunto_b64"] = None
            adjunto_txt = "De acuerdo, generaré un documento por ti. "
        else:
            await _finish(sender, "Envíame tu documento en PDF, o responde 'no' para que yo genere uno.", sess)
            return
        sess["estado_flujo"] = "TRAMITE_CONFIRM"
        resumen = (
            adjunto_txt + "Te leo el resumen para confirmar. "
            f"Trámite: {datos.get('tipodoc_nombre')}. "
            f"Dependencia: {datos.get('dependencia_nombre')}. "
            f"Asunto: {datos.get('asunto')}. "
            f"Folios: {datos.get('nrofolios')}. "
            f"Celular: {datos.get('celular')}. "
            f"Correo: {datos.get('correo')}. "
            "¿Confirmas el envío? Responde sí o no."
        )
        await _finish(sender, resumen, sess)
        return

    if estado == "TRAMITE_CONFIRM":
        if not _es_si(text):
            sess["datos_recolectados"] = {}
            sess["estado_flujo"] = "MENU"
            await _finish(sender, "No envié nada. Volvamos al menú cuando quieras.", sess)
            return
        await _emitir_tramite_real(sender, sess)
        return

    # --- Flujo 7: consulta de estado real (QELLQA) ---

    if estado == "ESTADO_EXP":
        nro = "".join(ch for ch in text if ch.isdigit())
        if not nro:
            await _finish(sender, "Dime el número de expediente, solo los dígitos.", sess)
            return
        datos["nro_expediente"] = int(nro)
        sess["estado_flujo"] = "ESTADO_DEP"
        lista = await _dependencias_texto()
        await _finish(sender, "¿En qué dependencia está tu trámite? Estas son: " + lista, sess)
        return

    if estado == "ESTADO_DEP":
        dep = await _elegir_dependencia(text)
        if not dep:
            await _finish(sender, "No identifiqué la dependencia. Dime el número o el nombre, por favor.", sess)
            return
        datos["id_dependencia"] = dep["iddependencia"]
        sess["estado_flujo"] = "ESTADO_ANIO"
        await _finish(sender, f"¿De qué año es el expediente? Si es de este año, di {ANIO_POR_DEFECTO}.", sess)
        return

    if estado == "ESTADO_ANIO":
        anio_txt = "".join(ch for ch in text if ch.isdigit())
        anio = int(anio_txt) if len(anio_txt) == 4 else ANIO_POR_DEFECTO
        nro = datos.get("nro_expediente")
        dep = datos.get("id_dependencia")
        movimientos = await qellqa.consultar_estado_expediente(nro, dep, anio)
        sess["datos_recolectados"] = {}
        sess["estado_flujo"] = "MENU"
        if not movimientos:
            await _finish(sender, f"No encontré el expediente {nro} en esa dependencia para el año {anio}. Verifica los datos e inténtalo otra vez.", sess)
            return
        await _finish(sender, _seguimiento_a_texto(movimientos, nro, anio), sess)
        return

    # --- Estado MENU / otros: atajo numérico del menú o clasificación de intención ---

    eleccion = text.strip()
    # Un saludo (hola, buenas…) saluda y muestra el menú, y REINICIA el contador de
    # "no entendí". Así un saludo nunca deriva a una persona por malentendidos acumulados.
    if _es_saludo(eleccion):
        sess["contador_no_entendi"] = 0
        sess["estado_flujo"] = "MENU"
        await _finish(
            sender,
            await llm.generate_message("bienvenida") + "\n\n" + await llm.generate_message("menu"),
            sess,
        )
        return

    if estado == "MENU" and eleccion == "1":
        sess["estado_flujo"] = "MENU"
        await _finish(
            sender,
            "Claro. ¿De qué trámite quieres saber los requisitos? "
            "Por ejemplo: certificado de trabajo.",
            sess,
        )
        return

    if estado == "MENU" and eleccion in MENU_CHOICES:
        intent = MENU_CHOICES[eleccion]
    else:
        intent = await llm.classify_intent(text, sess)

    response_text = await route_intent(intent, text, sender, sess)
    await _finish(sender, response_text, sess)


async def route_intent(intent: str, text: str, sender: str, sess: dict) -> str:
    if intent == "CONSULTAR_REQUISITOS":
        return await flow_consultar_requisitos(text, sess)
    if intent == "INICIAR_TRAMITE":
        return await flow_iniciar_tramite(sender, sess)
    if intent == "CONSULTAR_ESTADO":
        return await flow_consultar_estado(sender, sess)
    if intent in ("PEDIR_MENU_AYUDA", "NO_RECONOCIDO"):
        return await flow_menu_o_no_entendio(sender, sess, intent)
    if intent == "HABLAR_CON_PERSONA":
        return await flow_derivar_persona(sess)
    if intent == "FUERA_DE_ALCANCE":
        sess["estado_flujo"] = "MENU"
        return await llm.generate_message("fuera_alcance")
    if intent == "CIERRE":
        sess["estado_flujo"] = "CERRADA"
        return await llm.generate_message("cierre")
    return await flow_menu_o_no_entendio(sender, sess, "NO_RECONOCIDO")


# ---------------------------------------------------------------------------
# Flujos
# ---------------------------------------------------------------------------

async def flow_consultar_requisitos(text: str, sess: dict) -> str:
    """Flujo 4 — requisitos de un trámite, anclado al RAG."""
    sess["contador_no_entendi"] = 0
    sess["estado_flujo"] = "MENU"

    chunks = await rag.search(text, n_results=5)
    if not chunks:
        return (
            "No encontré información sobre ese trámite en los documentos del "
            "Gobierno Regional de Cusco. Dime el nombre de otro trámite o di "
            '"opciones" para ver el menú.'
        )
    respuesta = await llm.generate_response("CONSULTAR_REQUISITOS", chunks[:1], {"ultimo_mensaje": text})
    return respuesta + "\n\n(Información referencial, de demostración.)"


async def flow_iniciar_tramite(sender: str, sess: dict) -> str:
    """Flujo 5 — identidad (registro real) → términos → recolección → emitir en QELLQA."""
    sess["contador_no_entendi"] = 0
    datos = sess.get("datos_recolectados") or {}
    datos["_accion"] = "iniciar_tramite"
    sess["datos_recolectados"] = datos

    user = await database.get_user(sender)
    if not user:
        # No tiene identidad validada: la validamos aquí mismo (nombre → DNI contra RENIEC).
        # El módulo facial sigue siendo exclusivo de la web; en WhatsApp basta DNI + nombre.
        sess["estado_flujo"] = "REGISTRO_NOMBRE"
        return _demo(
            "Para iniciar un trámite primero debo validar tu identidad. "
            "¿Cuál es tu nombre completo?",
            DEMO_HINT_DNI,
        )

    # Usuario ya validado: recuperamos sus datos y pasamos a términos.
    datos["nombre"] = user.get("nombre")
    datos["dni"] = user.get("dni")
    sess["estado_flujo"] = "TRAMITE_TERMINOS"
    return TERMINOS_TEXTO


async def _elegir_tipo_documento(text: str, iddependencia):
    """Encuentra el tipo de documento por nombre o abreviatura dentro de la dependencia."""
    tipos = await qellqa.get_tipo_documentos(iddependencia) if iddependencia else []
    t = (text or "").strip().lower()
    if not (tipos and t):
        return None
    for d in tipos:
        if t == (d.get("nombre") or "").lower() or t == (d.get("abreviatura") or "").lower():
            return d
    for d in tipos:
        if t in (d.get("nombre") or "").lower():
            return d
    return None


async def _emitir_tramite_real(sender: str, sess: dict) -> None:
    """Genera el PDF, lo sube, emite el trámite REAL en QELLQA y lo guarda en Supabase."""
    datos = sess.get("datos_recolectados") or {}
    dni = datos.get("dni") or ""
    persona = await qellqa.consultar_persona("DNI", dni) if dni else None
    if not persona:
        sess["estado_flujo"] = "MENU"
        await _finish(sender, "No pude confirmar tu identidad para enviar el trámite. Inténtalo desde el menú.", sess)
        return

    # Documento: el que envió el ciudadano (PDF) o uno generado por Ñawi (demo).
    if datos.get("adjunto_b64"):
        pdf_bytes = base64.b64decode(datos["adjunto_b64"])
        archivo_nombre = datos.get("adjunto_nombre") or "documento.pdf"
    else:
        pdf_bytes = pdf_generator.generar_solicitud_pdf({
            "nombre": persona.get("razonSocial") or persona.get("nombres"),
            "dni": dni, "celular": datos.get("celular"), "correo": datos.get("correo"),
            "tipo_documento": datos.get("tipodoc_nombre"), "dependencia": datos.get("dependencia_nombre"),
            "asunto": datos.get("asunto"), "nrofolios": datos.get("nrofolios"),
        })
        archivo_nombre = "solicitud_nawi.pdf"
    archivo = await qellqa.subir_archivo(pdf_bytes, archivo_nombre, int(datos.get("iddependencia")))
    if not archivo:
        sess["estado_flujo"] = "MENU"
        await _finish(sender, "Hubo un problema al subir tu documento. Inténtalo más tarde.", sess)
        return

    emitido = await qellqa.emitir_tramite({
        "tipoDocumentoPersona": "DNI", "iddependencia": int(datos.get("iddependencia")),
        "nroDocumentoPersona": dni, "nombres": persona.get("nombres"),
        "apellidoPaterno": persona.get("apellidoPaterno"), "apellidoMaterno": persona.get("apellidoMaterno"),
        "celular": datos.get("celular"), "correo": datos.get("correo"),
        "idtipodocumento": int(datos.get("idtipodocumento")), "nrodocumento": 0,
        "asunto": datos.get("asunto"), "nrofolios": datos.get("nrofolios"),
        "adjunto": archivo.get("url"), "idFiles": [archivo.get("idfile")] if archivo.get("idfile") else [],
        "idFilesAnexos": [], "idtupa": None, "linkAnexo": "",
    })
    if not emitido:
        sess["estado_flujo"] = "MENU"
        await _finish(sender, "No se pudo registrar el trámite en el sistema del GORE. Inténtalo más tarde.", sess)
        return

    idtramite = emitido.get("idtramite")
    try:
        user = await database.get_user(sender)
        if user:
            await database.create_expediente(
                user["id"], datos.get("asunto") or datos.get("tipodoc_nombre") or "Trámite",
                str(idtramite), estado="Recibido",
                id_dependencia=int(datos.get("iddependencia")), anio=datetime.now().year,
                idtramite=str(idtramite),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] expediente {idtramite} no guardado en Supabase: {type(exc).__name__}")

    sess["datos_recolectados"] = {}
    sess["estado_flujo"] = "TRAMITE_COMPLETADO"
    await _finish(
        sender,
        f"Tu trámite fue registrado con el número {idtramite} en el sistema del GORE Cusco. "
        "Te llegará un correo con los detalles.",
        sess,
    )


async def flow_consultar_estado(sender: str, sess: dict) -> str:
    """Flujo 7 — estado REAL del expediente (QELLQA): pide nº de expediente, dependencia y año."""
    sess["contador_no_entendi"] = 0
    datos = sess.get("datos_recolectados") or {}
    sess["datos_recolectados"] = datos
    sess["estado_flujo"] = "ESTADO_EXP"
    return _demo("Con gusto. Dime el número de tu expediente, solo los dígitos.", DEMO_HINT_EXP)


async def flow_menu_o_no_entendio(sender: str, sess: dict, intent: str) -> str:
    """Flujo 3 / US-11 — menú o manejo de no comprensión repetida."""
    if intent == "PEDIR_MENU_AYUDA":
        sess["contador_no_entendi"] = 0
        sess["estado_flujo"] = "MENU"
        return await llm.generate_message("menu")

    # NO_RECONOCIDO → contador de "no entendí".
    count = (sess.get("contador_no_entendi") or 0) + 1
    sess["contador_no_entendi"] = count
    sess["estado_flujo"] = "MENU"

    if count == 1:
        return await llm.generate_message("no_entendi_1")
    if count == 2:
        return await llm.generate_message("no_entendi_2")  # ya incluye el menú

    # 3ª vez seguida → derivar a una persona y resetear.
    sess["contador_no_entendi"] = 0
    return await flow_derivar_persona(sess)


async def flow_derivar_persona(sess: dict) -> str:
    """Flujo 9 — derivar a Mesa de Partes."""
    sess["estado_flujo"] = "CERRADA"
    return (
        "Puedo conectarte con la Mesa de Partes del Gobierno Regional de Cusco.\n"
        f"Teléfono: {MESA_DE_PARTES_TELEFONO}\n"
        "Horario: lunes a viernes de 8:00 a.m. a 4:30 p.m.\n"
        "Cuando quieras, vuelve a escribirme por aquí."
    )


# ---------------------------------------------------------------------------
# Salida unificada: audio + envío + persistencia de la sesión
# ---------------------------------------------------------------------------

async def _finish(sender: str, response_text: str, sess: dict) -> None:
    # Regla de Ñawi: texto + audio. Pero si el audio falla (p. ej. sin créditos de TTS),
    # enviamos AL MENOS el texto, para no dejar al ciudadano sin respuesta.
    try:
        audio_bytes = await tts.synthesize(response_text)
        await whatsapp.send_text_and_audio(sender, response_text, audio_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[Ñawi] audio no disponible ({type(exc).__name__}); envío solo texto.")
        try:
            await whatsapp.send_text(sender, response_text)
        except Exception as exc2:  # noqa: BLE001
            print(f"[Ñawi] el envío de texto también falló: {type(exc2).__name__}: {exc2}")
    await session.save_session(sender, sess)


# ---------------------------------------------------------------------------
# Endpoint de prueba para notificación proactiva (Flujo 8, MVP)
# ---------------------------------------------------------------------------

@router.post("/test/notify")
async def test_notify(request: Request):
    """Simula una notificación proactiva de cambio de estado."""
    body = await request.json()
    # Si no se pasa numero_whatsapp, se usa el destino de prueba del .env (cámbialo allí
    # por el número real cuando lo tengan).
    numero = body.get("numero_whatsapp") or os.getenv("WHATSAPP_TEST_RECIPIENT", "").strip()
    expediente_id = body.get("expediente_id")
    if not numero or not expediente_id:
        raise HTTPException(
            status_code=400,
            detail="expediente_id is required, and numero_whatsapp (or WHATSAPP_TEST_RECIPIENT in .env)",
        )

    expediente = await database.get_expediente_by_id(expediente_id)
    if not expediente:
        raise HTTPException(status_code=404, detail="Expediente not found")

    response_text = await llm.generate_response(
        "NOTIFICACION", [], {"expediente": expediente}
    )
    audio_bytes = await tts.synthesize(response_text)
    await whatsapp.send_text_and_audio(numero, response_text, audio_bytes)
    return {"status": "notification_sent"}
