"""Genera la carga inicial de sql/01_crear_kardex.sql a partir del Excel de almacén.

Uso:
    python sql/generar_carga_inicial.py ["C:\\ruta\\kardex final.xlsx"]

Lee la hoja Sheet1 y reescribe SOLO dos bloques del script SQL:
  - <<CARGA_ESPERADO>>: los conteos que el SQL verifica antes de confirmar.
  - <<CARGA_INICIAL>>:  los VALUES de items.
Los INSERT no se escriben a mano: si el Excel cambia, se vuelve a correr.

Columnas (por nombre de encabezado, en cualquier orden; las demás se ignoran,
p. ej. las notas de revisión REVISAR / NOTA KARDEX):
    NP ATIM | DESCRIPCION | MARCA/MODELO/PROCESO/USO | UOM | MINIMO | MAXIMO | STOCK | ALMACEN

Reglas de limpieza (acordadas con almacén):
  - Espacios duros (\\xa0) y espacios dobles se normalizan; todo se recorta.
  - ALMACEN obligatorio en cada fila: ATI o MAKRO (sin distinguir mayúsculas).
    Igual que en el alta: ningún item entra sin almacén.
  - MINIMO vacío -> 0 (no entra a la alerta). MAXIMO vacío -> sin máximo.
    STOCK vacío -> error: el conteo físico no se adivina.
  - NP repetidos DENTRO del mismo almacén (CFB, BEND...) se distinguen como
    NP-<MARCA/MODELO/USO>, p. ej. CFB-3/4. Si así siguen repetidos, se detiene.
  - Un NP en ATI y en MAKRO a la vez -> error: un item vive en un solo almacén.
  - Descripción vacía -> "SIN DESCRIPCION" (el supervisor la corrige editando;
    en altas nuevas la descripción es obligatoria).
  - Filas que almacén decidió no cargar: OMITIR (NP + almacén). Si una ya no
    aparece en el Excel, se detiene: la regla quedó vieja y hay que revisarla.
Nunca escribe nada si encuentra un error.
"""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

EXCEL_DEFAULT = r"C:\Users\andres.garza\Downloads\kardex final.xlsx"
HOJA = "Sheet1"
SQL_PATH = Path(__file__).with_name("01_crear_kardex.sql")
MARCAS = {
    "valores": ("-- <<CARGA_INICIAL>>", "-- <</CARGA_INICIAL>>"),
    "esperado": ("-- <<CARGA_ESPERADO>>", "-- <</CARGA_ESPERADO>>"),
}

ALMACENES = ("ATI", "MAKRO")  # mismo CHECK que items.almacen
SIN_DESCRIPCION = "SIN DESCRIPCION"

# Encabezado del Excel (normalizado) -> campo.
COLUMNAS = {
    "NP ATIM": "numero_parte",
    "DESCRIPCION": "descripcion",
    "MARCA/MODELO/PROCESO/USO": "marca_modelo_uso",
    "UOM": "unidad_medida",
    "MINIMO": "minimo",
    "MAXIMO": "maximo",
    "STOCK": "fisico",
    "ALMACEN": "almacen",
}

# (NP, almacén) que no se cargan. La caja de cartón 42X21X24 de MAKRO trae el
# mismo NP que la etiqueta gris de ATI; almacén decidió cargar solo la de ATI
# (19/11/2026). Si hace falta, el supervisor la da de alta con su NP correcto.
OMITIR = {
    ("EMB00124", "MAKRO"),
}

LARGOS = {"numero_parte": 60, "descripcion": 200, "marca_modelo_uso": 200, "unidad_medida": 20}


