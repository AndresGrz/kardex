"""TODAS las reglas de negocio de Kardex.

server.py solo traduce HTTP <-> estas funciones; cada una recibe el usuario
de la sesión ({nomina, nombre, rol}) y valida permisos aquí mismo, así que
llamarlas directo (pruebas, scripts) aplica exactamente las mismas reglas.
Un rechazo de regla es ReglaNegocio(mensaje, status) -> {"detail": mensaje}.

Consistencia del stock:
  - `movimientos` es la fuente de verdad; `items.stock_activo` es un cache que
    se actualiza en la MISMA transacción que el insert del movimiento.
  - Contra carreras entre dos almacenistas: UPDATE atómico que valida y suma
    en una sola sentencia y regresa el stock nuevo (_mover_stock). Ojo:
    with_for_update() de SQLAlchemy NO hace nada en SQL Server.
  - vw_chequeo_stock debe regresar siempre 0 filas (chequeo_stock()).
"""
import re

from loguru import logger
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

import auth
import db
import infor
from db import Bitacora, Item, Movimiento

ENTRADA, SALIDA, AJUSTE = "ENTRADA", "SALIDA", "AJUSTE"
MOTIVO_CARGA_INICIAL = "Carga inicial"


class ReglaNegocio(Exception):
    """Rechazo de una regla. status: 400 datos, 403 permiso, 404 no existe,
    409 conflicto con el estado actual (stock, obsoleto, duplicado)."""

    def __init__(self, mensaje: str, status: int = 400):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status = status


# ============================================================
# Validación de entradas
# ============================================================
def _texto(valor, nombre: str, largo: int, obligatorio: bool = False) -> str | None:
    """Recorta y colapsa espacios (incluye \\xa0). Vacío = None."""
    if valor is None:
        s = ""
    elif isinstance(valor, str):
        s = " ".join(valor.split())
    else:
        raise ReglaNegocio(f"{nombre}: debe ser texto.")
    if not s:
        if obligatorio:
            raise ReglaNegocio(f"{nombre} es obligatorio.")
        return None
    if len(s) > largo:
        raise ReglaNegocio(f"{nombre}: máximo {largo} caracteres.")
    return s


def _entero(valor, nombre: str) -> int:
    """Solo enteros (las unidades no llevan decimales)."""
    if isinstance(valor, bool) or valor is None:
        raise ReglaNegocio(f"{nombre} es obligatorio y debe ser un número entero.")
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if not isinstance(valor, int):
        raise ReglaNegocio(f"{nombre} debe ser un número entero.")
    return valor


def _positivo(valor, nombre: str = "La cantidad") -> int:
    n = _entero(valor, nombre)
    if n <= 0:
        raise ReglaNegocio(f"{nombre} debe ser mayor a 0.")
    return n


def _no_negativo(valor, nombre: str) -> int:
    n = _entero(valor, nombre)
    if n < 0:
        raise ReglaNegocio(f"{nombre} no puede ser negativo.")
    return n


def _id_item(item_id) -> int:
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise ReglaNegocio("Selecciona un item.")
    return item_id


def _exigir_supervisor(usuario: dict, accion: str) -> None:
    if not auth.es_supervisor(usuario):
        raise ReglaNegocio(f"Solo un supervisor puede {accion}.", 403)


# ============================================================
# Serialización
# ============================================================
def _bajo_minimo(it: Item) -> bool:
    # Mismo criterio que vw_items_bajo_minimo (la que lee Mercurio).
    return (not it.obsoleto) and it.inventario_minimo > 0 and it.stock_activo <= it.inventario_minimo


def item_dict(it: Item, pendientes: dict | None) -> dict:
    return {
        "id": it.id,
        "numero_parte": it.numero_parte,
        "descripcion": it.descripcion,
        "marca_modelo_uso": it.marca_modelo_uso,
        "unidad_medida": it.unidad_medida,
        "inventario_minimo": it.inventario_minimo,
        "inventario_maximo": it.inventario_maximo,
        "requiere_cambio": bool(it.requiere_cambio),
        "obsoleto": bool(it.obsoleto),
        "obsoleto_por": it.obsoleto_por,
        "obsoleto_en": db.fecha_txt(it.obsoleto_en),
        "stock_activo": it.stock_activo,
        "pendiente": infor.pendiente_de(it.numero_parte, pendientes),
        "bajo_minimo": _bajo_minimo(it),
        "creado_por": it.creado_por,
        "creado_en": db.fecha_txt(it.creado_en),
    }


