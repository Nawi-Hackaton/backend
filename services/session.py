"""
Ñawi — Servicio de sesión (estado de cada conversación).

El estado de la conversación vive ÚNICAMENTE en Supabase (tabla "sesiones"), nunca en
variables globales ni en memoria del proceso. Cada número de WhatsApp tiene una sola
sesión activa (por eso sesiones.numero_whatsapp debe ser UNIQUE, para que el upsert
funcione con on_conflict).

Guarda: en qué paso del flujo está la conversación, los datos recolectados, el contador
de "no entendí" (US-11 / Flujo 3) y el historial reciente.

Reutiliza el cliente de Supabase de database.py (un solo cliente para todo el proyecto).

Prueba directa (requiere SUPABASE_URL/SUPABASE_KEY reales, el esquema creado y el
UNIQUE sobre sesiones.numero_whatsapp):
    python backend/services/session.py
"""

import asyncio
import sys
from pathlib import Path

# Permite ejecutar este archivo como script suelto (python backend/services/session.py)
# además de importarlo como módulo: añadimos la raíz del proyecto al sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Salida en UTF-8: las consolas de Windows usan cp1252 y romperían con acentos/emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Reutiliza el MISMO cliente de Supabase y los helpers de database.py.
from backend.services.database import _execute, _get_client, _now_iso  # noqa: E402
from backend.services import crypto  # noqa: E402

# Claves de datos_recolectados que son datos personales y se cifran en reposo (Ley 29733).
SENSITIVE_KEYS = {"dni", "nombre", "fullName", "celular", "correo", "correo_electronico"}


def _encrypt_datos(datos: dict) -> dict:
    if not isinstance(datos, dict):
        return datos
    return {k: (crypto.encrypt(v) if k in SENSITIVE_KEYS and v else v) for k, v in datos.items()}


def _decrypt_datos(datos: dict) -> dict:
    if not isinstance(datos, dict):
        return datos
    return {k: (crypto.decrypt(v) if k in SENSITIVE_KEYS and v else v) for k, v in datos.items()}


# Estados válidos del flujo de conversación.
VALID_STATES = {
    "INICIO",
    "MENU",
    "REGISTRO_NOMBRE",
    "REGISTRO_DNI",
    "RECOLECCION",
    "CONFIRMACION",
    "TRAMITE_COMPLETADO",
    "CERRADA",
}


def _default_session(numero_whatsapp: str) -> dict:
    """Sesión por defecto para un número sin sesión guardada (no se persiste aún)."""
    return {
        "numero_whatsapp": numero_whatsapp,
        "estado_flujo": "INICIO",
        "datos_recolectados": {},
        "contador_no_entendi": 0,
        "historial": [],
    }


