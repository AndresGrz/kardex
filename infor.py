"""Líneas de órdenes de compra en tránsito (Infor). AISLADO.

Consulta GET {API_BASE_URL}{INFOR_PO_PATH} (hoy /infor/kardex_transit), que
regresa una lista de líneas de OC abiertas:
    {"DocNum": "PEX000977", "LineNum": 10, "Reference": "ST-5-163-095 BROCA ...",
     "UoM": "EA", "PendingQty": 12.0, "CreateDate": "20/10/26",
     "ExpectedDeliveryDate": "20/11/26"}
Con INFOR_PO_PATH vacío queda apagado.

Este módulo SOLO trae y normaliza las líneas. Qué item le toca a cada línea
(match automático por la primera palabra de Reference o asignación manual) es
regla de negocio y vive en inventario.py.

Contrato:
    lineas() -> (lista_de_lineas, consultado_en_utc) | None
None = NO DISPONIBLE (apagado, caído, 404, timeout o respuesta con otra forma).
NUNCA se asume tránsito = 0: la UI muestra "N/D".
"""
import threading
import time
from datetime import date, datetime

import requests
from loguru import logger

import config
import db

# ---------- Forma de la respuesta (confirmada con /infor/kardex_transit) ----------
CAMPO_DOC = "DocNum"
CAMPO_LINEA = "LineNum"
CAMPO_REFERENCIA = "Reference"
CAMPO_CANTIDAD = "PendingQty"
CAMPO_UOM = "UoM"
CAMPO_CREADA = "CreateDate"
CAMPO_ENTREGA = "ExpectedDeliveryDate"
FORMATO_FECHA = "%d/%m/%y"  # "20/11/26" = 20 de noviembre de 2026

TIMEOUT_S = 5
CACHE_TTL_S = 300  # 5 min con una respuesta buena
CACHE_TTL_ERROR_S = 60  # tras una falla: no reintentar en cada búsqueda (cada intento puede tardar 5 s)

_cache = {"clave": None, "hasta": 0.0, "valor": None}
_lock = threading.Lock()


def normalizar_np(numero_parte) -> str:
    """Sin distinguir mayúsculas ni espacios (incluye \\xa0)."""
    return " ".join(str(numero_parte).split()).upper()


def codigo_de(referencia: str) -> str:
    """Primera palabra de Reference, normalizada: el NP que Compras escribe
    al inicio ('ST-5-163-095   BROCA ...' -> 'ST-5-163-095'). '' si no hay."""
    partes = referencia.split()  # split() sin argumento también parte en \xa0
    return normalizar_np(partes[0]) if partes else ""


def _fecha(valor) -> date | None:
    """'20/11/26' -> date(2026, 11, 20). None si viene vacía o con otro formato
    (una fecha rara no debe tumbar todo el tránsito)."""
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor).strip(), FORMATO_FECHA).date()
    except ValueError:
        return None


def _entero(valor, campo: str, linea) -> int:
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f"{campo!r} no es número en la línea {linea}")
    return int(round(n))


def normalizar(respuesta) -> list[dict]:
    """Convierte la respuesta del endpoint en líneas limpias.
    ValueError si no tiene la forma esperada (y entonces todo es 'no disponible')."""
    if not isinstance(respuesta, list):
        raise ValueError(f"se esperaba una lista de líneas, llegó {type(respuesta).__name__}")
    lineas = []
    for crudo in respuesta:
        if not isinstance(crudo, dict):
            raise ValueError(f"línea con forma inesperada: {crudo!r}")
        doc = " ".join(str(crudo.get(CAMPO_DOC) or "").split())
        if not doc or crudo.get(CAMPO_LINEA) is None or crudo.get(CAMPO_CANTIDAD) is None:
            raise ValueError(f"línea sin {CAMPO_DOC}, {CAMPO_LINEA} o {CAMPO_CANTIDAD}: {crudo}")
        referencia = " ".join(str(crudo.get(CAMPO_REFERENCIA) or "").split())
        lineas.append({
            "doc_num": doc,
            "line_num": _entero(crudo[CAMPO_LINEA], CAMPO_LINEA, crudo),
            "referencia": referencia,
            "codigo": codigo_de(referencia),
            "cantidad": _entero(crudo[CAMPO_CANTIDAD], CAMPO_CANTIDAD, crudo),
            "uom": " ".join(str(crudo.get(CAMPO_UOM) or "").split()),
            "creada": _fecha(crudo.get(CAMPO_CREADA)),
            "entrega": _fecha(crudo.get(CAMPO_ENTREGA)),
        })
    return lineas


def _consultar() -> list[dict] | None:
    url = f"{config.API_BASE_URL}{config.INFOR_PO_PATH}"
    try:
        r = requests.get(url, timeout=TIMEOUT_S)
        r.raise_for_status()
        return normalizar(r.json())
    except Exception as e:  # caído, 404, timeout, JSON raro: todo es "no disponible"
        logger.warning(f"Tránsito de Infor no disponible ({url}): {e}")
        return None


def lineas() -> tuple[list[dict], datetime] | None:
    """(líneas, momento UTC de la consulta), con cache de 5 min. None si no
    está disponible. El momento sirve para la limpieza de asignaciones: solo
    se borra lo que se asignó ANTES de esta consulta (ver inventario.py)."""
    if not config.INFOR_PO_PATH:
        return None
    clave = f"{config.API_BASE_URL}{config.INFOR_PO_PATH}"
    with _lock:  # una sola consulta a la vez; las demás esperan el resultado
        if _cache["clave"] == clave and time.monotonic() < _cache["hasta"]:
            return _cache["valor"]
        consultado_en = db.ahora_utc()
        lista = _consultar()
        valor = None if lista is None else (lista, consultado_en)
        ttl = CACHE_TTL_S if valor is not None else CACHE_TTL_ERROR_S
        _cache.update(clave=clave, hasta=time.monotonic() + ttl, valor=valor)
        return valor


def limpiar_cache() -> None:
    with _lock:
        _cache.update(clave=None, hasta=0.0, valor=None)
