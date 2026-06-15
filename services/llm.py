"""
Ñawi — Servicio LLM (enrutamiento de intención + generación de respuestas).

Es el cerebro del Flujo 2 (núcleo del sistema). Tres funciones:

  - classify_intent(text, context)   → clasifica el mensaje en una intención (US-09).
  - generate_response(intent, chunks, context) → respuesta anclada a los chunks del RAG.
  - generate_message(tipo, datos)    → mensajes del sistema (plantillas deterministas).

Decisiones de diseño:
  - Modelo: gpt-4o-mini (equivalente OpenAI rápido/económico de "1.5 Flash").
    El prompt original pedía Gemini, pero el stack del proyecto es OpenAI.
  - generate_message NO usa el LLM: son plantillas fijas. Razones:
      1. El menú debe tener las 4 opciones exactas (US-10), sin parafraseo.
      2. La confirmación relee los datos antes de enviar al SGD; el principio no
         negociable exige que sea TEXTUAL y exacta, nunca una paráfrasis del LLM.
      3. Son instantáneas y no gastan API.

Ley 29733: generate_response solo razona sobre información PÚBLICA (chunks del RAG
y, para CONSULTAR_ESTADO, el expediente del propio usuario que ya está autenticado).
Los datos personales nunca se envían a la base vectorial.

Prueba directa:
    python backend/services/llm.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --- Configuración a nivel de módulo (se ejecuta UNA sola vez al importar) -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

from openai import AsyncOpenAI  # noqa: E402

logger = logging.getLogger("nawi.llm")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = "gpt-4o-mini"  # equivalente OpenAI rápido/económico de "1.5 Flash"

# Cliente async inicializado una sola vez (no por llamada).
_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Conjunto canónico de intenciones. Cualquier salida fuera de aquí → NO_RECONOCIDO.
VALID_INTENTS = {
    "CONSULTAR_REQUISITOS",
    "INICIAR_TRAMITE",
    "CONSULTAR_ESTADO",
    "PEDIR_MENU_AYUDA",
    "HABLAR_CON_PERSONA",
    "FUERA_DE_ALCANCE",
    "CIERRE",
    "NO_RECONOCIDO",
}


# ---------------------------------------------------------------------------
# FUNCIÓN 1 — Clasificación de intención
# ---------------------------------------------------------------------------

_INTENT_SYSTEM_PROMPT = """\
Eres el clasificador de intenciones de Ñawi, el asistente del Gobierno Regional de
Cusco (GORE Cusco). Tu única tarea es leer el mensaje del ciudadano y responder con
EXACTAMENTE UNA de estas etiquetas, en mayúsculas, sin ninguna otra palabra:

CONSULTAR_REQUISITOS, INICIAR_TRAMITE, CONSULTAR_ESTADO, PEDIR_MENU_AYUDA,
HABLAR_CON_PERSONA, FUERA_DE_ALCANCE, CIERRE, NO_RECONOCIDO

Significado y ejemplos:

- CONSULTAR_REQUISITOS: pregunta qué necesita, requisitos, costo o plazo de un trámite.
  Ej: "qué necesito para mi papel de trabajo", "cuánto cuesta el certificado",
      "qué documentos piden para una constancia".
- INICIAR_TRAMITE: quiere empezar/hacer/presentar un trámite ahora.
  Ej: "quiero iniciar mi certificado de trabajo", "ayúdame a hacer el trámite",
      "deseo presentar una solicitud".
- CONSULTAR_ESTADO: pregunta por el avance de un trámite ya iniciado.
  Ej: "cómo va mi trámite", "en qué estado está mi solicitud", "ya salió mi documento".
- PEDIR_MENU_AYUDA: pide el menú, opciones o ayuda general.
  Ej: "opciones", "menú", "ayuda", "qué puedes hacer".
- HABLAR_CON_PERSONA: quiere ser atendido por una persona/humano.
  Ej: "quiero hablar con una persona", "pásame con un agente", "necesito atención humana".
- FUERA_DE_ALCANCE: trámite o tema que NO corresponde al GORE Cusco (otra entidad,
  o algo ajeno a trámites del gobierno regional).
  Ej: "quiero renovar mi DNI", "cómo pago mi luz", "trámites de la municipalidad".
- CIERRE: agradece o se despide.
  Ej: "gracias", "listo", "adiós", "chau", "eso es todo".
- NO_RECONOCIDO: mensaje confuso, ambiguo o que no encaja en ninguna categoría.
  Ej: "tengo una duda sobre lo de mi papá", "hola qué tal", texto sin sentido.

