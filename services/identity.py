"""
Ñawi — Servicio de validación de identidad (un solo motor para los 3 canales).

Implementa el flujo de identidad de US-07 (registro e identidad): validación de DNI,
código OTP de un solo uso y registro verificado. El mismo flujo sirve para WhatsApp,
web e IVR (llamada) — ver la nota de la PARTE 3 al final.

Ley 29733: el DNI, la fecha de nacimiento y el nombre NUNCA se envían al LLM ni se
vectorizan en el RAG. Solo se guardan en Supabase tras una validación exitosa.

Prueba directa (la parte offline, sin Supabase):
    python backend/services/identity.py
"""

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Permite ejecutar como script suelto además de importarlo como módulo.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from backend.services import database, qellqa, tts, whatsapp  # noqa: E402

OTP_TABLE = "otp_sessions"
OTP_TTL_MINUTES = 5


# ---------------------------------------------------------------------------
# PASO 1 — Validación de DNI
# ---------------------------------------------------------------------------

async def validate_dni_reniec(dni: str, nombre_ingresado: str) -> dict | None:
    """
    Valida la identidad del ciudadano contra RENIEC vía QELLQA (datos reales).

    1. Consulta el DNI en QELLQA (que internamente llama a RENIEC).
    2. Si no existe → None (DNI no encontrado).
    3. Si existe → compara el primer nombre ingresado con los nombres/apellidos reales
       (sin distinguir mayúsculas/acentos básicos). Si coincide, devuelve los datos
       completos de la persona; si no, None.

    No hardcodea "8 dígitos": la propia API valida el formato. No se loguea PII (Ley 29733).
    """
    dni = (dni or "").strip()
    if not dni:
        return None

    datos = await qellqa.consultar_persona("DNI", dni)
    if not datos:
        return None

    # Conjunto de tokens reales (nombres + apellidos + razón social).
    reales = " ".join(
        str(datos.get(k) or "")
        for k in ("nombres", "apellidoPaterno", "apellidoMaterno", "razonSocial")
    ).upper()
    reales_tokens = set(reales.split())

    ingresado = (nombre_ingresado or "").strip().upper()
    if not ingresado:
        # Sin nombre que comparar: el DNI existe, devolvemos los datos.
        return datos

    primer_token = ingresado.split()[0]
    # Coincidencia flexible: el primer nombre ingresado aparece entre los tokens reales,
    # o como subcadena del bloque real (cubre nombres compuestos).
    if primer_token in reales_tokens or primer_token in reales:
        return datos
    return None


# ---------------------------------------------------------------------------
# PASO 2 — Generar OTP
# ---------------------------------------------------------------------------

async def generate_otp(numero_whatsapp: str) -> str:
    """Genera un OTP de 4 dígitos, lo guarda en otp_sessions y lo devuelve."""
    codigo = f"{random.randint(0, 9999):04d}"
    ahora = datetime.now(timezone.utc)
    expira = ahora + timedelta(minutes=OTP_TTL_MINUTES)

    def _op():
        database._get_client().table(OTP_TABLE).insert(
            {
                "numero_whatsapp": numero_whatsapp,
                "codigo": codigo,
                "creado_en": ahora.isoformat(),
                "expira_en": expira.isoformat(),
                "usado": False,
            }
        ).execute()

    await database._execute(f"generate_otp({numero_whatsapp})", _op)
    return codigo


# ---------------------------------------------------------------------------
# PASO 3 — Enviar OTP por el canal correspondiente
# ---------------------------------------------------------------------------

async def send_otp(canal: str, destino: str, codigo: str) -> bool:
    """
    Entrega el OTP según el canal.

    - "whatsapp": envía el código por texto + un audio (ElevenLabs) que dice los dígitos
      uno por uno (accesible para personas con discapacidad visual).
    - "web" / "ivr": no se envía nada por mensaje; el propio canal ya tiene el código
      (lo recibió de generate_otp) y lo muestra (widget) o lo lee en la llamada (IVR).
    """
    canal = (canal or "").lower()

    if canal == "whatsapp":
        await whatsapp.send_text(
            destino,
            f"Tu código de verificación es: {codigo}. Válido por {OTP_TTL_MINUTES} minutos.",
        )
        digitos = ", ".join(codigo)  # "1, 2, 3, 4" → se escucha dígito por dígito
        audio = await tts.synthesize(
            f"Tu código de verificación es: {digitos}. "
            f"Válido por {OTP_TTL_MINUTES} minutos."
        )
        await whatsapp.send_audio(destino, audio)
        return True

    if canal in ("web", "ivr"):
        # El canal ya conoce el código (de generate_otp). No se envía mensaje.
        return True

    return False


