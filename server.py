"""Servidor web de Kardex (FastAPI + uvicorn).

Sirve el API bajo /api y el frontend estático (frontend/). Aquí solo hay rutas,
esquemas Pydantic y la traducción ReglaNegocio -> HTTP: las reglas viven en
inventario.py y la autenticación en auth.py.

    python -m uvicorn server:app --host 127.0.0.1 --port 1003 --reload   # desarrollo

Contrato:
  - Todas las rutas requieren 'Authorization: Bearer <token>' salvo login y health.
  - Errores de regla -> {"detail": "..."} en español con 400 / 403 / 404 / 409.
  - NO existen rutas PUT, PATCH ni DELETE para movimientos: son inmutables.
"""
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, ConfigDict

import auth
import config
import db
import inventario
from inventario import ReglaNegocio

FRONTEND_DIR = os.path.join(config.BASE_DIR, "frontend")

_log_configurado = False


def _configurar_logs() -> None:
    global _log_configurado
    if _log_configurado:
        return
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logger.add(
        os.path.join(config.LOG_DIR, "kardex.log"),
        rotation="5 MB", retention=10, encoding="utf-8", enqueue=True, level="INFO",
    )
    _log_configurado = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configurar_logs()
    config.validar()
    db.verificar_esquema()  # la app no crea tablas: si falta algo, no arranca
    auth.verificar()
    logger.info(f"Kardex v{config.APP_VERSION} listo")
    yield


app = FastAPI(title="Kardex", version=config.APP_VERSION, lifespan=lifespan)


# ============================================================
# Errores -> {"detail": "..."} en español
# ============================================================
@app.exception_handler(ReglaNegocio)
async def _regla(_request: Request, exc: ReglaNegocio):
    return JSONResponse(status_code=exc.status, content={"detail": exc.mensaje})


_TIPOS_ERROR = {
    "missing": "es obligatorio",
    "int_parsing": "debe ser un número entero",
    "int_from_float": "debe ser un número entero",
    "int_type": "debe ser un número entero",
    "bool_parsing": "debe ser sí o no",
    "bool_type": "debe ser sí o no",
    "string_type": "debe ser texto",
    "extra_forbidden": "no se acepta",
}


@app.exception_handler(RequestValidationError)
async def _validacion(_request: Request, exc: RequestValidationError):
    e = (exc.errors() or [{}])[0]
    campo = ".".join(str(p) for p in e.get("loc", ())[1:]) or "petición"
    if e.get("type") == "json_invalid":
        return JSONResponse(status_code=400, content={"detail": "El cuerpo de la petición no es JSON válido."})
    motivo = _TIPOS_ERROR.get(e.get("type"), "no es válido")
    return JSONResponse(status_code=400, content={"detail": f"El campo '{campo}' {motivo}."})


@app.exception_handler(Exception)
async def _inesperado(request: Request, exc: Exception):
    logger.exception(f"Error no controlado en {request.method} {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor. Revisa logs/kardex.log."})


@app.middleware("http")
async def _cache(request: Request, call_next):
    resp = await call_next(request)
    # El API nunca se cachea; el frontend se revalida (tras un git pull se ve
    # la versión nueva sin Ctrl+F5).
    resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return resp