Responde SOLO con la etiqueta. Nada más."""


async def classify_intent(text: str, context: dict) -> str:
    """
    Clasifica el mensaje del usuario en una de las VALID_INTENTS.

    Considera context["estado_flujo"]: si la conversación está en medio de un flujo
    (p. ej. RECOLECCION o CONFIRMACION), el mensaje suele ser un dato o una
    confirmación, no una nueva intención.

    Devuelve la etiqueta en mayúsculas; si el modelo responde algo fuera del set,
    devuelve "NO_RECONOCIDO".
    """
    context = context or {}
    estado_flujo = (context.get("estado_flujo") or "INICIO").upper()

    user_content = f'Mensaje del ciudadano: "{text}"'
    if estado_flujo not in ("", "INICIO"):
        user_content += (
            f"\n\nNota de contexto: la conversación está en el estado '{estado_flujo}'. "
            "Si el mensaje parece la respuesta a una pregunta de Ñawi (un dato como un "
            "nombre, un DNI, o un 'sí'/'no' de confirmación) y NO una petición nueva, "
            "clasifícalo como NO_RECONOCIDO para que el flujo en curso lo procese como dato."
        )

    response = await _client.chat.completions.create(
        model=MODEL,
        temperature=0,
        # 5 tokens truncaba etiquetas largas (INICIAR_TRAMITE, CONSULTAR_REQUISITOS) y caían
        # en NO_RECONOCIDO. 20 da margen para devolver la etiqueta completa.
        max_tokens=20,
        messages=[
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )

    raw = (response.choices[0].message.content or "").strip().upper()
    # Normaliza: nos quedamos con la primera "palabra" relevante.
    label = raw.split()[0].strip(".,:;\"'") if raw else ""
    return label if label in VALID_INTENTS else "NO_RECONOCIDO"


# ---------------------------------------------------------------------------
# FUNCIÓN 2 — Generación de respuesta anclada al RAG
# ---------------------------------------------------------------------------

_RESPONSE_SYSTEM_PROMPT = """\
Eres Ñawi, el asistente del Gobierno Regional de Cusco (GORE Cusco) para personas
con discapacidad visual.

Reglas que SIEMPRE debes cumplir:
1. Responde ÚNICAMENTE con la información que aparece en el CONTEXTO que se te da.
2. Si el contexto no contiene la información para responder, dilo claramente
   (por ejemplo: "No tengo esa información ahora mismo") y ofrece mostrar las
   opciones o derivar a una persona. NO intentes adivinar.
3. Nunca inventes requisitos, plazos, costos ni nombres de oficinas.
4. Usa lenguaje simple y cálido, máximo 3 o 4 oraciones, pensado para ESCUCHARSE en
   audio (frases cortas, sin viñetas complejas ni símbolos raros).
5. No uses jerga ni siglas técnicas: nunca digas "TUPA", "SGD V3" ni "expediente
   técnico". Habla de "trámite", "documento" o "solicitud".
