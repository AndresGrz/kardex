"""Configuración de Kardex: lectura y validación del .env.

Único lugar que conoce los nombres de las variables. Los demás módulos leen
`config.<VAR>` al momento de usarla (no `from config import ...`), así las
pruebas pueden sobreescribir un valor sin recargar módulos.
"""
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

APP_VERSION = "1.0.0"


def _env(nombre: str, default: str = "") -> str:
    return os.getenv(nombre, default).strip()


DB_URL = _env("DB_URL")
API_BASE_URL = _env("API_BASE_URL", "http://10.207.5.9:1303").rstrip("/")
EMAIL_API_URL = _env("EMAIL_API_URL", "http://10.207.5.9:1999").rstrip("/")  # sin uso en v1
JWT_SECRET = _env("JWT_SECRET")
USUARIOS_TABLE = _env("USUARIOS_TABLE", "dossier_local.dbo.usuarios")
INFOR_PO_PATH = _env("INFOR_PO_PATH")  # vacío = pendiente de Infor apagado
TZ_LOCAL = _env("TZ_LOCAL", "America/Monterrey")

LOG_DIR = os.path.join(BASE_DIR, "logs")

# USUARIOS_TABLE se interpola en el SQL de auth.py: solo se acepta un nombre
# de tabla de 1 a 3 partes (base.esquema.tabla), nada más.
_NOMBRE_TABLA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


def validar() -> None:
    """Corta el arranque si falta configuración crítica, en vez de fallar raro
    más tarde. Bajo PM2 se ve como un proceso que no levanta (`pm2 logs kardex`)."""
    faltan = [n for n, v in (("DB_URL", DB_URL), ("JWT_SECRET", JWT_SECRET)) if not v]
    if faltan:
        raise RuntimeError(
            f"Falta {', '.join(faltan)} en el .env. Copia .env.example a .env junto a "
            'server.py. JWT_SECRET se genera con: python -c "import secrets; '
            'print(secrets.token_urlsafe(48))"'
        )
    if not _NOMBRE_TABLA.match(USUARIOS_TABLE):
        raise RuntimeError(f"USUARIOS_TABLE inválido: {USUARIOS_TABLE!r} (esperado base.esquema.tabla)")
    try:
        ZoneInfo(TZ_LOCAL)
    except ZoneInfoNotFoundError:
        raise RuntimeError(f"TZ_LOCAL inválido: {TZ_LOCAL!r}. ¿Está instalado tzdata?")
