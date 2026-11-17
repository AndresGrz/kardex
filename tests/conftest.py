"""Fixtures de pruebas de Kardex.

Las pruebas corren contra la BD REAL (decisión del proyecto: no hay BD de
pruebas). Todo dato de prueba lleva el prefijo ZZTEST-:
  - items en kardex.dbo.items (y sus movimientos / bitácora);
  - usuarios ZZTEST-ALM, ZZTEST-SUP y ZZTEST-SIN (sin rol_kardex) en la tabla
    de Dossier, con contraseña aleatoria por corrida y rol='export' (el CHECK
    de Dossier solo acepta admin | export | embarques).
Se borran al inicio y al final. Para borrar movimientos y bitácora se apagan
los triggers DENTRO de una transacción (el DDL es transaccional en SQL
Server), así nunca quedan apagados. Al terminar se verifica: 0 filas ZZTEST-,
triggers habilitados y vw_chequeo_stock vacía.
"""
import secrets
import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import config
import db
import infor
import server

PREFIJO = "ZZTEST-"
TRIGGERS = (("tr_movimientos_inmutable", "movimientos"), ("tr_bitacora_inmutable", "bitacora"))

USUARIOS = {
    "alm": {"nomina": "ZZTEST-ALM", "nombre": "Prueba Almacenista", "rol_kardex": "almacenista"},
    "sup": {"nomina": "ZZTEST-SUP", "nombre": "Prueba Supervisor", "rol_kardex": "supervisor"},
    "sin": {"nomina": "ZZTEST-SIN", "nombre": "Prueba Sin Acceso", "rol_kardex": None},
}


def _tabla(sql: str) -> str:
    return sql.replace("{tabla}", config.USUARIOS_TABLE)


def purgar() -> None:
    """Borra todo lo ZZTEST- en una sola transacción."""
    with db.engine().begin() as c:
        hay = c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE numero_parte LIKE 'ZZTEST-%'")).scalar()
        if hay:
            for trg, tabla in TRIGGERS:
                c.execute(text(f"DISABLE TRIGGER dbo.{trg} ON dbo.{tabla}"))
            for tabla in ("movimientos", "bitacora"):
                c.execute(text(
                    f"DELETE FROM dbo.{tabla} WHERE item_id IN "
                    "(SELECT id FROM dbo.items WHERE numero_parte LIKE 'ZZTEST-%')"
                ))
            for trg, tabla in TRIGGERS:
                c.execute(text(f"ENABLE TRIGGER dbo.{trg} ON dbo.{tabla}"))
            c.execute(text("DELETE FROM dbo.items WHERE numero_parte LIKE 'ZZTEST-%'"))
        c.execute(text(_tabla("DELETE FROM {tabla} WHERE username LIKE 'ZZTEST-%'")))


def verificar_limpio() -> None:
    with db.engine().connect() as c:
        items = c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE numero_parte LIKE 'ZZTEST-%'")).scalar()
        usuarios = c.execute(text(_tabla("SELECT COUNT(*) FROM {tabla} WHERE username LIKE 'ZZTEST-%'"))).scalar()
        apagados = c.execute(text(
            "SELECT COUNT(*) FROM sys.triggers WHERE is_disabled = 1 AND name IN "
            "('tr_movimientos_inmutable', 'tr_bitacora_inmutable')"
        )).scalar()
        descuadres = c.execute(text("SELECT COUNT(*) FROM dbo.vw_chequeo_stock")).scalar()
    assert items == 0, f"quedaron {items} items ZZTEST-"
    assert usuarios == 0, f"quedaron {usuarios} usuarios ZZTEST-"
    assert apagados == 0, "quedó un trigger de inmutabilidad deshabilitado"
    assert descuadres == 0, f"vw_chequeo_stock tiene {descuadres} filas"


@pytest.fixture(scope="session")
def passwords():
    """Crea los usuarios de prueba con contraseñas aleatorias de esta corrida."""
    config.validar()
    purgar()
    pw = {k: secrets.token_urlsafe(12) for k in USUARIOS}
    with db.engine().begin() as c:
        for k, u in USUARIOS.items():
            # rounds=4: el costo vive en el hash, así las pruebas no se alentan.
            h = bcrypt.hashpw(pw[k].encode(), bcrypt.gensalt(rounds=4)).decode()
            c.execute(
                text(_tabla(
                    "INSERT INTO {tabla} (username, password_hash, nombre, correo, rol, rol_kardex) "
                    "VALUES (:u, :h, :n, :c, 'export', :r)"
                )),
                {"u": u["nomina"], "h": h, "n": u["nombre"], "c": "", "r": u["rol_kardex"]},
            )
    yield pw
    purgar()
    verificar_limpio()


@pytest.fixture(scope="session")
def client(passwords):
    with TestClient(server.app) as c:  # corre el lifespan: verifica el esquema
        yield c


@pytest.fixture(scope="session")
def H(client, passwords):
    """Headers con token por rol: H['alm'], H['sup']."""
    headers = {}
    for k in ("alm", "sup"):
        r = client.post("/api/login", json={"nomina": USUARIOS[k]["nomina"], "password": passwords[k]})
        assert r.status_code == 200, r.text
        headers[k] = {"Authorization": f"Bearer {r.json()['token']}"}
    return headers


@pytest.fixture(scope="session")
def U():
    """Usuarios para llamar la lógica directo (como los arma auth.usuario_actual)."""
    return {
        k: {"nomina": u["nomina"], "nombre": u["nombre"], "rol": u["rol_kardex"], "correo": ""}
        for k, u in USUARIOS.items() if u["rol_kardex"]
    }


@pytest.fixture(autouse=True)
def _infor_limpio():
    infor.limpiar_cache()
    yield
    infor.limpiar_cache()


@pytest.fixture
def nuevo_item(client, H):
    """Crea un item ZZTEST- vía API (como supervisor) y regresa su dict."""
    def _crear(**kw):
        datos = {
            "numero_parte": f"{PREFIJO}{uuid.uuid4().hex[:10].upper()}",
            "descripcion": "Item de prueba",
            **kw,
        }
        r = client.post("/api/items", json=datos, headers=H["sup"])
        assert r.status_code == 201, r.text
        return r.json()
    return _crear


def stock_de(item_id: int) -> int:
    with db.engine().connect() as c:
        return c.execute(text("SELECT stock_activo FROM dbo.items WHERE id = :id"), {"id": item_id}).scalar()
