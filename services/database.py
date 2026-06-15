"""
Ñawi — Servicio de base de datos (Supabase).

Guarda el *estado* de Ñawi: usuarios registrados y sus expedientes. Es distinto a
ChromaDB, que guarda el *conocimiento* (documentos públicos del GORE). Aquí SÍ viven
datos personales (nombre, DNI, número de WhatsApp), por eso nunca se vectorizan ni se
mandan al RAG (Ley 29733).

Funciones async usadas por los flujos:
  - Usuarios:     get_user, create_user                       (US-07 registro)
  - Expedientes:  get_expedientes, get_expediente_by_id,
                  create_expediente, update_estado            (US-03 consultar estado)

Notas de implementación:
  - supabase-py 2.4.3 es SÍNCRONO; envolvemos cada operación en asyncio.to_thread
    para no bloquear el event loop de FastAPI (mismo patrón que rag.py).
  - El cliente se inicializa UNA sola vez de forma perezosa (_get_client). Así, si
    faltan credenciales, damos un mensaje claro en vez de romper el import.

Prueba directa (requiere SUPABASE_URL/SUPABASE_KEY reales y las tablas creadas):
    python backend/services/database.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
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

logger = logging.getLogger("nawi.database")

# El cliente se crea perezosamente para no fallar el import si faltan credenciales.
_client = None


def _now_iso() -> str:
    """Timestamp actual en ISO 8601 con zona horaria (para columnas TIMESTAMPTZ)."""
    return datetime.now(timezone.utc).isoformat()


def _get_client():
    """Devuelve el cliente de Supabase, creándolo una sola vez."""
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError(
                "Faltan SUPABASE_URL y/o SUPABASE_KEY en el archivo .env. "
                "Crea el proyecto en supabase.com, ve a Settings > API, y pega "
                "'Project URL' en SUPABASE_URL y 'anon public key' en SUPABASE_KEY."
            )
        from supabase import create_client

        _client = create_client(url, key)
    return _client


async def _execute(descripcion: str, op):
    """Corre una operación síncrona de Supabase en un hilo, con manejo de errores claro."""
    try:
        return await asyncio.to_thread(op)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Supabase ({descripcion}): {type(exc).__name__}: {exc}")
        raise


# ---------------------------------------------------------------------------
# USUARIOS
# ---------------------------------------------------------------------------

def _decrypt_user(user: dict | None) -> dict | None:
    """Descifra los campos personales de un usuario (nombre, dni) para uso interno."""
    if not user:
        return user
    from backend.services import crypto

    if "nombre" in user:
        user["nombre"] = crypto.decrypt(user.get("nombre"))
    if "dni" in user:
        user["dni"] = crypto.decrypt(user.get("dni"))
    return user


async def get_user(numero_whatsapp: str) -> dict | None:
    """Busca un usuario por su número de WhatsApp. Devuelve el registro (descifrado) o None."""
    def _op():
        resp = (
            _get_client()
            .table("usuarios")
            .select("*")
            .eq("numero_whatsapp", numero_whatsapp)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    user = await _execute(f"get_user({numero_whatsapp})", _op)
    return _decrypt_user(user)


async def create_user(
    nombre: str, dni: str, numero_whatsapp: str, verified: bool = False
) -> dict:
    """Inserta un nuevo usuario y devuelve el registro creado.

    `verified` lo usa identity.complete_registration para crear usuarios ya verificados
    por OTP. Requiere que la tabla usuarios tenga la columna `verified` (ver esquema).
    """
    from backend.services import crypto

    def _op():
        resp = (
            _get_client()
            .table("usuarios")
            .insert(
                {
                    # Datos personales cifrados en reposo (Ley 29733).
                    "nombre": crypto.encrypt(nombre),
                    "dni": crypto.encrypt(dni),
                    "numero_whatsapp": numero_whatsapp,
                    "verified": verified,
                }
            )
            .execute()
        )
        return resp.data[0]

    user = await _execute(f"create_user({numero_whatsapp})", _op)
    return _decrypt_user(user)


# ---------------------------------------------------------------------------
# EXPEDIENTES
# ---------------------------------------------------------------------------

async def get_expedientes(usuario_id: str) -> list[dict]:
    """Devuelve todos los expedientes de un usuario, ordenados por fecha_ingreso desc."""
    def _op():
        resp = (
            _get_client()
            .table("expedientes")
            .select("*")
            .eq("usuario_id", usuario_id)
            .order("fecha_ingreso", desc=True)
            .execute()
        )
        return resp.data or []

    return await _execute(f"get_expedientes({usuario_id})", _op)


async def get_expediente_by_id(expediente_id: str) -> dict | None:
    """Busca un expediente por su id UUID. Devuelve el registro o None."""
    def _op():
        resp = (
            _get_client()
            .table("expedientes")
            .select("*")
            .eq("id", expediente_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    return await _execute(f"get_expediente_by_id({expediente_id})", _op)


async def create_expediente(
    usuario_id: str,
    nombre_tramite: str,
    numero_expediente: str,
    estado: str = "En revisión",
    oficina_actual: str = "Mesa de Partes",
    id_dependencia: int = None,
    anio: int = None,
    idtramite: str = None,
) -> dict:
    """
    Inserta un nuevo expediente y devuelve el registro creado.

    id_dependencia/anio/idtramite vienen del trámite real de QELLQA y permiten luego
    consultar el seguimiento (nº expediente + dependencia + año). Solo se incluyen si se
    proveen (requieren las columnas correspondientes en Supabase).
    """
    def _op():
        ahora = _now_iso()
        fila = {
            "usuario_id": usuario_id,
            "numero_expediente": numero_expediente,
            "nombre_tramite": nombre_tramite,
            "estado": estado,
            "oficina_actual": oficina_actual,
            "fecha_ingreso": ahora,            # el esquema no le da default
            "fecha_ultimo_cambio": ahora,
        }
        if id_dependencia is not None:
            fila["id_dependencia"] = id_dependencia
        if anio is not None:
            fila["anio"] = anio
        if idtramite is not None:
            fila["idtramite"] = str(idtramite)
        resp = _get_client().table("expedientes").insert(fila).execute()
        return resp.data[0]

    return await _execute(f"create_expediente({numero_expediente})", _op)


async def update_estado(expediente_id: str, nuevo_estado: str, oficina: str = None) -> dict:
    """Actualiza el estado (y opcionalmente la oficina) de un expediente."""
    def _op():
        cambios = {
            "estado": nuevo_estado,
            "fecha_ultimo_cambio": _now_iso(),
        }
        if oficina is not None:
            cambios["oficina_actual"] = oficina

        resp = (
            _get_client()
            .table("expedientes")
            .update(cambios)
            .eq("id", expediente_id)
            .execute()
        )
        return resp.data[0]

    return await _execute(f"update_estado({expediente_id})", _op)


# ---------------------------------------------------------------------------
# Prueba directa
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    NUMERO_PRUEBA = "51999000111"

    async def _demo() -> None:
        # Pre-limpieza: si una corrida anterior dejó este usuario, lo quitamos primero
        # (junto con sus expedientes, porque el esquema no tiene ON DELETE CASCADE).
        previo = await get_user(NUMERO_PRUEBA)
        if previo:
            print("[INFO] Encontrado usuario de prueba previo; limpiando antes de empezar...")
            await _borrar_usuario_y_expedientes(previo["id"])

        print("1) Creando usuario de prueba...")
        user = await create_user("Usuario Prueba", "00000000", NUMERO_PRUEBA)
        print(f"   → {user}")
        usuario_id = user["id"]

        print("2) Creando expediente para ese usuario...")
        exp = await create_expediente(
            usuario_id=usuario_id,
            nombre_tramite="Certificado de trabajo",
            numero_expediente="EXP-TEST-001",
        )
        print(f"   → {exp}")

        print("3) Consultando los expedientes del usuario...")
        expedientes = await get_expedientes(usuario_id)
        print(f"   → {len(expedientes)} expediente(s): {expedientes}")

        print("4) Limpieza final: borrando expediente(s) y usuario de prueba...")
        await _borrar_usuario_y_expedientes(usuario_id)
        print("   Limpieza completa. La base queda sin datos de prueba.")

    async def _borrar_usuario_y_expedientes(usuario_id: str) -> None:
        """Borra primero los expedientes (FK) y luego el usuario."""
        def _op():
            client = _get_client()
            client.table("expedientes").delete().eq("usuario_id", usuario_id).execute()
            client.table("usuarios").delete().eq("id", usuario_id).execute()

        await _execute(f"borrar_usuario({usuario_id})", _op)

    try:
        asyncio.run(_demo())
    except Exception as exc:  # noqa: BLE001
        print(
            f"\n[ERROR] La prueba no se completó: {type(exc).__name__}: {exc}\n\n"
            "   Revisa que en .env estén SUPABASE_URL y SUPABASE_KEY, y que ya hayas "
            "ejecutado el SQL del esquema (docs/CONTEXT.md sección 6) en el SQL Editor "
            "de Supabase.\n"
        )
        raise SystemExit(1)
