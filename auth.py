"""Autenticación de Kardex: login, JWT, roles y cambio de contraseña.

AISLADO A PROPÓSITO: a futuro habrá un login centralizado para todas las apps
internas y solo debe cambiar este archivo. El resto de la app usa únicamente:
    login(), usuario_actual (dependencia FastAPI), es_supervisor(),
    validar_supervisor(), cambiar_password(), verificar().

Hoy los usuarios viven en la tabla de Dossier (config.USUARIOS_TABLE,
dossier_local.dbo.usuarios): mismo usuario y contraseña que Dossier.
  - username = nómina (p. ej. ATI3990); password_hash = bcrypt.
  - rol_kardex: 'almacenista' | 'supervisor' | NULL (= sin acceso a Kardex).
  - Cambiar la contraseña aquí la cambia también en Dossier.
  - IT da de alta usuarios y resetea contraseñas directo en la BD.

Sesión sin estado: JWT de 8 h (un turno). El rol viaja en el token, así que un
cambio de rol aplica hasta el siguiente login. La autorización de excepciones
(validar_supervisor) sí consulta la BD en el momento.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Header, HTTPException
from loguru import logger
from sqlalchemy import text

import config
import db

ROL_ALMACENISTA = "almacenista"
ROL_SUPERVISOR = "supervisor"
ROLES = (ROL_ALMACENISTA, ROL_SUPERVISOR)

JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = 8


def _sql(sql: str) -> str:
    # USUARIOS_TABLE ya lo validó config.validar() como nombre de tabla.
    return sql.replace("{tabla}", config.USUARIOS_TABLE)


def _buscar(nomina: str):
    with db.engine().connect() as c:
        return c.execute(
            text(_sql("SELECT username, password_hash, nombre, correo, rol_kardex FROM {tabla} WHERE username = :u")),
            {"u": nomina},
        ).mappings().first()


def _password_ok(password: str, password_hash) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), str(password_hash).encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _usuario(row) -> dict:
    return {
        "nomina": row["username"],  # la del registro, no la tecleada
        "nombre": row["nombre"] or row["username"],
        "rol": row["rol_kardex"],
        "correo": row["correo"] or "",
    }


def _credenciales(nomina: str | None, password: str | None):
    """Registro del usuario si la contraseña es correcta; si no, None."""
    nomina = (nomina or "").strip()
    if not nomina or not password:
        return None
    row = _buscar(nomina)
    if row is None or not _password_ok(password, row["password_hash"]):
        return None
    return row


# ============================================================
# Login / token
# ============================================================
def _secret() -> str:
    if not config.JWT_SECRET:
        raise RuntimeError("JWT_SECRET no está definido en el .env")
    return config.JWT_SECRET


def crear_token(usuario: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {**usuario, "iat": now, "exp": now + timedelta(hours=JWT_EXPIRE_HOURS)}
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def login(nomina: str | None, password: str | None) -> dict:
    """{token, user}. 401 si la contraseña está mal o si no tiene rol_kardex."""
    row = _credenciales(nomina, password)
    if row is None:
        logger.warning(f"login rechazado: {nomina!r}")
        raise HTTPException(status_code=401, detail="Nómina o contraseña incorrecta.")
    if row["rol_kardex"] not in ROLES:
        logger.warning(f"login sin rol_kardex: {row['username']!r}")
        raise HTTPException(
            status_code=401,
            detail="Tu usuario no tiene acceso a Kardex. Pide a IT que te dé de alta.",
        )
    usuario = _usuario(row)
    return {"token": crear_token(usuario), "user": usuario}


def usuario_actual(authorization: str | None = Header(default=None)) -> dict:
    """Dependencia FastAPI: usuario del header 'Authorization: Bearer <token>'.
    401 si falta, expiró o es inválido (el frontend regresa al login)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin sesión. Inicia sesión.")
    token = authorization[len("Bearer "):].strip()
    try:
        p = jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Tu sesión expiró. Vuelve a iniciar sesión.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Sesión inválida. Vuelve a iniciar sesión.")
    if p.get("rol") not in ROLES:
        raise HTTPException(status_code=401, detail="Sesión inválida. Vuelve a iniciar sesión.")
    return {"nomina": p["nomina"], "nombre": p["nombre"], "rol": p["rol"], "correo": p.get("correo", "")}


# ============================================================
# Roles
# ============================================================
def es_supervisor(usuario: dict) -> bool:
    return usuario.get("rol") == ROL_SUPERVISOR


def validar_supervisor(nomina: str | None, password: str | None) -> str | None:
    """Autorización en la pantalla del almacenista: nómina del supervisor si sus
    credenciales son correctas y su rol_kardex es supervisor; si no, None."""
    row = _credenciales(nomina, password)
    if row is None or row["rol_kardex"] != ROL_SUPERVISOR:
        return None
    return row["username"]


# ============================================================
# Contraseña (es la misma de Dossier)
# ============================================================
def cambiar_password(nomina: str, actual: str | None, nueva: str | None) -> None:
    if not nueva:
        raise HTTPException(status_code=400, detail="La contraseña nueva no puede ir vacía.")
    row = _credenciales(nomina, actual)
    if row is None:
        # 400 y no 401: un 401 sacaría al usuario de su sesión.
        raise HTTPException(status_code=400, detail="La contraseña actual no es correcta.")
    with db.engine().begin() as c:
        c.execute(
            text(_sql("UPDATE {tabla} SET password_hash = :h WHERE username = :u")),
            {"h": _hash(nueva), "u": row["username"]},
        )


def verificar() -> None:
    """Al arrancar: la tabla de usuarios existe y ya tiene rol_kardex."""
    try:
        with db.engine().connect() as c:
            c.execute(text(_sql("SELECT TOP 0 username, password_hash, nombre, correo, rol_kardex FROM {tabla}")))
    except Exception as e:
        raise RuntimeError(
            f"No puedo leer {config.USUARIOS_TABLE} con la columna rol_kardex. "
            f"Corre sql/01_crear_kardex.sql (paso 6). Detalle: {e}"
        ) from e
