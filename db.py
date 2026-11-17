"""Acceso a la BD kardex: engine, modelos, fechas UTC <-> Monterrey y
verificación de esquema.

El esquema NO se crea aquí: su fuente de verdad es sql/01_crear_kardex.sql.
Al arrancar, verificar_esquema() solo comprueba que exista y, si falta algo,
corta el arranque con un mensaje claro.

Trampas de SQL Server (ya pasaron):
  - SQL Server no permite OUTPUT sin INTO en tablas con triggers: movimientos
    y bitacora llevan implicit_returning=False. items no tiene triggers.
  - `col.is_(False)` genera `IS 0` (error de sintaxis): usa `col == 0`.
  - with_for_update() no hace nada en mssql: ver inventario._mover_stock().
"""
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Unicode, create_engine, text
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import config

_engine = None
_engine_lock = threading.Lock()


def engine():
    """Engine perezoso: importar este módulo no requiere BD (pruebas de infor)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_engine(
                    config.DB_URL, pool_pre_ping=True, pool_size=10, max_overflow=10
                )
    return _engine


def sesion() -> Session:
    return Session(engine(), expire_on_commit=False)


# ============================================================
# Fechas: se guardan en UTC (DATETIME2(0), sin tz) y se muestran en hora local.
# En Windows la variable TZ no funciona: se usa zoneinfo + tzdata.
# ============================================================
def ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def a_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.TZ_LOCAL))


def fecha_iso(dt: datetime | None) -> str | None:
    """ISO con offset de Monterrey, p. ej. 2026-10-21T15:04:00-06:00."""
    loc = a_local(dt)
    return loc.isoformat() if loc else None


def fecha_txt(dt: datetime | None) -> str | None:
    """Para mostrar: 21/10/2026 15:04."""
    loc = a_local(dt)
    return loc.strftime("%d/%m/%Y %H:%M") if loc else None


# ============================================================
# Modelos (espejo de sql/01_crear_kardex.sql)
# ============================================================
class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    numero_parte: Mapped[str] = mapped_column(Unicode(60))
    descripcion: Mapped[str] = mapped_column(Unicode(200))
    marca_modelo_uso: Mapped[str | None] = mapped_column(Unicode(200))
    unidad_medida: Mapped[str] = mapped_column(Unicode(20))
    inventario_minimo: Mapped[int] = mapped_column(Integer)
    inventario_maximo: Mapped[int | None] = mapped_column(Integer)
    requiere_cambio: Mapped[bool] = mapped_column(Boolean)
    obsoleto: Mapped[bool] = mapped_column(Boolean)
    obsoleto_por: Mapped[str | None] = mapped_column(Unicode(50))
    obsoleto_en: Mapped[datetime | None] = mapped_column(DATETIME2(0))
    stock_activo: Mapped[int] = mapped_column(Integer)
    creado_por: Mapped[str] = mapped_column(Unicode(50))
    creado_en: Mapped[datetime] = mapped_column(DATETIME2(0))


class Movimiento(Base):
    __tablename__ = "movimientos"
    __table_args__ = {"implicit_returning": False}  # tiene trigger: sin OUTPUT

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    tipo: Mapped[str] = mapped_column(Unicode(10))
    cantidad: Mapped[int] = mapped_column(Integer)
    stock_resultante: Mapped[int] = mapped_column(Integer)
    usuario_nomina: Mapped[str] = mapped_column(Unicode(50))
    fecha_hora: Mapped[datetime] = mapped_column(DATETIME2(0))
    motivo: Mapped[str | None] = mapped_column(Unicode(500))
    orden_compra: Mapped[str | None] = mapped_column(Unicode(60))
    proveedor: Mapped[str | None] = mapped_column(Unicode(150))
    departamento_destino: Mapped[str | None] = mapped_column(Unicode(100))
    solicitante_nomina: Mapped[str | None] = mapped_column(Unicode(50))
    solicitante_nombre: Mapped[str | None] = mapped_column(Unicode(150))
    pieza_devuelta: Mapped[bool] = mapped_column(Boolean, default=False)
    excepcion_autorizada_por: Mapped[str | None] = mapped_column(Unicode(50))
    excepcion_motivo: Mapped[str | None] = mapped_column(Unicode(500))


class Bitacora(Base):
    __tablename__ = "bitacora"
    __table_args__ = {"implicit_returning": False}  # tiene trigger: sin OUTPUT

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    evento: Mapped[str] = mapped_column(Unicode(20))
    campo: Mapped[str | None] = mapped_column(Unicode(40))
    valor_anterior: Mapped[str | None] = mapped_column(Unicode(500))
    valor_nuevo: Mapped[str | None] = mapped_column(Unicode(500))
    motivo: Mapped[str | None] = mapped_column(Unicode(500))
    usuario_nomina: Mapped[str] = mapped_column(Unicode(50))
    fecha_hora: Mapped[datetime] = mapped_column(DATETIME2(0))


# ============================================================
# Verificación de esquema al arrancar
# ============================================================
OBJETOS_REQUERIDOS = {
    "items": "U",
    "movimientos": "U",
    "bitacora": "U",
    "vw_items_bajo_minimo": "V",
    "vw_chequeo_stock": "V",
}
TRIGGERS_REQUERIDOS = ("tr_movimientos_inmutable", "tr_bitacora_inmutable")


def verificar_esquema() -> None:
    """Falla con un mensaje claro si la BD no tiene el esquema de
    sql/01_crear_kardex.sql (la app nunca crea tablas)."""
    with engine().connect() as c:
        objetos = {
            r.name: r.type.strip()
            for r in c.execute(text("SELECT name, type FROM sys.objects WHERE schema_id = SCHEMA_ID('dbo')"))
        }
        triggers = {
            r.name: bool(r.is_disabled)
            for r in c.execute(text("SELECT name, is_disabled FROM sys.triggers"))
        }
    faltan = [n for n, t in OBJETOS_REQUERIDOS.items() if objetos.get(n) != t]
    faltan += [n for n in TRIGGERS_REQUERIDOS if n not in triggers]
    if faltan:
        raise RuntimeError(
            f"La BD no tiene el esquema de Kardex (faltan: {', '.join(faltan)}). "
            "Corre sql/01_crear_kardex.sql en SSMS. La app no crea tablas."
        )
    apagados = [n for n in TRIGGERS_REQUERIDOS if triggers[n]]
    if apagados:
        raise RuntimeError(
            f"Triggers de inmutabilidad deshabilitados: {', '.join(apagados)}. "
            "Habilítalos con ENABLE TRIGGER antes de arrancar."
        )
