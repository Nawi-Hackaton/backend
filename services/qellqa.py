"""
Ñawi — Cliente de QELLQA (Mesa de Partes Virtual del GORE Cusco).

Encapsula las APIs públicas reales de QELLQA (https://qellqa.regioncusco.gob.pe). No
requieren autenticación. Este servicio es la fuente de verdad de:
  - dependencias y tipos de documento reales,
  - validación de identidad real (consulta a RENIEC por DNI/RUC),
  - estado real de un expediente (historial de movimientos),
  - alta real de un trámite (subir PDF + emitir documento).

Ley 29733: la consulta de persona devuelve datos REALES de RENIEC. Este módulo NUNCA
loguea esos datos (ni nombres, ni DNI). Quien persista el resultado debe cifrarlo
(ver crypto.py).

Manejo de errores: si QELLQA falla (timeout, 5xx, body inesperado) se loguea SIN datos
personales y se devuelve None / []. El que llama debe tener un fallback.

Prueba directa (solo lectura, segura):
    python backend/services/qellqa.py
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.qellqa")

BASE_URL = os.getenv("QELLQA_BASE_URL", "https://qellqa.regioncusco.gob.pe").strip().rstrip("/")
_TIMEOUT = 30.0

# Identidad FICTICIA para la demo: este DNI no consulta RENIEC, devuelve una persona inventada.
# Permite probar el flujo de validación sin usar datos reales de una persona. Apagar con
# DEMO_MODE=false en .env.
DEMO_MODE = os.getenv("DEMO_MODE", "true").strip().lower() in ("1", "true", "yes", "si", "sí")
DEMO_DNI = "12345678"
DEMO_PERSONA = {
    "tipoDocumento": "DNI", "nroDocumento": DEMO_DNI, "razonSocial": "MARIA QUISPE MAMANI",
    "nombres": "MARIA", "apellidoPaterno": "QUISPE", "apellidoMaterno": "MAMANI",
}
_HEADERS = {
    "User-Agent": "Nawi-GORE-Cusco/1.0 (asistente accesible)",
    "Accept": "application/json",
}

# Rutas (confirmadas en vivo).
_R_DEPENDENCIAS = "/api/virtual/mesa-partes/dependencias/listar"
_R_TIPO_DOCS = "/api/virtual/mesa-partes/tipo-documentos/listar"
_R_PERSONA = "/api/virtual/mesa-partes/persona/consultar"
_R_SEGUIMIENTO = "/api-sgd/general/publico/mesa-partes/seguimiento/externo"
_R_SUBIR = "/api/virtual/mesa-partes/archivo/subir"
_R_EMITIR = "/api/virtual/mesa-partes/documento/emitir"

# Caché en memoria de dependencias (no cambian; TTL 1 hora).
_dep_cache = {"data": None, "ts": 0.0}
_DEP_TTL = 3600.0


def _client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL, timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True, **kwargs
    )


# ---------------------------------------------------------------------------
# GET — lectura
# ---------------------------------------------------------------------------

async def get_dependencias() -> list[dict]:
    """Lista de dependencias del GORE Cusco: [{iddependencia, nombre}]. Cacheada 1 h."""
    now = time.monotonic()
    if _dep_cache["data"] is not None and (now - _dep_cache["ts"]) < _DEP_TTL:
        return _dep_cache["data"]
    try:
        async with _client() as c:
            r = await c.get(_R_DEPENDENCIAS)
            r.raise_for_status()
            data = r.json()
        deps = data if isinstance(data, list) else (data.get("data") or [])
        _dep_cache["data"] = deps
        _dep_cache["ts"] = now
        return deps
    except Exception as exc:  # noqa: BLE001
        logger.error("get_dependencias falló: %s: %s", type(exc).__name__, str(exc)[:160])
        # Si hay caché vieja, mejor devolverla que nada.
        return _dep_cache["data"] or []


async def get_tipo_documentos(iddependencia: int) -> list[dict]:
    """Tipos de documento por dependencia: [{idtipodocumento, nombre, abreviatura}]."""
    try:
        async with _client() as c:
            r = await c.get(_R_TIPO_DOCS, params={"iddependencia": iddependencia})
            r.raise_for_status()
            data = r.json()
        return data if isinstance(data, list) else (data.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        logger.error("get_tipo_documentos(%s) falló: %s", iddependencia, type(exc).__name__)
        return []


async def consultar_persona(tipo_doc: str, nro_doc: str) -> dict | None:
    """
    Consulta datos reales de una persona (RENIEC) por documento.

    tipo_doc: "DNI" o "RUC".  Devuelve el dict de `data`
    ({nombres, apellidoPaterno, apellidoMaterno, razonSocial, ...}) o None si no existe.
    NO loguea los datos devueltos (Ley 29733).
    """
    tipo_doc = (tipo_doc or "DNI").strip().upper()
    nro_doc = (nro_doc or "").strip()
    if not nro_doc:
        return None
    # Demo: DNI ficticio → persona inventada, sin consultar RENIEC.
    if DEMO_MODE and tipo_doc == "DNI" and nro_doc == DEMO_DNI:
        return dict(DEMO_PERSONA)
    try:
        async with _client() as c:
            r = await c.get(_R_PERSONA, params={"tipoDocumento": tipo_doc, "nroDocumento": nro_doc})
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("consultar_persona falló (%s): %s", tipo_doc, type(exc).__name__)
        return None

    data = body.get("data") if isinstance(body, dict) else None
    if not data:
        return None
    # Sin datos útiles (ni nombres ni razón social) → tratar como no encontrado.
    if not (data.get("nombres") or data.get("razonSocial")):
        return None
    return data


async def consultar_estado_expediente(
    nro_expediente: int, id_dependencia: int, anio: int
) -> list[dict] | None:
    """
    Historial real de movimientos de un expediente. Devuelve la lista (ordenada por la
    API) o None si no se encontró. Cada movimiento trae: accion, fecha, origen, destino,
    estadorecepcion, asunto, tipodocumento, nrofolios, nota, idtramite, ...
    """
    try:
        async with _client() as c:
            r = await c.get(
                _R_SEGUIMIENTO,
                params={"nroExpediente": nro_expediente, "idDependencia": id_dependencia, "anio": anio},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("consultar_estado_expediente falló: %s", type(exc).__name__)
        return None

    movimientos = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
    if not movimientos:
        return None
    return movimientos


# ---------------------------------------------------------------------------
# POST — escritura (crea registros REALES en el GORE)
# ---------------------------------------------------------------------------

async def subir_archivo(archivo_bytes: bytes, nombre_archivo: str, id_dependencia: int) -> dict | None:
    """
    Sube un PDF a QELLQA y devuelve {idfile, url, nombre} (o None si falla).

    La API responde una lista de archivos; tomamos el primero.
    """
    try:
        # QELLQA espera el archivo en el campo array "archivos[]" e iddependencia entero.
        files = [("archivos[]", (nombre_archivo, archivo_bytes, "application/pdf"))]
        data = {"iddependencia": str(id_dependencia)}
        async with _client() as c:
            r = await c.post(_R_SUBIR, files=files, data=data)
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("subir_archivo falló: %s: %s", type(exc).__name__, str(exc)[:160])
        return None

    item = None
    if isinstance(body, list) and body:
        item = body[0]
    elif isinstance(body, dict):
        item = (body.get("data") or [body])[0] if isinstance(body.get("data"), list) else body
    if not item:
        return None
    return {
        "idfile": item.get("idfile") or item.get("id"),
        "url": item.get("url") or item.get("ruta"),
        "nombre": item.get("nombre") or nombre_archivo,
    }


async def emitir_tramite(datos: dict) -> dict | None:
    """
    Registra un trámite real en QELLQA (POST /documento/emitir).

    `datos` debe traer el body documentado: tipoDocumentoPersona, iddependencia,
    nroDocumentoPersona, nombres, apellidoPaterno, apellidoMaterno, celular, correo,
    idtipodocumento, nrodocumento, asunto, nrofolios, adjunto, idFiles, idFilesAnexos,
    idtupa, linkAnexo.

    Devuelve `data` de la respuesta (incluye idtramite, el nº de expediente real) o None.
    ATENCIÓN: esto crea un expediente REAL en el sistema del GORE Cusco.
    """
    try:
        async with _client() as c:
            r = await c.post(_R_EMITIR, json=datos)
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("emitir_tramite falló: %s: %s", type(exc).__name__, str(exc)[:160])
        return None

    if isinstance(body, dict):
        return body.get("data") or body
    return None


# ---------------------------------------------------------------------------
# Prueba directa (solo lectura)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _demo() -> None:
        deps = await get_dependencias()
        print("dependencias:", len(deps))
        if deps:
            print("  ej:", deps[0])
        tipos = await get_tipo_documentos(deps[0]["iddependencia"]) if deps else []
        print("tipos de documento (dep 0):", len(tipos), "ej:", tipos[0] if tipos else None)
        seg = await consultar_estado_expediente(12, 6, 2026)
        print("seguimiento (12, 6, 2026):", (len(seg) if seg else 0), "movimientos")
        if seg:
            print("  primer movimiento accion/estado:", seg[0].get("accion"), "/", seg[0].get("estadorecepcion"))

    asyncio.run(_demo())
