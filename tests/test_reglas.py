"""Criterios de aceptación de Kardex (contra la BD real, datos ZZTEST-)."""
import threading

import pytest
import requests
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import config
import db
import infor
import inventario
from conftest import stock_de


def salida(client, H, rol, item_id, cantidad=1, **kw):
    body = {
        "item_id": item_id, "cantidad": cantidad, "departamento_destino": "ZZTEST Mantenimiento",
        "solicitante_nomina": "ZZTEST-SOL", "solicitante_nombre": "Solicitante de prueba", **kw,
    }
    return client.post("/api/movimientos/salida", json=body, headers=H[rol])


def entrada(client, H, rol, item_id, cantidad=1, **kw):
    body = {"item_id": item_id, "cantidad": cantidad, "orden_compra": "ZZTEST-OC1", "proveedor": "Proveedor prueba", **kw}
    return client.post("/api/movimientos/entrada", json=body, headers=H[rol])


def ajuste(client, H, item_id, cantidad, motivo="ZZTEST conteo"):
    return client.post("/api/movimientos/ajuste", json={"item_id": item_id, "cantidad": cantidad, "motivo": motivo}, headers=H["sup"])


def en_vista_bajo_minimo(numero_parte):
    with db.engine().connect() as c:
        return bool(c.execute(
            text("SELECT COUNT(*) FROM dbo.vw_items_bajo_minimo WHERE numero_parte = :np"), {"np": numero_parte}
        ).scalar())