def mov_dict(m: Movimiento) -> dict:
    return {
        "id": m.id,
        "item_id": m.item_id,
        "tipo": m.tipo,
        "cantidad": m.cantidad,
        "stock_resultante": m.stock_resultante,
        "usuario_nomina": m.usuario_nomina,
        "fecha_hora": db.fecha_iso(m.fecha_hora),
        "fecha": db.fecha_txt(m.fecha_hora),
        "motivo": m.motivo,
        "orden_compra": m.orden_compra,
        "proveedor": m.proveedor,
        "departamento_destino": m.departamento_destino,
        "solicitante_nomina": m.solicitante_nomina,
        "solicitante_nombre": m.solicitante_nombre,
        "pieza_devuelta": bool(m.pieza_devuelta),
        "excepcion_autorizada_por": m.excepcion_autorizada_por,
        "excepcion_motivo": m.excepcion_motivo,
    }


def bit_dict(b: Bitacora) -> dict:
    return {
        "id": b.id,
        "item_id": b.item_id,
        "evento": b.evento,
        "campo": b.campo,
        "valor_anterior": b.valor_anterior,
        "valor_nuevo": b.valor_nuevo,
        "motivo": b.motivo,
        "usuario_nomina": b.usuario_nomina,
        "fecha_hora": db.fecha_iso(b.fecha_hora),
        "fecha": db.fecha_txt(b.fecha_hora),
    }