6. Trata al ciudadano de "tú", con respeto y claridad."""


def _format_chunks(chunks: list) -> str:
    """Arma el bloque de CONTEXTO a partir de los chunks del RAG."""
    if not chunks:
        return ""
    partes = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            partes.append(chunk.get("text", ""))
        else:
            partes.append(str(chunk))
    return "\n\n---\n\n".join(p for p in partes if p)


async def generate_response(intent: str, chunks: list, context: dict) -> str:
    """
    Genera la respuesta de Ñawi según la intención y los chunks del RAG.

    - CONSULTAR_REQUISITOS: lista numerada de requisitos, tomada solo de los chunks.
    - CONSULTAR_ESTADO: usa context["expediente"] (datos del propio usuario).
    - NOTIFICACION: aviso proactivo breve de cambio de estado.
    - otros: respuesta genérica útil basada en el contexto disponible.
    """
    context = context or {}
    contexto_rag = _format_chunks(chunks)

    if intent == "CONSULTAR_REQUISITOS":
        if not contexto_rag:
            return (
                "No tengo la información de ese trámite ahora mismo. "
                "¿Quieres que te muestre las opciones o te comunique con una persona del "
                "Gobierno Regional de Cusco?"
            )
        instruccion = (
            "El ciudadano pregunta por los requisitos de un trámite. Usando SOLO el "
            "CONTEXTO, dale los requisitos como una lista numerada, breve y clara para "
            "escuchar. Si el contexto no incluye algún dato (costo o plazo), no lo inventes."
        )
        user_content = f"{instruccion}\n\nCONTEXTO:\n{contexto_rag}"

    elif intent == "CONSULTAR_ESTADO":
        expediente = context.get("expediente") or {}
        instruccion = (
            "El ciudadano pregunta cómo va su trámite. Con los DATOS DEL TRÁMITE de abajo, "
            "explícale en lenguaje simple en qué estado está, en qué oficina y el próximo "
            "paso o plazo si aparece. No inventes datos que no estén."
        )
        user_content = f"{instruccion}\n\nDATOS DEL TRÁMITE:\n{expediente}"

    elif intent == "NOTIFICACION":
        expediente = context.get("expediente") or {}
        instruccion = (
            "Genera un aviso proactivo breve para el ciudadano informándole que su trámite "
            "cambió de estado. Usa los DATOS DEL TRÁMITE de abajo. Tono cálido, 2 o 3 "
            "oraciones, pensado para audio. No inventes datos."
        )
        user_content = f"{instruccion}\n\nDATOS DEL TRÁMITE:\n{expediente}"

    else:
        base = (
            "Responde de forma útil y breve a la consulta del ciudadano usando el CONTEXTO "
            "si lo hay. Si no hay información suficiente, dilo y ofrece las opciones."
        )
        consulta = context.get("ultimo_mensaje", "")
        user_content = base
        if consulta:
            user_content += f'\n\nCONSULTA: "{consulta}"'
        if contexto_rag:
            user_content += f"\n\nCONTEXTO:\n{contexto_rag}"

    response = await _client.chat.completions.create(
        model=MODEL,
        temperature=0.3,
        max_tokens=300,
        messages=[
            {"role": "system", "content": _RESPONSE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# FUNCIÓN 3 — Mensajes del sistema (plantillas deterministas, sin LLM)
# ---------------------------------------------------------------------------

_MENU = (
    "Estas son las opciones:\n"
    "1. Consultar qué necesito para un trámite.\n"
    "2. Iniciar un trámite.\n"
    "3. Ver el estado de mi trámite.\n"
    "4. Hablar con una persona.\n"
    "Dime el número o cuéntame con tus palabras qué necesitas."
)


def _confirmacion(datos: dict) -> str:
    datos = datos or {}
    if not datos:
        return (
            "Antes de enviar, necesito confirmar tus datos, pero todavía no tengo ninguno "
            "registrado. ¿Empezamos de nuevo?"
        )
    lineas = "\n".join(f"- {clave}: {valor}" for clave, valor in datos.items())
    return (
        "Antes de enviar, voy a leerte los datos para confirmar:\n"
        f"{lineas}\n"
        "¿Está todo correcto? Responde sí o no."
    )


async def generate_message(tipo: str, datos: dict = None) -> str:
    """
    Devuelve un mensaje del sistema a partir de plantillas fijas (no usa el LLM).
    `datos` se usa en 'confirmacion' y 'registro_exitoso'.
    """
    datos = datos or {}

    if tipo == "bienvenida":
        return (
            "Hola, soy Ñawi, tu asistente del Gobierno Regional de Cusco.\n"
            "Puedo decirte qué necesitas para un trámite, ayudarte a iniciar uno o contarte "
            "cómo va el tuyo.\n"
            'Háblame con tus propias palabras o di "opciones" para escuchar el menú.'
        )

    if tipo == "menu":
        return _MENU

    if tipo == "confirmacion":
        return _confirmacion(datos)

    if tipo == "registro_solicitud":
        return (
            "Para continuar necesito registrarte y así guardar tus trámites de forma segura. "
            "¿Cuál es tu nombre completo?"
        )

    if tipo == "registro_dni":
        return "Gracias. Ahora dime tu número de DNI, los ocho dígitos."

    if tipo == "registro_exitoso":
        nombre = datos.get("nombre", "").strip()
        saludo = f"Listo, {nombre}. " if nombre else "Listo. "
        return (
            f"{saludo}Quedaste registrado y tus datos se guardan para tus próximas "
            "consultas. ¿En qué te ayudo?"
        )

    if tipo == "cierre":
        return "Con gusto. Estoy aquí cuando me necesites. ¡Que te vaya bien!"

    if tipo == "no_entendi_1":
        return "Disculpa, no te entendí bien. ¿Puedes explicármelo de otra forma?"

    if tipo == "no_entendi_2":
        return "Sigo sin entenderte. Te muestro las opciones para ayudarte mejor.\n" + _MENU

    if tipo == "fuera_alcance":
        return (
            "Eso no corresponde a los trámites del Gobierno Regional de Cusco, así que no "
            "puedo ayudarte con ese tema. Si quieres, puedo ayudarte con un trámite del GORE."
        )

    # Tipo desconocido → fallback neutro.
    logger.warning("generate_message: tipo desconocido '%s'", tipo)
    return "¿En qué puedo ayudarte?"


# ---------------------------------------------------------------------------
# Prueba directa
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pruebas = [
        ("quiero saber qué necesito para sacar mi papel de trabajo", "CONSULTAR_REQUISITOS"),
        ("ayúdame a iniciar mi certificado de trabajo", "INICIAR_TRAMITE"),
        ("cómo va mi trámite", "CONSULTAR_ESTADO"),
        ("opciones", "PEDIR_MENU_AYUDA"),
        ("muchas gracias, adiós", "CIERRE"),
    ]

    async def _run() -> None:
        print("Prueba de classify_intent:\n")
        for mensaje, esperado in pruebas:
            intent = await classify_intent(mensaje, {"estado_flujo": "INICIO"})
            marca = "[OK]" if intent == esperado else "[FALLO]"
            print(f"{marca} {mensaje!r}\n   esperado={esperado}  obtenido={intent}\n")

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(
            f"[ERROR] No se pudo ejecutar la prueba:\n"
            f"   {type(exc).__name__}: {exc}\n\n"
            "   Causa común: OPENAI_API_KEY ausente o inválida en .env.\n"
            "   Pega una clave válida de platform.openai.com en .env y reintenta.\n"
        )
        raise SystemExit(1)
