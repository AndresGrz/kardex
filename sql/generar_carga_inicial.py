"""Genera la carga inicial de sql/01_crear_kardex.sql a partir del Excel de almacén.

Uso:
    python sql/generar_carga_inicial.py ["C:\\ruta\\tools .xlsx"]

Lee la hoja Sheet1, limpia los textos y reescribe SOLO el bloque entre los
marcadores <<CARGA_INICIAL>> del script SQL. Los INSERT no se escriben a mano:
si el Excel cambia, se vuelve a correr este script.

Reglas de limpieza (acordadas con almacén):
  - Espacios duros (\\xa0) y espacios dobles se normalizan; todo se recorta.
  - Números de parte repetidos (CFB, 20000000, BEND) se distinguen como
    NP-<MARCA/MODELO/USO>, p. ej. CFB-3/4.
  - Descripción vacía -> "SIN DESCRIPCION" (el supervisor la corrige editando;
    en altas nuevas la descripción es obligatoria).
  - Correcciones puntuales en CORRECCIONES.
"""
import re
import sys
from collections import Counter
from pathlib import Path

import openpyxl

EXCEL_DEFAULT = r"C:\Users\andres.garza\Downloads\tools .xlsx"
HOJA = "Sheet1"
SQL_PATH = Path(__file__).with_name("01_crear_kardex.sql")
INICIO = "-- <<CARGA_INICIAL>>"
FIN = "-- <</CARGA_INICIAL>>"

# Deben coincidir con la verificación del paso 5 del script SQL.
ESPERADO_ITEMS = 72
ESPERADO_CON_STOCK = 66

SIN_DESCRIPCION = "SIN DESCRIPCION"

# Encabezado del Excel (normalizado) -> campo. "N°" se ignora.
COLUMNAS = {
    "NP ATIM": "numero_parte",
    "DESCRIPCION": "descripcion",
    "MARCA/MODELO/PROCESO/USO": "marca_modelo_uso",
    "UM": "unidad_medida",
    "MINIMO": "minimo",
    "MAXIMO": "maximo",
    "FISICO": "fisico",
}

# Excel convirtió "9/32" en la fecha 01/09/1932 (serial 11933) en el NP y en la
# marca. El diámetro de la fila (0.2812" = 9/32) lo confirma.
CORRECCIONES = {
    "BROCA 11933(ST-5-160-090)": {
        "numero_parte": "BROCA 9/32(ST-5-160-090)",
        "marca_modelo_uso": "9/32",
    },
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


def entero(v, campo, fila):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ValueError(f"Fila {fila}: {campo} debe ser entero >= 0 (viene {v!r})")
    return v


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

    items = []
    for n, row in enumerate(filas, start=2):
        if all(c is None for c in row):
            continue
        it = {
            "fila": n,
            "numero_parte": texto(row[idx["numero_parte"]]),
            "descripcion": texto(row[idx["descripcion"]]),
            "marca_modelo_uso": texto(row[idx["marca_modelo_uso"]]),
            "unidad_medida": texto(row[idx["unidad_medida"]]) or "EACH",
            "minimo": entero(row[idx["minimo"]], "MINIMO", n),
            "maximo": entero(row[idx["maximo"]], "MAXIMO", n),
            "fisico": entero(row[idx["fisico"]], "FISICO", n),
        }
        if not it["numero_parte"]:
            raise ValueError(f"Fila {n}: sin número de parte")
        it.update(CORRECCIONES.get(it["numero_parte"], {}))
        if not it["descripcion"]:
            it["descripcion"] = SIN_DESCRIPCION
        if it["maximo"] < it["minimo"]:
            raise ValueError(f"Fila {n}: máximo {it['maximo']} < mínimo {it['minimo']}")
        items.append(it)
    wb.close()
    return items


def distinguir_repetidos(items):
    """NP repetido -> NP-<marca>. SQL Server compara sin mayúsculas/minúsculas."""
    cuenta = Counter(it["numero_parte"].upper() for it in items)
    renombrados = []
    for it in items:
        if cuenta[it["numero_parte"].upper()] > 1:
            if not it["marca_modelo_uso"]:
                raise ValueError(f"Fila {it['fila']}: NP repetido sin MARCA/MODELO/USO para distinguirlo")
            it["numero_parte"] = f"{it['numero_parte']}-{it['marca_modelo_uso']}"
            renombrados.append(it["numero_parte"])
    finales = Counter(it["numero_parte"].upper() for it in items)
    dup = [np for np, c in finales.items() if c > 1]
    if dup:
        raise ValueError(f"Siguen repetidos después de distinguir: {dup}")
    return renombrados


def sql_texto(s):
    return "NULL" if s is None else "N'" + s.replace("'", "''") + "'"


def validar(items):
    for it in items:
        for campo, largo in LARGOS.items():
            if it[campo] and len(it[campo]) > largo:
                raise ValueError(f"Fila {it['fila']}: {campo} excede {largo} caracteres: {it[campo]!r}")
    con_stock = sum(1 for it in items if it["fisico"] > 0)
    if len(items) != ESPERADO_ITEMS or con_stock != ESPERADO_CON_STOCK:
        raise ValueError(
            f"Se esperaban {ESPERADO_ITEMS} items y {ESPERADO_CON_STOCK} con FISICO > 0; "
            f"vienen {len(items)} y {con_stock}. Revisa el Excel y las constantes."
        )
    return con_stock


def bloque_sql(items):
    lineas = [
        "    -- Generado por sql/generar_carga_inicial.py; no editar a mano.",
        "    -- (numero_parte, descripcion, marca_modelo_uso, unidad_medida, minimo, maximo, FISICO, creado_por, creado_en)",
    ]
    valores = [
        f"    ({sql_texto(it['numero_parte'])}, {sql_texto(it['descripcion'])}, "
        f"{sql_texto(it['marca_modelo_uso'])}, {sql_texto(it['unidad_medida'])}, "
        f"{it['minimo']}, {it['maximo']}, {it['fisico']}, N'admin', @ahora)"
        for it in items
    ]
    return "\n".join(lineas + [",\n".join(valores)])


def escribir(bloque):
    sql = SQL_PATH.read_text(encoding="utf-8-sig")
    a, b = sql.index(INICIO), sql.index(FIN)
    nuevo = sql[: a + len(INICIO)] + "\n" + bloque + "\n    " + sql[b:]
    # Con BOM: SSMS abre bien los acentos y símbolos (Φ, Ó) solo así.
    SQL_PATH.write_text(nuevo, encoding="utf-8-sig")


def main():
    excel = sys.argv[1] if len(sys.argv) > 1 else EXCEL_DEFAULT
    items = leer(excel)
    renombrados = distinguir_repetidos(items)
    con_stock = validar(items)
    escribir(bloque_sql(items))
    sin_desc = [it["numero_parte"] for it in items if it["descripcion"] == SIN_DESCRIPCION]
    print(f"OK: {len(items)} items, {con_stock} con FISICO > 0 -> {SQL_PATH.name}")
    print(f"Renombrados ({len(renombrados)}): {', '.join(renombrados)}")
    print(f"Sin descripción ({len(sin_desc)}): {', '.join(sin_desc)}")


if __name__ == "__main__":
    main()