# ============================================================
# Login y sesión
# ============================================================
def test_health_no_requiere_sesion(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["app"] == "kardex"


def test_login_ok(client, passwords):
    r = client.post("/api/login", json={"nomina": "ZZTEST-ALM", "password": passwords["alm"]})
    assert r.status_code == 200
    assert r.json()["user"] == {"nomina": "ZZTEST-ALM", "nombre": "Prueba Almacenista", "rol": "almacenista", "correo": ""}


def test_login_password_mala_401(client):
    r = client.post("/api/login", json={"nomina": "ZZTEST-ALM", "password": "no-es-esta"})
    assert r.status_code == 401


def test_login_sin_rol_kardex_401(client, passwords):
    r = client.post("/api/login", json={"nomina": "ZZTEST-SIN", "password": passwords["sin"]})
    assert r.status_code == 401
    assert "no tiene acceso" in r.json()["detail"]


def test_sin_token_401(client):
    assert client.get("/api/items").status_code == 401
    assert client.get("/api/yo", headers={"Authorization": "Bearer basura"}).status_code == 401


def test_yo(client, H):
    assert client.get("/api/yo", headers=H["sup"]).json()["rol"] == "supervisor"


def test_cambiar_password(client, passwords):
    login = client.post("/api/login", json={"nomina": "ZZTEST-ALM", "password": passwords["alm"]}).json()
    h = {"Authorization": f"Bearer {login['token']}"}
    assert client.post("/api/password", json={"actual": "mala", "nueva": "x"}, headers=h).status_code == 400
    nueva = passwords["alm"] + "-2"
    assert client.post("/api/password", json={"actual": passwords["alm"], "nueva": nueva}, headers=h).status_code == 200
    assert client.post("/api/login", json={"nomina": "ZZTEST-ALM", "password": nueva}).status_code == 200
    # se regresa para no afectar otras pruebas
    assert client.post("/api/password", json={"actual": nueva, "nueva": passwords["alm"]}, headers=h).status_code == 200


# ============================================================
# Permisos: el almacenista recibe 403 de la API
# ============================================================
def test_almacenista_no_puede_acciones_de_supervisor(client, H, passwords, nuevo_item):
    it = nuevo_item(stock_inicial=5, requiere_cambio=True)
    alm = H["alm"]
    assert client.post("/api/items", json={"numero_parte": "ZZTEST-NOPE", "descripcion": "x"}, headers=alm).status_code == 403
    assert client.patch(f"/api/items/{it['id']}", json={"descripcion": "otra"}, headers=alm).status_code == 403
    assert client.post("/api/movimientos/ajuste", json={"item_id": it["id"], "cantidad": 1, "motivo": "x"}, headers=alm).status_code == 403
    assert client.get("/api/chequeo-stock", headers=alm).status_code == 403
    # revertir obsoleto
    assert client.post(f"/api/items/{it['id']}/obsoleto", json={}, headers=alm).status_code == 200
    assert client.post(f"/api/items/{it['id']}/reactivar", json={}, headers=alm).status_code == 403
    assert client.post(f"/api/items/{it['id']}/reactivar", json={}, headers=H["sup"]).status_code == 200
    # autorizarse a sí mismo una excepción (con su propia nómina y contraseña)
    r = salida(client, H, "alm", it["id"], excepcion={
        "motivo": "me autorizo", "supervisor_nomina": "ZZTEST-ALM", "supervisor_password": passwords["alm"],
    })
    assert r.status_code == 403
    assert stock_de(it["id"]) == 5


# ============================================================
# Entradas y salidas
# ============================================================
def test_entrada_exige_orden_compra_y_proveedor(client, H, nuevo_item):
    it = nuevo_item()
    assert entrada(client, H, "alm", it["id"], orden_compra=None).status_code == 400
    assert entrada(client, H, "alm", it["id"], orden_compra="   ").status_code == 400
    assert entrada(client, H, "alm", it["id"], proveedor="").status_code == 400
    assert stock_de(it["id"]) == 0
    r = entrada(client, H, "alm", it["id"], cantidad=7, comentario="llegó completo")
    assert r.status_code == 201
    m = r.json()
    assert (m["tipo"], m["cantidad"], m["stock_resultante"], m["usuario_nomina"]) == ("ENTRADA", 7, 7, "ZZTEST-ALM")
    assert stock_de(it["id"]) == 7


def test_cantidades_solo_enteros_positivos(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=3)
    assert entrada(client, H, "alm", it["id"], cantidad=0).status_code == 400
    assert entrada(client, H, "alm", it["id"], cantidad=-2).status_code == 400
    assert entrada(client, H, "alm", it["id"], cantidad=1.5).status_code == 400
    assert salida(client, H, "alm", it["id"], cantidad=0).status_code == 400
    assert stock_de(it["id"]) == 3


def test_salida_mayor_al_stock_rechazada(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=3)
    r = salida(client, H, "alm", it["id"], cantidad=4)
    assert r.status_code == 409
    assert "Stock insuficiente" in r.json()["detail"]
    assert stock_de(it["id"]) == 3
    r = salida(client, H, "alm", it["id"], cantidad=3)
    assert r.status_code == 201 and r.json()["stock_resultante"] == 0


def test_salida_exige_datos_del_solicitante(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=3)
    for campo in ("departamento_destino", "solicitante_nomina", "solicitante_nombre"):
        assert salida(client, H, "alm", it["id"], **{campo: ""}).status_code == 400
    assert stock_de(it["id"]) == 3


def test_salida_con_requiere_cambio(client, H, passwords, nuevo_item):
    it = nuevo_item(stock_inicial=10, requiere_cambio=True)
    # sin pieza ni autorización
    assert salida(client, H, "alm", it["id"]).status_code == 400
    # contraseña de supervisor incorrecta
    r = salida(client, H, "alm", it["id"], excepcion={
        "motivo": "se perdió", "supervisor_nomina": "ZZTEST-SUP", "supervisor_password": "mala",
    })
    assert r.status_code == 403
    # autorización de un usuario que no es supervisor
    r = salida(client, H, "alm", it["id"], excepcion={
        "motivo": "se perdió", "supervisor_nomina": "ZZTEST-SIN", "supervisor_password": passwords["sin"],
    })
    assert r.status_code == 403
    # excepción sin motivo
    r = salida(client, H, "alm", it["id"], excepcion={
        "motivo": "", "supervisor_nomina": "ZZTEST-SUP", "supervisor_password": passwords["sup"],
    })
    assert r.status_code == 400
    assert stock_de(it["id"]) == 10
    # autorización válida: se guarda quién autorizó
    r = salida(client, H, "alm", it["id"], cantidad=2, excepcion={
        "motivo": "la pieza se rompió en la línea", "supervisor_nomina": "zztest-sup", "supervisor_password": passwords["sup"],
    })
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["excepcion_autorizada_por"] == "ZZTEST-SUP"
    assert m["excepcion_motivo"] == "la pieza se rompió en la línea"
    assert m["pieza_devuelta"] is False and m["usuario_nomina"] == "ZZTEST-ALM"
    assert stock_de(it["id"]) == 8
    # un supervisor que captura se autoriza a sí mismo con solo el motivo
    r = salida(client, H, "sup", it["id"], excepcion={"motivo": "urgente"})
    assert r.status_code == 201 and r.json()["excepcion_autorizada_por"] == "ZZTEST-SUP"


def test_pieza_devuelta_no_suma_stock(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=4, requiere_cambio=True)
    r = salida(client, H, "alm", it["id"], cantidad=1, pieza_devuelta=True)
    assert r.status_code == 201
    assert r.json()["pieza_devuelta"] is True and r.json()["stock_resultante"] == 3
    assert stock_de(it["id"]) == 3


def test_item_sin_requiere_cambio_no_pide_pieza(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=2)
    r = salida(client, H, "alm", it["id"], cantidad=1)
    assert r.status_code == 201 and r.json()["pieza_devuelta"] is False


# ============================================================
# Ajustes
# ============================================================
def test_ajuste(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=5)
    assert ajuste(client, H, it["id"], -2, motivo="").status_code == 400
    assert ajuste(client, H, it["id"], -2, motivo=None).status_code == 400
    assert ajuste(client, H, it["id"], 0).status_code == 400
    r = ajuste(client, H, it["id"], -6)
    assert r.status_code == 409 and "negativo" in r.json()["detail"]
    assert stock_de(it["id"]) == 5
    r = ajuste(client, H, it["id"], -2)
    assert r.status_code == 201 and r.json()["stock_resultante"] == 3
    r = ajuste(client, H, it["id"], 4)
    assert r.status_code == 201 and r.json()["stock_resultante"] == 7
    assert stock_de(it["id"]) == 7


# ============================================================
# Obsoleto
# ============================================================
def test_obsoleto(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=2, inventario_minimo=5)  # bajo mínimo
    assert en_vista_bajo_minimo(it["numero_parte"])
    r = client.post(f"/api/items/{it['id']}/obsoleto", json={"motivo": "ya no se usa"}, headers=H["alm"])
    assert r.status_code == 200 and r.json()["obsoleto"] is True
    assert client.post(f"/api/items/{it['id']}/obsoleto", json={}, headers=H["alm"]).status_code == 409
    # rechaza entrada y salida, conserva el stock
    assert entrada(client, H, "alm", it["id"]).status_code == 409
    assert salida(client, H, "alm", it["id"]).status_code == 409
    assert stock_de(it["id"]) == 2
    # permite ajuste
    assert ajuste(client, H, it["id"], -1).status_code == 201
    assert stock_de(it["id"]) == 1
    # sale de la alerta y de la búsqueda (salvo "incluir obsoletos")
    assert not en_vista_bajo_minimo(it["numero_parte"])
    q = it["numero_parte"]
    assert client.get("/api/items", params={"q": q}, headers=H["alm"]).json()["items"] == []
    con = client.get("/api/items", params={"q": q, "obsoletos": "true"}, headers=H["alm"]).json()["items"]
    assert [x["id"] for x in con] == [it["id"]] and con[0]["bajo_minimo"] is False
    # solo el supervisor revierte
    assert client.post(f"/api/items/{it['id']}/reactivar", json={}, headers=H["alm"]).status_code == 403
    assert client.post(f"/api/items/{it['id']}/reactivar", json={"motivo": "se volvió a usar"}, headers=H["sup"]).status_code == 200
    assert en_vista_bajo_minimo(it["numero_parte"])
    # la bitácora registra los eventos
    k = client.get(f"/api/items/{it['id']}", headers=H["alm"]).json()
    eventos = [(b["evento"], b["usuario_nomina"], b["motivo"]) for b in k["bitacora"]]
    assert eventos[:2] == [
        ("REVERTIR_OBSOLETO", "ZZTEST-SUP", "se volvió a usar"),
        ("OBSOLETO", "ZZTEST-ALM", "ya no se usa"),
    ]
    assert eventos[-1][0] == "ALTA_ITEM"


def test_alerta_excluye_minimo_cero(nuevo_item):
    cero = nuevo_item(inventario_minimo=0)
    con_min = nuevo_item(inventario_minimo=5, stock_inicial=5)  # stock <= mínimo
    arriba = nuevo_item(inventario_minimo=5, stock_inicial=6)
    assert not en_vista_bajo_minimo(cero["numero_parte"])
    assert en_vista_bajo_minimo(con_min["numero_parte"])
    assert not en_vista_bajo_minimo(arriba["numero_parte"])
    assert con_min["bajo_minimo"] is True and cero["bajo_minimo"] is False


# ============================================================
# Alta y edición de items
# ============================================================
def test_alta_con_stock_inicial(client, H, nuevo_item):
    it = nuevo_item(stock_inicial=12, marca_modelo_uso="3/4", inventario_minimo=2, inventario_maximo=20)
    assert it["stock_activo"] == 12 and it["unidad_medida"] == "EACH" and it["creado_por"] == "ZZTEST-SUP"
    k = client.get(f"/api/items/{it['id']}", headers=H["alm"]).json()
    assert [(m["tipo"], m["cantidad"], m["motivo"]) for m in k["movimientos"]] == [("AJUSTE", 12, "Carga inicial")]
    assert [b["evento"] for b in k["bitacora"]] == ["ALTA_ITEM"]


def test_alta_validaciones(client, H, nuevo_item):
    sup = H["sup"]
    assert client.post("/api/items", json={"numero_parte": "ZZTEST-SIN-DESC"}, headers=sup).status_code == 400
    assert client.post("/api/items", json={"descripcion": "sin np"}, headers=sup).status_code == 400
    r = client.post("/api/items", json={
        "numero_parte": "ZZTEST-MAXMIN", "descripcion": "x", "inventario_minimo": 10, "inventario_maximo": 5,
    }, headers=sup)
    assert r.status_code == 400 and "máximo" in r.json()["detail"]


def test_alta_np_duplicado_409(client, H, nuevo_item):
    it = nuevo_item()
    r = client.post("/api/items", json={"numero_parte": it["numero_parte"], "descripcion": "otra"}, headers=H["sup"])
    assert r.status_code == 409


def test_edicion_registra_una_fila_por_campo(client, H, nuevo_item):
    it = nuevo_item(inventario_minimo=1, requiere_cambio=False)
    otro = nuevo_item()
    nuevo_np = it["numero_parte"] + "-B"
    r = client.patch(f"/api/items/{it['id']}", json={
        "numero_parte": nuevo_np, "descripcion": "Item de prueba", "inventario_minimo": 3,
        "inventario_maximo": 9, "requiere_cambio": True,
    }, headers=H["sup"])
    assert r.status_code == 200, r.text
    assert r.json()["numero_parte"] == nuevo_np
    k = client.get(f"/api/items/{it['id']}", headers=H["sup"]).json()
    filas = {(b["campo"], b["valor_anterior"], b["valor_nuevo"]) for b in k["bitacora"] if b["evento"] == "EDICION_ITEM"}
    # descripcion no cambió: no deja fila
    assert filas == {
        ("numero_parte", it["numero_parte"], nuevo_np),
        ("inventario_minimo", "1", "3"),
        ("inventario_maximo", None, "9"),
        ("requiere_cambio", "No", "Sí"),
    }
    # NP duplicado al editar -> 409
    r = client.patch(f"/api/items/{it['id']}", json={"numero_parte": otro["numero_parte"]}, headers=H["sup"])
    assert r.status_code == 409
    # stock no es editable
    assert client.patch(f"/api/items/{it['id']}", json={"stock_activo": 99}, headers=H["sup"]).status_code == 400


def test_busqueda(client, H, nuevo_item):
    it = nuevo_item(descripcion="Punzón de prueba ZZ", marca_modelo_uso="BALA 5/8 ZZ")
    for q in (it["numero_parte"], "punzon de prueba zz", "BALA 5/8 ZZ"):
        res = client.get("/api/items", params={"q": q}, headers=H["alm"]).json()
        assert it["id"] in [x["id"] for x in res["items"]], q


# ============================================================
# Inmutabilidad
# ============================================================
def test_movimientos_solo_aceptan_post(client, H):
    for ruta in ("/api/movimientos/entrada", "/api/movimientos/salida", "/api/movimientos/ajuste", "/api/movimientos/1"):
        for metodo in ("PUT", "PATCH", "DELETE", "GET"):
            r = client.request(metodo, ruta, json={}, headers=H["sup"])
            assert r.status_code in (404, 405), (metodo, ruta, r.status_code)


@pytest.mark.parametrize("sql", [
    "UPDATE dbo.movimientos SET cantidad = cantidad WHERE item_id = :id",
    "DELETE FROM dbo.movimientos WHERE item_id = :id",
    "UPDATE dbo.bitacora SET motivo = N'x' WHERE item_id = :id",
    "DELETE FROM dbo.bitacora WHERE item_id = :id",
])
def test_trigger_impide_update_delete(sql, nuevo_item):
    it = nuevo_item(stock_inicial=1)
    with pytest.raises(DBAPIError) as e:
        with db.engine().begin() as c:
            c.execute(text(sql), {"id": it["id"]})
    assert "inmutable" in str(e.value)


# ============================================================
# Concurrencia: el UPDATE atómico nunca sobregira
# ============================================================
def test_concurrencia_salidas(nuevo_item, U):
    it = nuevo_item(stock_inicial=5)
    barrera = threading.Barrier(12)
    ok, rechazos, errores = [], [], []

    def sacar():
        barrera.wait()
        try:
            m = inventario.registrar_salida(it["id"], 1, "ZZTEST Prod", "ZZTEST-SOL", "Hilo", U["alm"])
            ok.append(m["stock_resultante"])
        except inventario.ReglaNegocio as e:
            rechazos.append(e.status)
        except Exception as e:  # cualquier otra cosa es una falla de la prueba
            errores.append(repr(e))

    hilos = [threading.Thread(target=sacar) for _ in range(12)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert errores == []
    assert len(ok) == 5 and len(rechazos) == 7
    assert sorted(ok) == [0, 1, 2, 3, 4]
    assert set(rechazos) == {409}
    assert stock_de(it["id"]) == 0
    assert inventario.chequeo_stock(U["sup"]) == {"ok": True, "filas": []}


def test_chequeo_stock(client, H):
    r = client.get("/api/chequeo-stock", headers=H["sup"])
    assert r.status_code == 200 and r.json() == {"ok": True, "filas": []}


# ============================================================
# Infor (stock pendiente)
# ============================================================
def test_infor_apagado(client, H, nuevo_item, monkeypatch):
    it = nuevo_item()
    monkeypatch.setattr(config, "INFOR_PO_PATH", "")
    res = client.get("/api/items", params={"q": it["numero_parte"]}, headers=H["alm"]).json()
    assert res["pendiente_disponible"] is False
    assert res["items"][0]["pendiente"] is None
    k = client.get(f"/api/items/{it['id']}", headers=H["alm"]).json()
    assert k["pendiente_disponible"] is False and k["item"]["pendiente"] is None


def test_infor_caido(client, H, nuevo_item, monkeypatch):
    it = nuevo_item()
    monkeypatch.setattr(config, "INFOR_PO_PATH", "/infor/activePurchaseOrders")

    def caido(*a, **kw):
        raise requests.ConnectionError("sin red")

    monkeypatch.setattr(infor.requests, "get", caido)
    res = client.get("/api/items", params={"q": it["numero_parte"]}, headers=H["alm"]).json()
    assert res["pendiente_disponible"] is False and res["items"][0]["pendiente"] is None


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._data


def test_infor_404_es_no_disponible(monkeypatch):
    monkeypatch.setattr(config, "INFOR_PO_PATH", "/infor/activePurchaseOrders")
    monkeypatch.setattr(infor.requests, "get", lambda *a, **kw: _Resp({"detail": "Not Found"}, 404))
    assert infor.pendientes() is None


def test_infor_mapeo_suma_lineas(client, H, nuevo_item, monkeypatch):
    con_oc = nuevo_item()
    sin_oc = nuevo_item()
    lineas = [
        {infor.CAMPO_NUMERO_PARTE: con_oc["numero_parte"].lower(), infor.CAMPO_CANTIDAD_ABIERTA: 3},
        {infor.CAMPO_NUMERO_PARTE: con_oc["numero_parte"], infor.CAMPO_CANTIDAD_ABIERTA: "4.000"},
        {infor.CAMPO_NUMERO_PARTE: "OTRO-NP", infor.CAMPO_CANTIDAD_ABIERTA: 9},
    ]
    assert infor.mapear(lineas) == {con_oc["numero_parte"]: 7, "OTRO-NP": 9}
    monkeypatch.setattr(config, "INFOR_PO_PATH", "/infor/activePurchaseOrders")
    llamadas = []

    def get(url, timeout):
        llamadas.append((url, timeout))
        return _Resp(lineas)

    monkeypatch.setattr(infor.requests, "get", get)
    res = client.get("/api/items", params={"q": "ZZTEST-"}, headers=H["alm"]).json()
    pend = {x["id"]: x["pendiente"] for x in res["items"]}
    assert res["pendiente_disponible"] is True
    assert pend[con_oc["id"]] == 7
    assert pend[sin_oc["id"]] == 0  # Infor respondió y el item no aparece -> 0
    client.get("/api/items", params={"q": "ZZTEST-"}, headers=H["alm"])
    assert llamadas == [(f"{config.API_BASE_URL}/infor/activePurchaseOrders", infor.TIMEOUT_S)]  # cache 5 min