def texto(v):
    """Celda -> texto limpio o None. Los números enteros salen sin '.0'."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def entero(v, campo, fila, vacio="error"):
    """Entero >= 0. vacio: 'error' o el valor que toma una celda vacía."""
    if v is None or (isinstance(v, str) and not v.strip()):
        if vacio == "error":
            raise ValueError(f"Fila {fila}: {campo} está vacío")
        return vacio
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"Fila {fila}: {campo} debe ser entero >= 0 (viene {v!r})")
    return v


def almacen(v, fila):
    a = (texto(v) or "").upper()
    if a not in ALMACENES:
        raise ValueError(f"Fila {fila}: ALMACEN debe ser {' o '.join(ALMACENES)} (viene {v!r})")
    return a


def leer(excel_path):
    wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=True)
    ws = wb[HOJA]
    filas = ws.iter_rows(values_only=True)
    encabezado = [(texto(h) or "").upper() for h in next(filas)]
    idx = {}
    for nombre, campo in COLUMNAS.items():
        if nombre not in encabezado:
            raise ValueError(f"No encuentro la columna {nombre!r} en {encabezado}")
        idx[campo] = encabezado.index(nombre)

    items, omitidas = [], set()
    for n, row in enumerate(filas, start=2):
        datos = [row[i] if i < len(row) else None for i in idx.values()]
        if all(texto(c) is None for c in datos):  # fila vacía (las notas no cuentan)
            continue
        celda = lambda campo: row[idx[campo]] if idx[campo] < len(row) else None
        it = {
            "fila": n,
            "numero_parte": texto(celda("numero_parte")),
            "almacen": almacen(celda("almacen"), n),
            "descripcion": texto(celda("descripcion")),
            "marca_modelo_uso": texto(celda("marca_modelo_uso")),
            "unidad_medida": texto(celda("unidad_medida")) or "EACH",
            "minimo": entero(celda("minimo"), "MINIMO", n, vacio=0),
            "maximo": entero(celda("maximo"), "MAXIMO", n, vacio=None),
            "fisico": entero(celda("fisico"), "STOCK", n),
        }
        if not it["numero_parte"]:
            raise ValueError(f"Fila {n}: sin número de parte")
        clave = (it["numero_parte"].upper(), it["almacen"])
        if clave in OMITIR:
            omitidas.add(clave)
            continue
        if not it["descripcion"]:
            it["descripcion"] = SIN_DESCRIPCION
        if it["maximo"] is not None and it["maximo"] < it["minimo"]:
            raise ValueError(f"Fila {n}: máximo {it['maximo']} < mínimo {it['minimo']}")
        items.append(it)
    wb.close()
    faltan = OMITIR - omitidas
    if faltan:
        raise ValueError(f"OMITIR tiene filas que ya no están en el Excel: {sorted(faltan)}")
    return items


def distinguir_repetidos(items):
    """NP repetido en el mismo almacén -> NP-<marca>. SQL Server compara sin
    mayúsculas/minúsculas, así que aquí también."""
    grupos = defaultdict(list)
    for it in items:
        grupos[it["numero_parte"].upper()].append(it)

    errores, renombrados, reportados = [], [], set()
    for np, grupo in grupos.items():
        if len(grupo) < 2:
            continue
        filas = ", ".join(str(it["fila"]) for it in grupo)
        if len({it["almacen"] for it in grupo}) > 1:
            errores.append(f"NP {np} está en ATI y en MAKRO (filas {filas}): un item vive en un solo almacén")
            reportados.add(np)
            continue
        sin_marca = [it["fila"] for it in grupo if not it["marca_modelo_uso"]]
        if sin_marca:
            errores.append(f"NP {np} repetido (filas {filas}) y sin MARCA/MODELO/USO para distinguirlo en {sin_marca}")
            reportados.add(np)
            continue
        for it in grupo:
            it["numero_parte"] = f"{it['numero_parte']}-{it['marca_modelo_uso']}"
            renombrados.append(it["numero_parte"])

    finales = Counter(it["numero_parte"].upper() for it in items)
    for np, c in finales.items():
        if c > 1 and np not in reportados:
            filas = ", ".join(str(it["fila"]) for it in items if it["numero_parte"].upper() == np)
            errores.append(f"NP {np} sigue repetido después de agregar la marca (filas {filas}): necesita un NP único")
    if errores:
        raise ValueError("No se puede generar la carga:\n  - " + "\n  - ".join(errores))
    return renombrados


def sql_texto(s):
    return "NULL" if s is None else "N'" + s.replace("'", "''") + "'"


def sql_entero(n):
    return "NULL" if n is None else str(n)


def validar(items):
    for it in items:
        for campo, largo in LARGOS.items():
            if it[campo] and len(it[campo]) > largo:
                raise ValueError(f"Fila {it['fila']}: {campo} excede {largo} caracteres: {it[campo]!r}")


def conteos(items):
    por_almacen = Counter(it["almacen"] for it in items)
    return {
        "items": len(items),
        "ati": por_almacen["ATI"],
        "makro": por_almacen["MAKRO"],
        "movs": sum(1 for it in items if it["fisico"] > 0),
    }


def bloque_esperado(c):
    return (
        "    -- Generado por sql/generar_carga_inicial.py; no editar a mano.\n"
        f"    DECLARE @esperado_items INT = {c['items']}, @esperado_ati INT = {c['ati']}, "
        f"@esperado_makro INT = {c['makro']}, @esperado_movs INT = {c['movs']};"
    )


def bloque_valores(items):
    lineas = [
        "    -- Generado por sql/generar_carga_inicial.py; no editar a mano.",
        "    -- (numero_parte, almacen, descripcion, marca_modelo_uso, unidad_medida, minimo, maximo, STOCK, creado_por, creado_en)",
    ]
    valores = [
        f"    ({sql_texto(it['numero_parte'])}, {sql_texto(it['almacen'])}, {sql_texto(it['descripcion'])}, "
        f"{sql_texto(it['marca_modelo_uso'])}, {sql_texto(it['unidad_medida'])}, "
        f"{it['minimo']}, {sql_entero(it['maximo'])}, {it['fisico']}, N'admin', @ahora)"
        for it in items
    ]
    return "\n".join(lineas + [",\n".join(valores)])


def reemplazar(sql, marca, bloque):
    inicio, fin = MARCAS[marca]
    a, b = sql.index(inicio), sql.index(fin)
    return sql[: a + len(inicio)] + "\n" + bloque + "\n    " + sql[b:]


def escribir(items, c):
    sql = SQL_PATH.read_text(encoding="utf-8-sig")
    sql = reemplazar(sql, "esperado", bloque_esperado(c))
    sql = reemplazar(sql, "valores", bloque_valores(items))
    # Con BOM: SSMS abre bien los acentos y símbolos (Φ, Ó) solo así.
    SQL_PATH.write_text(sql, encoding="utf-8-sig")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    excel = sys.argv[1] if len(sys.argv) > 1 else EXCEL_DEFAULT
    try:
        items = leer(excel)
        renombrados = distinguir_repetidos(items)
        validar(items)
    except ValueError as e:
        print(f"ERROR (no se escribió nada): {e}")
        sys.exit(1)
    c = conteos(items)
    escribir(items, c)
    sin_desc = [it["numero_parte"] for it in items if it["descripcion"] == SIN_DESCRIPCION]
    print(f"OK: {c['items']} items (ATI {c['ati']}, MAKRO {c['makro']}), "
          f"{c['movs']} con STOCK > 0 -> {SQL_PATH.name}")
    print(f"Renombrados ({len(renombrados)}): {', '.join(renombrados)}")
    print(f"Sin descripción ({len(sin_desc)}): {', '.join(sin_desc)}")


if __name__ == "__main__":
    main()