# ============================================================
# Esquemas. Los textos y cantidades llegan opcionales a propósito: la
# validación (con mensajes en español) es de inventario.py.
# ============================================================
class _Cuerpo(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(_Cuerpo):
    nomina: str | None = None
    password: str | None = None


class PasswordIn(_Cuerpo):
    actual: str | None = None
    nueva: str | None = None


class ItemCambios(_Cuerpo):
    numero_parte: str | None = None
    descripcion: str | None = None
    marca_modelo_uso: str | None = None
    unidad_medida: str | None = None
    inventario_minimo: int | None = None
    inventario_maximo: int | None = None
    requiere_cambio: bool | None = None


class ItemNuevo(ItemCambios):
    stock_inicial: int | None = None


class MotivoIn(_Cuerpo):
    motivo: str | None = None


class EntradaIn(_Cuerpo):
    item_id: int | None = None
    cantidad: int | None = None
    orden_compra: str | None = None
    proveedor: str | None = None
    comentario: str | None = None


class ExcepcionIn(_Cuerpo):
    motivo: str | None = None
    supervisor_nomina: str | None = None
    supervisor_password: str | None = None


class SalidaIn(_Cuerpo):
    item_id: int | None = None
    cantidad: int | None = None
    departamento_destino: str | None = None
    solicitante_nomina: str | None = None
    solicitante_nombre: str | None = None
    comentario: str | None = None
    pieza_devuelta: bool = False
    excepcion: ExcepcionIn | None = None


class AjusteIn(_Cuerpo):
    item_id: int | None = None
    cantidad: int | None = None
    motivo: str | None = None


class LineaTransitoIn(_Cuerpo):
    doc_num: str | None = None
    line_num: int | None = None


class AsignarTransitoIn(LineaTransitoIn):
    item_id: int | None = None


# ============================================================
# Rutas
# ============================================================
api = APIRouter(prefix="/api")
Usuario = Depends(auth.usuario_actual)


@api.get("/health")
def health():
    """Liveness para verificar el deploy. NO toca la BD.
        curl http://10.207.5.9:1003/api/health"""
    return {"ok": True, "app": "kardex", "version": config.APP_VERSION}


@api.post("/login")
def login(body: LoginIn):
    res = auth.login(body.nomina, body.password)
    logger.info(f"login {res['user']['nomina']} ({res['user']['rol']})")
    return res


@api.get("/yo")
def yo(u: dict = Usuario):
    return u


@api.post("/password")
def password(body: PasswordIn, u: dict = Usuario):
    auth.cambiar_password(u["nomina"], body.actual, body.nueva)
    logger.info(f"cambio de contraseña de {u['nomina']}")
    return {"ok": True}


@api.get("/items")
def buscar(q: str | None = None, obsoletos: bool = False, u: dict = Usuario):
    return inventario.buscar_items(q, obsoletos)


@api.get("/items/{item_id}")
def kardex(item_id: int, u: dict = Usuario):
    return inventario.obtener_kardex(item_id)


@api.post("/items", status_code=201)
def crear_item(body: ItemNuevo, u: dict = Usuario):
    return inventario.crear_item(body.model_dump(exclude_none=True), u)


@api.patch("/items/{item_id}")
def editar_item(item_id: int, body: ItemCambios, u: dict = Usuario):
    return inventario.editar_item(item_id, body.model_dump(exclude_unset=True), u)


@api.post("/items/{item_id}/obsoleto")
def marcar_obsoleto(item_id: int, body: MotivoIn | None = None, u: dict = Usuario):
    return inventario.marcar_obsoleto(item_id, body.motivo if body else None, u)


@api.post("/items/{item_id}/reactivar")
def reactivar(item_id: int, body: MotivoIn | None = None, u: dict = Usuario):
    return inventario.reactivar(item_id, body.motivo if body else None, u)


@api.post("/movimientos/entrada", status_code=201)
def entrada(body: EntradaIn, u: dict = Usuario):
    return inventario.registrar_entrada(
        body.item_id, body.cantidad, body.orden_compra, body.proveedor, u, comentario=body.comentario
    )


@api.post("/movimientos/salida", status_code=201)
def salida(body: SalidaIn, u: dict = Usuario):
    return inventario.registrar_salida(
        body.item_id, body.cantidad, body.departamento_destino,
        body.solicitante_nomina, body.solicitante_nombre, u,
        comentario=body.comentario, pieza_devuelta=body.pieza_devuelta,
        excepcion=body.excepcion.model_dump() if body.excepcion else None,
    )


@api.post("/movimientos/ajuste", status_code=201)
def ajuste(body: AjusteIn, u: dict = Usuario):
    return inventario.registrar_ajuste(body.item_id, body.cantidad, body.motivo, u)


@api.get("/chequeo-stock")
def chequeo_stock(u: dict = Usuario):
    return inventario.chequeo_stock(u)


@api.get("/transito/por-asignar")
def transito_por_asignar(u: dict = Usuario):
    return inventario.transito_por_asignar(u)


@api.post("/transito/asignar", status_code=201)
def asignar_transito(body: AsignarTransitoIn, u: dict = Usuario):
    return inventario.asignar_transito(body.doc_num, body.line_num, body.item_id, u)


@api.post("/transito/quitar")
def quitar_transito(body: LineaTransitoIn, u: dict = Usuario):
    return inventario.quitar_transito(body.doc_num, body.line_num, u)


app.include_router(api)

# Al final, para no ensombrecer /api: el resto de rutas sirven el frontend.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
