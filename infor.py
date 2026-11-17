"""Stock pendiente de Infor (órdenes de compra abiertas). AISLADO.

ENDPOINT PENDIENTE: GET {API_BASE_URL}/infor/activePurchaseOrders hoy responde
404 (se está arreglando la consulta con Compras). Mientras tanto se deja
apagado con INFOR_PO_PATH vacío en el .env; para encenderlo:
    INFOR_PO_PATH=/infor/activePurchaseOrders

Contrato con el resto de la app:
    pendientes()                -> {np_normalizado: cantidad} | None
    pendiente_de(np, mapa)      -> int | None
None = NO DISPONIBLE (apagado, caído, 404, timeout o respuesta con otra forma).
NUNCA se asume pendiente = 0: la UI muestra "N/D". Si Infor sí respondió y el
item no aparece en ninguna OC abierta, su pendiente es 0.

Nota: los NP renombrados en la carga inicial (CFB-3/4, BEND-1/2,
20000000-...) no van a coincidir con Infor tal cual.
"""
import threading
import time

import requests
from loguru import logger

import config

# ---------- Forma de la respuesta ----------
# Se espera una lista de líneas de OC abiertas, como los demás endpoints
# /infor/* del API de datos (p. ej. /infor/transit -> [{"Id ATI", "Stock"}]).
CAMPO_LISTA = None  # [CONFIRMAR con respuesta real] si viene envuelta, p. ej. "data"
CAMPO_NUMERO_PARTE = "Id ATI"  # [CONFIRMAR con respuesta real]
CAMPO_CANTIDAD_ABIERTA = "Open Qty"  # [CONFIRMAR con respuesta real]

TIMEOUT_S = 5
CACHE_TTL_S = 300  # 5 min con una respuesta buena
CACHE_TTL_ERROR_S = 60  # tras una falla: no reintentar en cada búsqueda (cada intento puede tardar 5 s)

_cache = {"clave": None, "hasta": 0.0, "valor": None}
_lock = threading.Lock()


def normalizar_np(numero_parte) -> str:
    return " ".join(str(numero_parte).split()).upper()


def mapear(respuesta) -> dict[str, int]:
    """Suma las cantidades abiertas de todas las líneas de cada NP.
    ValueError si la respuesta no tiene la forma esperada."""
    lineas = respuesta[CAMPO_LISTA] if CAMPO_LISTA else respuesta
    if not isinstance(lineas, list):
        raise ValueError(f"se esperaba una lista de líneas, llegó {type(lineas).__name__}")
    totales: dict[str, float] = {}
    for linea in lineas:
        np = linea.get(CAMPO_NUMERO_PARTE)
        cant = linea.get(CAMPO_CANTIDAD_ABIERTA)
        if np is None or cant is None:
            raise ValueError(f"línea sin {CAMPO_NUMERO_PARTE!r} o {CAMPO_CANTIDAD_ABIERTA!r}: {linea}")
        clave = normalizar_np(np)
        totales[clave] = totales.get(clave, 0) + float(cant)
    return {k: int(round(v)) for k, v in totales.items()}


def _consultar() -> dict[str, int] | None:
    url = f"{config.API_BASE_URL}{config.INFOR_PO_PATH}"
    try:
        r = requests.get(url, timeout=TIMEOUT_S)
        r.raise_for_status()
        return mapear(r.json())
    except Exception as e:  # caído, 404, timeout, JSON raro: todo es "no disponible"
        logger.warning(f"Pendiente de Infor no disponible ({url}): {e}")
        return None


def pendientes() -> dict[str, int] | None:
    """Mapa NP -> pendiente, con cache de 5 min. None si no está disponible."""
    if not config.INFOR_PO_PATH:
        return None
    clave = f"{config.API_BASE_URL}{config.INFOR_PO_PATH}"
    with _lock:  # una sola consulta a la vez; las demás esperan el resultado
        if _cache["clave"] == clave and time.monotonic() < _cache["hasta"]:
            return _cache["valor"]
        valor = _consultar()
        ttl = CACHE_TTL_S if valor is not None else CACHE_TTL_ERROR_S
        _cache.update(clave=clave, hasta=time.monotonic() + ttl, valor=valor)
        return valor


def pendiente_de(numero_parte: str, mapa: dict[str, int] | None) -> int | None:
    if mapa is None:
        return None
    return mapa.get(normalizar_np(numero_parte), 0)


def limpiar_cache() -> None:
    with _lock:
        _cache.update(clave=None, hasta=0.0, valor=None)
