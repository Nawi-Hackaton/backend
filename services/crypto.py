"""
Ñawi — Cifrado de datos personales (Ley 29733).

Los datos personales del ciudadano (nombre, DNI, celular, correo) que provienen de
RENIEC/QELLQA o que el ciudadano entrega NUNCA deben quedar en texto plano: ni en logs,
ni en la base de datos. Este módulo cifra/descifra esos campos con Fernet (AES-128 en
modo CBC + HMAC), usando la clave ENCRYPTION_KEY del .env.

Diseño:
  - encrypt(texto)  -> str   (token Fernet, seguro para guardar en una columna TEXT)
  - decrypt(token)  -> str   (devuelve el texto original; si el valor no está cifrado o
                              la clave no coincide, devuelve el valor tal cual, para no
                              romper datos antiguos en texto plano).
  - is_enabled()    -> bool  (hay clave configurada).

No usamos cifrado determinista: cada llamada produce un token distinto. Por eso NO se debe
buscar en la base por el valor cifrado (p. ej. no buscar usuarios por DNI cifrado). Las
búsquedas se hacen por numero_whatsapp, que no es dato sensible de RENIEC.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("nawi.crypto")

_ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
_fernet = None

if _ENCRYPTION_KEY:
    try:
        from cryptography.fernet import Fernet

        _fernet = Fernet(_ENCRYPTION_KEY.encode())
    except Exception as exc:  # noqa: BLE001
        logger.error("ENCRYPTION_KEY inválida (%s). El cifrado quedará deshabilitado.", exc)
        _fernet = None


def is_enabled() -> bool:
    """True si hay una clave de cifrado válida configurada."""
    return _fernet is not None


def encrypt(text) -> str:
    """Cifra un texto y devuelve el token. Si no hay clave, devuelve el texto sin cambios."""
    if text is None:
        return text
    text = str(text)
    if _fernet is None or text == "":
        return text
    return _fernet.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token) -> str:
    """
    Descifra un token Fernet. Si el valor no está cifrado (datos antiguos en texto plano)
    o no se puede descifrar, devuelve el valor original sin romper la app.
    """
    if token is None:
        return token
    token = str(token)
    if _fernet is None or token == "":
        return token
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return token


if __name__ == "__main__":
    print("cifrado habilitado:", is_enabled())
    muestra = "MARSI VALERIA FIGUEROA LARRAGAN"
    tok = encrypt(muestra)
    print("cifrado:", tok[:40], "...")
    print("descifrado OK:", decrypt(tok) == muestra)