def _valor_bitacora(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "Sí" if v else "No"
    return str(v)


# ============================================================
# Consultas
# ============================================================
def _like(term: str) -> str:
    # En SQL Server también '[' es comodín de LIKE.
    return "%" + re.sub(r"([\\%_\[])", r"\\\1", term) + "%"


# Búsqueda sin distinguir mayúsculas ni acentos ("punzon" encuentra "PUNZÓN").
_SIN_ACENTOS = "Latin1_General_CI_AI"


def buscar_items(q: str | None = None, obsoletos: bool = False) -> dict:
    """Busca en número de parte, descripción y marca/uso. Varias palabras =
    todas deben aparecer (en cualquiera de los tres campos). Los obsoletos se
    ocultan salvo con obsoletos=True."""
    stmt = select(Item)
    if not obsoletos:
        stmt = stmt.where(Item.obsoleto == 0)
    for term in (q or "").split():
        patron = _like(term)
        stmt = stmt.where(
            or_(*(
                col.collate(_SIN_ACENTOS).like(patron, escape="\\")
                for col in (Item.numero_parte, Item.descripcion, Item.marca_modelo_uso)
            ))
        )
    stmt = stmt.order_by(Item.numero_parte)
    with db.sesion() as s:
        items = s.scalars(stmt).all()
    pend = infor.pendientes()
    return {
        "items": [item_dict(it, pend) for it in items],
        "pendiente_disponible": pend is not None,
    }


def obtener_kardex(item_id: int) -> dict:
    """Item + movimientos + bitácora, de más reciente a más antiguo."""
    item_id = _id_item(item_id)
    with db.sesion() as s:
        it = s.get(Item, item_id)
        if it is None:
            raise ReglaNegocio("El item no existe.", 404)
        movs = s.scalars(
            select(Movimiento).where(Movimiento.item_id == item_id)
            .order_by(Movimiento.fecha_hora.desc(), Movimiento.id.desc())
        ).all()
        bits = s.scalars(
            select(Bitacora).where(Bitacora.item_id == item_id)
            .order_by(Bitacora.fecha_hora.desc(), Bitacora.id.desc())
        ).all()
    pend = infor.pendientes()
    return {
        "item": item_dict(it, pend),
        "movimientos": [mov_dict(m) for m in movs],
        "bitacora": [bit_dict(b) for b in bits],
        "pendiente_disponible": pend is not None,
    }


def chequeo_stock(usuario: dict) -> dict:
    """Items cuyo stock_activo no coincide con la suma de movimientos."""
    _exigir_supervisor(usuario, "ver el chequeo de stock")
    with db.engine().connect() as c:
        filas = [
            dict(r) for r in c.execute(text(
                "SELECT item_id, numero_parte, stock_activo, suma_movimientos, diferencia "
                "FROM dbo.vw_chequeo_stock ORDER BY numero_parte"
            )).mappings()
        ]
    return {"ok": not filas, "filas": filas}


# ============================================================
# Catálogo de items (alta / edición / obsoleto)
# ============================================================
CAMPOS_EDITABLES = (
    "numero_parte", "descripcion", "marca_modelo_uso", "unidad_medida",
    "inventario_minimo", "inventario_maximo", "requiere_cambio",
)


def _validar_campos(datos: dict) -> dict:
    """Valida solo los campos presentes en `datos`."""
    v = {}
    if "numero_parte" in datos:
        v["numero_parte"] = _texto(datos["numero_parte"], "El número de parte", 60, obligatorio=True)
    if "descripcion" in datos:
        v["descripcion"] = _texto(datos["descripcion"], "La descripción", 200, obligatorio=True)
    if "marca_modelo_uso" in datos:
        v["marca_modelo_uso"] = _texto(datos["marca_modelo_uso"], "Marca/modelo/uso", 200)
    if "unidad_medida" in datos:
        v["unidad_medida"] = _texto(datos["unidad_medida"], "La unidad de medida", 20) or "EACH"
    if "inventario_minimo" in datos:
        v["inventario_minimo"] = _no_negativo(datos["inventario_minimo"], "El mínimo")
    if "inventario_maximo" in datos:
        mx = datos["inventario_maximo"]
        v["inventario_maximo"] = None if mx is None else _no_negativo(mx, "El máximo")
    if "requiere_cambio" in datos:
        if not isinstance(datos["requiere_cambio"], bool):
            raise ReglaNegocio("'Requiere cambio' debe ser sí o no.")
        v["requiere_cambio"] = datos["requiere_cambio"]
    return v


def _validar_min_max(minimo: int, maximo: int | None) -> None:
    if maximo is not None and maximo < minimo:
        raise ReglaNegocio(f"El máximo ({maximo}) no puede ser menor al mínimo ({minimo}).")


def _np_duplicado(mensaje_np: str) -> ReglaNegocio:
    return ReglaNegocio(f"Ya existe un item con el número de parte {mensaje_np}.", 409)


def _es_unique_np(e: IntegrityError) -> bool:
    return "UQ_items_numero_parte" in str(e.orig)


def crear_item(datos: dict, usuario: dict) -> dict:
    """Alta (supervisor). `stock_inicial` opcional entra como AJUSTE 'Carga inicial'."""
    _exigir_supervisor(usuario, "dar de alta items")
    datos = dict(datos)
    stock_inicial = datos.pop("stock_inicial", None)
    stock_inicial = 0 if stock_inicial is None else _no_negativo(stock_inicial, "El stock inicial")
    desconocidos = set(datos) - set(CAMPOS_EDITABLES)
    if desconocidos:
        raise ReglaNegocio(f"Campo desconocido: {', '.join(sorted(desconocidos))}.")
    base = {
        "numero_parte": None, "descripcion": None, "marca_modelo_uso": None, "unidad_medida": "EACH",
        "inventario_minimo": 0, "inventario_maximo": None, "requiere_cambio": False,
    }
    v = _validar_campos({**base, **datos})
    _validar_min_max(v["inventario_minimo"], v["inventario_maximo"])

    ahora = db.ahora_utc()
    try:
        with db.sesion() as s, s.begin():
            if s.scalar(select(Item.id).where(Item.numero_parte == v["numero_parte"])):
                raise _np_duplicado(v["numero_parte"])
            it = Item(
                **v, obsoleto=False, stock_activo=stock_inicial,
                creado_por=usuario["nomina"], creado_en=ahora,
            )
            s.add(it)
            s.flush()
            s.add(Bitacora(
                item_id=it.id, evento="ALTA_ITEM", valor_nuevo=it.numero_parte,
                usuario_nomina=usuario["nomina"], fecha_hora=ahora,
            ))
            if stock_inicial > 0:
                s.add(Movimiento(
                    item_id=it.id, tipo=AJUSTE, cantidad=stock_inicial, stock_resultante=stock_inicial,
                    motivo=MOTIVO_CARGA_INICIAL, usuario_nomina=usuario["nomina"], fecha_hora=ahora,
                ))
    except IntegrityError as e:
        if _es_unique_np(e):  # dos altas simultáneas del mismo NP
            raise _np_duplicado(v["numero_parte"])
        raise
    logger.info(f"ALTA_ITEM {it.numero_parte} (id {it.id}) stock_inicial={stock_inicial} por {usuario['nomina']}")
    return item_dict(it, infor.pendientes())


def editar_item(item_id: int, cambios: dict, usuario: dict) -> dict:
    """Edición (supervisor): una fila EDICION_ITEM por campo que cambió."""
    _exigir_supervisor(usuario, "editar items")
    item_id = _id_item(item_id)
    desconocidos = set(cambios) - set(CAMPOS_EDITABLES)
    if desconocidos:
        raise ReglaNegocio(f"Campo no editable: {', '.join(sorted(desconocidos))}.")
    v = _validar_campos(cambios)

    ahora = db.ahora_utc()
    cambiados = []
    try:
        with db.sesion() as s, s.begin():
            it = s.get(Item, item_id)
            if it is None:
                raise ReglaNegocio("El item no existe.", 404)
            _validar_min_max(
                v.get("inventario_minimo", it.inventario_minimo),
                v.get("inventario_maximo", it.inventario_maximo),
            )
            if "numero_parte" in v and v["numero_parte"] != it.numero_parte:
                otro = s.scalar(select(Item.id).where(Item.numero_parte == v["numero_parte"], Item.id != item_id))
                if otro:
                    raise _np_duplicado(v["numero_parte"])
            for campo in CAMPOS_EDITABLES:  # orden fijo en la bitácora
                if campo not in v:
                    continue
                anterior, nuevo = getattr(it, campo), v[campo]
                if campo == "requiere_cambio":
                    anterior = bool(anterior)
                if anterior == nuevo:
                    continue
                setattr(it, campo, nuevo)
                cambiados.append(campo)
                s.add(Bitacora(
                    item_id=it.id, evento="EDICION_ITEM", campo=campo,
                    valor_anterior=_valor_bitacora(anterior), valor_nuevo=_valor_bitacora(nuevo),
                    usuario_nomina=usuario["nomina"], fecha_hora=ahora,
                ))
    except IntegrityError as e:
        if _es_unique_np(e):
            raise _np_duplicado(v["numero_parte"])
        raise
    if cambiados:
        logger.info(f"EDICION_ITEM {it.numero_parte} (id {it.id}) campos={cambiados} por {usuario['nomina']}")
    return item_dict(it, infor.pendientes())


def _cambiar_obsoleto(item_id: int, obsoleto: bool, motivo, usuario: dict) -> dict:
    item_id = _id_item(item_id)
    motivo = _texto(motivo, "El motivo", 500)
    ahora = db.ahora_utc()
    # UPDATE condicionado: si dos personas lo marcan a la vez, solo una gana
    # (no quedan dos filas en la bitácora).
    if obsoleto:
        sql = ("UPDATE dbo.items SET obsoleto = 1, obsoleto_por = :u, obsoleto_en = :t "
               "OUTPUT inserted.id WHERE id = :id AND obsoleto = 0")
    else:
        sql = ("UPDATE dbo.items SET obsoleto = 0, obsoleto_por = NULL, obsoleto_en = NULL "
               "OUTPUT inserted.id WHERE id = :id AND obsoleto = 1")
    with db.sesion() as s, s.begin():
        ok = s.execute(text(sql), {"u": usuario["nomina"], "t": ahora, "id": item_id}).first()
        if ok is None:
            it = s.get(Item, item_id)
            if it is None:
                raise ReglaNegocio("El item no existe.", 404)
            raise ReglaNegocio(
                "El item ya está obsoleto." if obsoleto else "El item no está obsoleto.", 409
            )
        s.add(Bitacora(
            item_id=item_id, evento="OBSOLETO" if obsoleto else "REVERTIR_OBSOLETO", campo="obsoleto",
            valor_anterior=_valor_bitacora(not obsoleto), valor_nuevo=_valor_bitacora(obsoleto),
            motivo=motivo, usuario_nomina=usuario["nomina"], fecha_hora=ahora,
        ))
    with db.sesion() as s:
        it = s.get(Item, item_id)
    logger.info(f"{'OBSOLETO' if obsoleto else 'REVERTIR_OBSOLETO'} {it.numero_parte} (id {item_id}) por {usuario['nomina']}")
    return item_dict(it, infor.pendientes())


def marcar_obsoleto(item_id: int, motivo, usuario: dict) -> dict:
    """Cualquier rol. Conserva su stock, rechaza entradas y salidas, sale de la
    alerta de mínimos y de la búsqueda (salvo 'incluir obsoletos')."""
    return _cambiar_obsoleto(item_id, True, motivo, usuario)


def reactivar(item_id: int, motivo, usuario: dict) -> dict:
    """Revertir obsoleto: solo supervisor."""
    _exigir_supervisor(usuario, "revertir un obsoleto")
    return _cambiar_obsoleto(item_id, False, motivo, usuario)


# ============================================================
# Movimientos (inmutables: solo se insertan)
# ============================================================
def _mover_stock(s, item_id: int, delta: int, permitir_obsoleto: bool) -> int | None:
    """UPDATE atómico: valida y suma en una sola sentencia y regresa el stock
    nuevo (= stock_resultante). None si el item no existe, el stock quedaría
    negativo o (sin permitir_obsoleto) el item está obsoleto. Dos salidas
    simultáneas se serializan en el candado de la fila: nunca se sobregira."""
    sql = ("UPDATE dbo.items SET stock_activo = stock_activo + :d "
           "OUTPUT inserted.stock_activo "
           "WHERE id = :id AND stock_activo + :d >= 0")
    if not permitir_obsoleto:
        sql += " AND obsoleto = 0"
    row = s.execute(text(sql), {"d": delta, "id": item_id}).first()
    return None if row is None else row[0]


def _rechazo_stock(s, item_id: int, tipo: str, delta: int) -> ReglaNegocio:
    """Por qué no pasó el UPDATE atómico (se consulta ya dentro de la misma transacción)."""
    it = s.get(Item, item_id)
    if it is None:
        return ReglaNegocio("El item no existe.", 404)
    if it.obsoleto and tipo != AJUSTE:
        return ReglaNegocio(f"{it.numero_parte} está obsoleto: no acepta entradas ni salidas.", 409)
    if tipo == AJUSTE:
        return ReglaNegocio(
            f"El ajuste dejaría el stock en negativo: hay {it.stock_activo} y el ajuste es {delta}.", 409
        )
    return ReglaNegocio(
        f"Stock insuficiente: hay {it.stock_activo} de {it.numero_parte} y se piden {-delta}.", 409
    )


def _insertar_movimiento(item_id: int, tipo: str, delta: int, usuario: dict, **campos) -> dict:
    """Movimiento + cache de stock en UNA transacción. Los ajustes sí aplican a obsoletos."""
    with db.sesion() as s, s.begin():
        stock = _mover_stock(s, item_id, delta, permitir_obsoleto=(tipo == AJUSTE))
        if stock is None:
            raise _rechazo_stock(s, item_id, tipo, delta)
        m = Movimiento(
            item_id=item_id, tipo=tipo, cantidad=delta, stock_resultante=stock,
            usuario_nomina=usuario["nomina"], fecha_hora=db.ahora_utc(), **campos,
        )
        s.add(m)
        s.flush()
    logger.info(f"{tipo} item {item_id} cantidad={delta} stock={stock} por {usuario['nomina']}")
    return mov_dict(m)


def registrar_entrada(item_id, cantidad, orden_compra, proveedor, usuario: dict, comentario=None) -> dict:
    """Suma stock. OC y proveedor obligatorios; comentario opcional."""
    item_id = _id_item(item_id)
    cantidad = _positivo(cantidad)
    oc = _texto(orden_compra, "La orden de compra", 60, obligatorio=True)
    prov = _texto(proveedor, "El proveedor", 150, obligatorio=True)
    coment = _texto(comentario, "El comentario", 500)
    return _insertar_movimiento(
        item_id, ENTRADA, cantidad, usuario,
        orden_compra=oc, proveedor=prov, motivo=coment,
    )


def registrar_salida(
    item_id, cantidad, departamento_destino, solicitante_nomina, solicitante_nombre,
    usuario: dict, comentario=None, pieza_devuelta: bool = False, excepcion: dict | None = None,
) -> dict:
    """Resta stock y se cierra en el momento (no hay borradores).

    Si el item requiere cambio de pieza, exige `pieza_devuelta` (solo
    constancia: NO suma stock) o una `excepcion` {motivo, supervisor_nomina,
    supervisor_password}. Un supervisor que captura se autoriza a sí mismo
    (solo da el motivo); un almacenista necesita nómina y contraseña de un
    supervisor tecleadas en su misma pantalla, y nunca puede autorizarse."""
    item_id = _id_item(item_id)
    cantidad = _positivo(cantidad)
    depto = _texto(departamento_destino, "El departamento destino", 100, obligatorio=True)
    sol_nomina = _texto(solicitante_nomina, "La nómina del solicitante", 50, obligatorio=True)
    sol_nombre = _texto(solicitante_nombre, "El nombre del solicitante", 150, obligatorio=True)
    coment = _texto(comentario, "El comentario", 500)

    with db.sesion() as s:
        it = s.get(Item, item_id)
    if it is None:
        raise ReglaNegocio("El item no existe.", 404)
    if it.obsoleto:
        raise ReglaNegocio(f"{it.numero_parte} está obsoleto: no acepta entradas ni salidas.", 409)

    pieza, autorizador, exc_motivo = False, None, None
    if it.requiere_cambio:
        if pieza_devuelta and excepcion:
            raise ReglaNegocio("Elige solo una: pieza devuelta o excepción autorizada.")
        if pieza_devuelta:
            pieza = True
        elif excepcion:
            exc_motivo = _texto(excepcion.get("motivo"), "El motivo de la excepción", 500, obligatorio=True)
            autorizador = _autorizar_excepcion(excepcion, usuario)
        else:
            raise ReglaNegocio(
                f"{it.numero_parte} requiere cambio de pieza: marca que se recibió la pieza usada "
                "o pide la autorización de un supervisor."
            )

    mov = _insertar_movimiento(
        item_id, SALIDA, -cantidad, usuario,
        departamento_destino=depto, solicitante_nomina=sol_nomina, solicitante_nombre=sol_nombre,
        motivo=coment, pieza_devuelta=pieza,
        excepcion_autorizada_por=autorizador, excepcion_motivo=exc_motivo,
    )
    if autorizador:
        logger.info(f"Excepción de pieza en movimiento {mov['id']}: autorizó {autorizador}")
    return mov


def _autorizar_excepcion(excepcion: dict, usuario: dict) -> str:
    """Nómina del supervisor que autoriza, validada en el momento."""
    if auth.es_supervisor(usuario):
        return usuario["nomina"]  # se autoriza a sí mismo
    nomina = (excepcion.get("supervisor_nomina") or "").strip()
    password = excepcion.get("supervisor_password") or ""
    if not nomina or not password:
        raise ReglaNegocio("Para la excepción, el supervisor debe teclear su nómina y contraseña.")
    if nomina.upper() == usuario["nomina"].upper():
        raise ReglaNegocio("Un almacenista no puede autorizar su propia excepción.", 403)
    autorizador = auth.validar_supervisor(nomina, password)
    if autorizador is None:
        logger.warning(f"Autorización de excepción rechazada: nómina {nomina!r} (capturó {usuario['nomina']})")
        raise ReglaNegocio("Nómina o contraseña de supervisor incorrecta, o ese usuario no es supervisor.", 403)
    return autorizador


def registrar_ajuste(item_id, cantidad, motivo, usuario: dict) -> dict:
    """Ajuste +/− (supervisor). Única forma de corregir el stock. Motivo
    obligatorio, no puede dejar el stock negativo y SÍ se permite en obsoletos."""
    _exigir_supervisor(usuario, "hacer ajustes de inventario")
    item_id = _id_item(item_id)
    cantidad = _entero(cantidad, "La cantidad del ajuste")
    if cantidad == 0:
        raise ReglaNegocio("La cantidad del ajuste no puede ser 0.")
    motivo = _texto(motivo, "El motivo del ajuste", 500, obligatorio=True)
    return _insertar_movimiento(item_id, AJUSTE, cantidad, usuario, motivo=motivo)