async def get_session(numero_whatsapp: str) -> dict:
    """
    Devuelve la sesión del número. Si no existe, devuelve el dict por defecto
    SIN crearlo en la base de datos.
    """
    def _op():
        resp = (
            _get_client()
            .table("sesiones")
            .select("*")
            .eq("numero_whatsapp", numero_whatsapp)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    sesion = await _execute(f"get_session({numero_whatsapp})", _op)
    if not sesion:
        return _default_session(numero_whatsapp)
    if isinstance(sesion.get("datos_recolectados"), dict):
        sesion["datos_recolectados"] = _decrypt_datos(sesion["datos_recolectados"])
    return sesion


async def save_session(numero_whatsapp: str, datos: dict) -> dict:
    """
    Hace upsert de la sesión (clave de conflicto: numero_whatsapp).
    Devuelve el registro guardado.
    """
    def _op():
        fila = dict(datos or {})
        fila["numero_whatsapp"] = numero_whatsapp
        fila["actualizado_en"] = _now_iso()
        if isinstance(fila.get("datos_recolectados"), dict):
            fila["datos_recolectados"] = _encrypt_datos(fila["datos_recolectados"])
        resp = (
            _get_client()
            .table("sesiones")
            .upsert(fila, on_conflict="numero_whatsapp")
            .execute()
        )
        return resp.data[0]

    return await _execute(f"save_session({numero_whatsapp})", _op)


async def increment_no_entendi(numero_whatsapp: str) -> int:
    """Incrementa contador_no_entendi, guarda, y devuelve el nuevo valor."""
    sesion = await get_session(numero_whatsapp)
    nuevo_valor = (sesion.get("contador_no_entendi") or 0) + 1
    sesion["contador_no_entendi"] = nuevo_valor
    await save_session(numero_whatsapp, sesion)
    return nuevo_valor


async def reset_no_entendi(numero_whatsapp: str) -> None:
    """Resetea contador_no_entendi a 0 y guarda."""
    sesion = await get_session(numero_whatsapp)
    sesion["contador_no_entendi"] = 0
    await save_session(numero_whatsapp, sesion)


async def close_session(numero_whatsapp: str) -> None:
    """Marca la conversación como CERRADA."""
    sesion = await get_session(numero_whatsapp)
    sesion["estado_flujo"] = "CERRADA"
    await save_session(numero_whatsapp, sesion)


async def update_flow_state(numero_whatsapp: str, estado: str, datos: dict = None) -> None:
    """
    Actualiza estado_flujo y, opcionalmente, datos_recolectados.
    Lanza ValueError si `estado` no es uno de los VALID_STATES.
    """
    if estado not in VALID_STATES:
        raise ValueError(
            f"Estado de flujo inválido: '{estado}'. "
            f"Válidos: {', '.join(sorted(VALID_STATES))}."
        )

    sesion = await get_session(numero_whatsapp)
    sesion["estado_flujo"] = estado
    if datos is not None:
        sesion["datos_recolectados"] = datos
    await save_session(numero_whatsapp, sesion)


# ---------------------------------------------------------------------------
# Prueba directa
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    NUMERO_PRUEBA = "51988777666"

    async def _demo() -> None:
        print("1) get_session (nueva, debe ser el default INICIO):")
        sesion = await get_session(NUMERO_PRUEBA)
        print(f"   → {sesion}")

        print("2) save_session (persiste la sesión inicial):")
        guardada = await save_session(NUMERO_PRUEBA, sesion)
        print(f"   → guardada con id={guardada.get('id')}, estado={guardada.get('estado_flujo')}")

        print("3) increment_no_entendi x3:")
        for _ in range(3):
            valor = await increment_no_entendi(NUMERO_PRUEBA)
            print(f"   → contador_no_entendi = {valor}")

        print("4) reset_no_entendi:")
        await reset_no_entendi(NUMERO_PRUEBA)
        tras_reset = await get_session(NUMERO_PRUEBA)
        print(f"   → contador_no_entendi = {tras_reset.get('contador_no_entendi')}")

        print("5) close_session:")
        await close_session(NUMERO_PRUEBA)
        cerrada = await get_session(NUMERO_PRUEBA)
        print(f"   → estado_flujo = {cerrada.get('estado_flujo')}")

        print("6) Limpieza: borrando la sesión de prueba...")
        def _cleanup():
            _get_client().table("sesiones").delete().eq(
                "numero_whatsapp", NUMERO_PRUEBA
            ).execute()
        await _execute("cleanup_session", _cleanup)
        print("   Limpieza completa. La base queda sin datos de prueba.")

    try:
        asyncio.run(_demo())
    except Exception as exc:  # noqa: BLE001
        print(
            f"\n[ERROR] La prueba no se completó: {type(exc).__name__}: {exc}\n\n"
            "   Revisa que en .env estén SUPABASE_URL y SUPABASE_KEY, que hayas ejecutado\n"
            "   el esquema (docs/CONTEXT.md §6) y el UNIQUE sobre sesiones.numero_whatsapp:\n"
            "     ALTER TABLE sesiones\n"
            "       ADD CONSTRAINT sesiones_numero_whatsapp_key UNIQUE (numero_whatsapp);\n"
        )
        raise SystemExit(1)