# ---------------------------------------------------------------------------
# PASO 4 — Verificar OTP
# ---------------------------------------------------------------------------

def _parse_dt(valor: str):
    """Parsea un timestamp ISO de Supabase a datetime con zona horaria (UTC por defecto)."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def verify_otp(numero_whatsapp: str, codigo_ingresado: str) -> bool:
    """
    Verifica el OTP: existe, coincide, no está usado y no expiró (< 5 min).
    Si todo coincide, lo marca como usado y devuelve True. Si no, False.
    """
    def _buscar():
        resp = (
            database._get_client()
            .table(OTP_TABLE)
            .select("*")
            .eq("numero_whatsapp", numero_whatsapp)
            .eq("codigo", codigo_ingresado)
            .eq("usado", False)
            .order("creado_en", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    registro = await database._execute(f"verify_otp({numero_whatsapp})", _buscar)
    if not registro:
        return False

    expira = _parse_dt(registro.get("expira_en"))
    if expira is None or datetime.now(timezone.utc) > expira:
        return False

    def _marcar():
        database._get_client().table(OTP_TABLE).update({"usado": True}).eq(
            "id", registro["id"]
        ).execute()

    await database._execute(f"verify_otp.mark({numero_whatsapp})", _marcar)
    return True


# ---------------------------------------------------------------------------
# PASO 5 — Completar el registro
# ---------------------------------------------------------------------------

async def complete_registration(numero_whatsapp: str, nombre: str, dni: str) -> dict:
    """Crea el usuario verificado, limpia sus OTP y devuelve el usuario creado."""
    user = await database.create_user(nombre, dni, numero_whatsapp, verified=True)

    def _borrar():
        database._get_client().table(OTP_TABLE).delete().eq(
            "numero_whatsapp", numero_whatsapp
        ).execute()

    await database._execute(f"complete_registration.cleanup({numero_whatsapp})", _borrar)
    return user


# ===========================================================================
# PARTE 3 — Integración con el flujo de llamada IVR (documentación, no implementado)
# ===========================================================================
# El MISMO flujo de validación de identidad funciona en una llamada telefónica. No se
# implementa ahora; se documenta cómo encajaría, reutilizando ESTE módulo sin cambios:
#
#   1. El usuario llama al número de Ñawi.
#   2. IVR: "Bienvenido. Para verificar tu identidad, di tu número de DNI después del tono."
#   3. El usuario dice el DNI en voz alta → Whisper (stt.transcribe) lo transcribe a texto.
#   4. IVR: "Ahora di tu fecha de nacimiento." → Whisper transcribe.
#   5. validate_dni_reniec(dni, fecha)  (en producción, contra RENIEC).
#   6. generate_otp(numero) y send_otp("ivr", numero, codigo): el IVR LEE el código dígito
#      por dígito en la llamada (TTS); no se envía nada por mensaje.
#   7. El usuario teclea el código en el teclado del teléfono (DTMF).
#   8. verify_otp(numero, codigo_tecleado) → identidad confirmada.
#
# Tecnología prevista: Twilio Voice o Google Dialogflow CX (ver docs/TECNOLOGIAS.md,
# roadmap). El núcleo (validar, OTP, verificar) es este módulo: un solo motor para todos
# los canales (WhatsApp, web, IVR).
# ===========================================================================


if __name__ == "__main__":
    import asyncio

    async def _run() -> None:
        print("Prueba de validate_dni_reniec contra QELLQA/RENIEC (requiere internet):\n")
        # DNI real de prueba + nombre que sí coincide.
        ok = await validate_dni_reniec("76601704", "Marsi")
        print("  DNI 76601704 + 'Marsi' →", "OK (coincide)" if ok else "no validado")
        # Mismo DNI con un nombre que no coincide.
        no = await validate_dni_reniec("76601704", "Juan")
        print("  DNI 76601704 + 'Juan'  →", "OK" if no else "no validado (esperado)")
        print(
            "\n(generate_otp / verify_otp / complete_registration requieren Supabase y se "
            "prueban con el backend levantado.)"
        )

    asyncio.run(_run())
