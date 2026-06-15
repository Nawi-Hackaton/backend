from fastapi import APIRouter

from backend.services import qellqa

router = APIRouter()


@router.get("/health")
async def health():
    """Verifica que el backend está corriendo."""
    return {"status": "ok", "service": "nawi", "version": "1.0.0"}


@router.get("/api/debug/qellqa")
async def debug_qellqa():
    """Verifica la conectividad con la API de QELLQA (Mesa de Partes Virtual del GORE)."""
    deps = await qellqa.get_dependencias()
    reachable = bool(deps)
    return {
        "qellqa_api": "reachable" if reachable else "unreachable",
        "base_url": qellqa.BASE_URL,
        "dependencias_count": len(deps),
        "sample": deps[0] if deps else None,
    }
