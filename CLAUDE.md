# CLAUDE.md

Guía operativa de **Kardex**: qué es, cómo está construido y cómo desplegarlo.

## Qué es

App web interna (solo LAN) del almacén de la planta (American Tubing / ATI) para las
**refacciones que no viven en ningún sistema** (ni Infor ni producción). Almacén registra
números de parte, entradas, salidas, ajustes y traslados; queda un historial completo e
**inmutable**. Hay **dos almacenes**, uno por nave: **ATI** y **MAKRO**.
Prioridad: trazabilidad y que el stock en sistema cuadre con el físico. Simplicidad para el
almacenista por encima de funciones. UI en español.

Mismo patrón que **Dossier** (`C:\Users\andres.garza\Desktop\dossier`): FastAPI + uvicorn
sirve API y frontend estático; PM2 lo mantiene vivo; sin nginx; sin Alembic.

```
navegador -> uvicorn (10.207.5.9:1003) -> FastAPI (/api + frontend/)  -> SQL Server 10.207.5.10 (kardex)
                  ^ PM2                                                   + dossier_local.dbo.usuarios
```

## Estructura

```
server.py      Rutas FastAPI, esquemas Pydantic, ReglaNegocio -> HTTP, lifespan
               (valida .env + esquema), estáticos. Sin reglas de negocio.
inventario.py  TODAS las reglas de negocio (permisos incluidos). Llamarlo directo
               aplica las mismas reglas que la API.
auth.py        Login / JWT 8 h / roles / cambio de contraseña. AISLADO: cuando haya
               login centralizado, solo cambia este archivo.
infor.py       Trae y normaliza las líneas de OC en tránsito de Infor. AISLADO. Timeout 5 s,
               cache 5 min. Qué item le toca a cada línea es regla de inventario.py.
db.py          Engine, modelos SQLAlchemy, fechas UTC <-> Monterrey, verificar_esquema().
config.py      Lectura y validación del .env (único lugar con nombres de variables).
sql/01_crear_kardex.sql      FUENTE DE VERDAD del esquema + carga inicial. Se corre en SSMS.
                             No hay migraciones: un cambio de esquema = recrear la BD (ver Carga).
sql/generar_carga_inicial.py Regenera los bloques de carga del SQL (VALUES + conteos esperados)
                             desde el Excel.
frontend/      index.html, app.js (rutas por hash, sin framework), styles.css (sistema
               visual de Dossier + secciones KARDEX), icons.js.
tests/         conftest.py (fixtures, limpieza ZZTEST-), test_reglas.py (criterios).
deploy/run_server.bat, ecosystem.config.js, requirements.txt, .env.example
```

## Comandos

```bash
python -m venv venv && venv\Scripts\python.exe -m pip install -r requirements.txt
python -m uvicorn server:app --host 127.0.0.1 --port 1003 --reload   # desarrollo
python -m pytest                                                      # pruebas (BD real)
python sql/generar_carga_inicial.py ["C:\ruta\kardex final.xlsx"]  # regenerar carga inicial
deploy\run_server.bat                                                 # como en prod, sin PM2
```

## Configuración (`.env`, nunca en git)

`.env` lleva la contraseña `sa`: **no se sube a git**; `.env.example` documenta cada variable.
`DB_URL` = el de Dossier cambiando `/dossier_local` por `/kardex`. `JWT_SECRET` obligatorio
(sin él no arranca). `USUARIOS_TABLE=dossier_local.dbo.usuarios`.
`INFOR_PO_PATH=/infor/kardex_transit` (vacío = tránsito apagado). `TZ_LOCAL=America/Monterrey`.

## Modelo de datos (BD `kardex`)

La app **no crea tablas**: al arrancar `db.verificar_esquema()` y `auth.verificar()` revisan que
existan tablas, vistas, triggers (habilitados) y la columna `rol_kardex`; si falta algo, no arranca.

- `items`: catálogo. `stock_activo` es un **cache** de `SUM(movimientos.cantidad)`.
  `almacen` (`ATI | MAKRO`, NOT NULL, CHECK, **sin default**): el item vive en UN almacén a la vez.
- `movimientos`: **inmutable** (trigger `INSTEAD OF UPDATE, DELETE`). `cantidad` con signo:
  ENTRADA > 0, SALIDA < 0, AJUSTE ≠ 0. CHECKs de campos obligatorios por tipo. `almacen` es una
  **foto** del almacén del item al momento del movimiento (los viejos conservan el suyo tras un traslado).
- `bitacora`: **inmutable**. Eventos `ALTA_ITEM | EDICION_ITEM | OBSOLETO | REVERTIR_OBSOLETO |
  ASIGNAR_TRANSITO | QUITAR_TRANSITO | TRASLADO`; una fila por campo editado; booleanos como "Sí"/"No".
  Columna `motivo` para obsoleto/revertir/traslado. Tránsito: `campo='transito'`, valor
  `PEX000977-10 · <referencia>`. Traslado: `campo='almacen'`, `ATI → MAKRO`, motivo obligatorio
  (lo exige también `CK_bitacora_traslado`).
- `asignacion_transito`: línea de OC (`doc_num` + `line_num`, PK: una línea, un item) → `item_id`,
  asignada **a mano**. **No** es inmutable: la app borra las filas de líneas que ya no salen en el
  endpoint. El rastro queda en la bitácora.
- `vw_items_bajo_minimo`: la lee **Mercurio directo** para su correo (Kardex no envía correos ni
  expone nada para Mercurio). Trae la columna `almacen` (desde v1.2) para decir a qué nave le falta.
- `vw_chequeo_stock`: items con cache ≠ suma de movimientos. **Siempre debe estar vacía.**
- Usuarios: tabla de Dossier + columna `rol_kardex` (`almacenista | supervisor | NULL`).
  `username` = nómina. Todo lo que referencia a un usuario guarda la **nómina** (texto).
- Fechas en UTC (`DATETIME2(0)`); se muestran en hora de Monterrey con `zoneinfo` + `tzdata`.

## Reglas de negocio clave (todas en `inventario.py`)

| Acción | Almacenista | Supervisor |
| --- | --- | --- |
| Buscar, ver kardex, entrada, salida, marcar obsoleto, cambiar su contraseña | sí | sí |
| Ver el tránsito del item (tarjetas, solo lectura) | sí | sí |
| Alta / edición de items, ajustes, revertir obsoleto, chequeo de stock | **403** | sí |
| Trasladar un item entre almacenes (ATI ↔ MAKRO) | **403** | sí |
| Autorizar salida sin pieza devuelta | **403** | sí |
| Pestaña "Tránsito por asignar", asignar y quitar líneas | **403** | sí |

- **Entrada**: entero > 0; OC y proveedor obligatorios; comentario opcional.
- **Salida**: entero > 0, nunca mayor al stock; se cierra al momento. Departamento, nómina y
  nombre del solicitante obligatorios (texto libre). Si el item `requiere_cambio`: `pieza_devuelta`
  (solo constancia, **no suma stock**) o excepción. Excepción: el supervisor teclea nómina y
  contraseña en la pantalla del almacenista + motivo (se valida en el momento); un supervisor que
  captura se autoriza a sí mismo; un almacenista nunca se autoriza.
- **Ajuste**: solo supervisor, motivo obligatorio, no deja stock negativo, **sí en obsoletos**.
  Es la única forma de corregir el stock.
- **Obsoleto**: cualquier rol lo marca; conserva stock, rechaza entradas/salidas, sale de la
  alerta y de la búsqueda (salvo "incluir obsoletos"). Solo el supervisor lo revierte.
- **Alta**: NP único en todo Kardex (409, aunque sea del otro almacén), descripción obligatoria,
  **almacén obligatorio** (UI sin opción preseleccionada, API 400, BD NOT NULL sin default),
  máximo ≥ mínimo; `stock_inicial` entra como AJUSTE "Carga inicial" en ese almacén.
- **Almacenes**: los usuarios **no** están ligados a un almacén (los almacenistas rotan de nave).
  El almacén es un dato visible y un filtro: chip en cada fila, filtro Todos | ATI | Makro que
  recuerda el navegador (`localStorage`, cada PC está en una nave) y el almacén en grande en el
  resumen de cada captura. No se edita (PATCH con `almacen` → 400): solo cambia con un traslado.
- **Traslado** (`inventario.trasladar`, `POST /api/items/{id}/traslado {almacen, motivo}`): solo
  supervisor; mueve el item **completo con todo su stock**, al momento; motivo obligatorio; se
  permite con stock 0; **409 si está obsoleto** (primero se revierte) o si ya está en el destino.
  El destino va explícito para que dos supervisores a la vez no lo regresen. No es movimiento (el
  stock no cambia): va a la bitácora. UPDATE condicionado con `OUTPUT deleted.almacen`.
- **Consistencia**: el movimiento y el cache se escriben en la misma transacción con un **UPDATE
  atómico** (`inventario._mover_stock`): `UPDATE items SET stock_activo = stock_activo + :d OUTPUT
  inserted.stock_activo, inserted.almacen WHERE id = :id AND stock_activo + :d >= 0 [AND obsoleto = 0]`.
  Sin fila = stock insuficiente u obsoleto. Así dos almacenistas nunca sobregiran, y el almacén del
  movimiento sale de la misma fila bloqueada (una salida simultánea a un traslado no se equivoca).
- **Tránsito (Infor)**: `GET /infor/kardex_transit` → líneas `DocNum, LineNum, Reference, UoM,
  PendingQty, CreateDate, ExpectedDeliveryDate` (fechas `dd/mm/yy`). `inventario._resolver_transito()`:
  1. Primera palabra de `Reference` == `numero_parte` (sin mayúsculas/espacios) → **automática,
     siempre gana**. Compras escribe el NP primero; si no empata, se corrige el NP en Kardex.
  2. Si no, `DocNum + LineNum` en `asignacion_transito` → manual.
  3. Si no, sin asignar: no suma a nadie y sale en la pestaña del supervisor.
  "En tránsito" = suma de `PendingQty`. `infor.lineas()` regresa `None` si está apagado o falla —
  **nunca asumir 0**; la UI muestra "N/D". No hay opción "No es de Kardex": si sobran líneas, se
  filtra la consulta SQL del endpoint.
- **Limpieza con candado**: cuando el endpoint responde con líneas, se borran las asignaciones
  cuya línea ya no aparece **y** que se asignaron antes de esa consulta (con 2 workers, uno puede
  tener un cache más viejo que la asignación del otro). Si falla o viene vacío, no se borra nada.
  La limpieza no va a la bitácora; asignar/quitar a mano sí.
- **Mercurio** lee `vw_items_bajo_minimo`; cómo obtendrá el tránsito está pendiente (no es de v1).

## Trampas de SQL Server (ya pasaron)

- `with_for_update()` no hace nada en mssql: usar el UPDATE atómico.
- `OUTPUT` sin `INTO` no se permite en tablas con triggers: `movimientos` y `bitacora` llevan
  `implicit_returning: False`. `items` (sin triggers) sí usa `OUTPUT`.
- `col.is_(False)` genera `IS 0` (error de sintaxis): usar `col == 0`.
- `@app.on_event` está deprecado: se usa `lifespan`.
- La búsqueda usa `COLLATE Latin1_General_CI_AI` para ignorar acentos ("punzon" = "PUNZÓN").

## Pruebas

Corren contra la **BD real** (decisión del proyecto). Datos con prefijo `ZZTEST-` (items y los
usuarios `ZZTEST-ALM`, `ZZTEST-SUP`, `ZZTEST-SIN` con `rol='export'` y contraseña aleatoria por
corrida). Se borran al inicio y al final; para borrar movimientos/bitácora se deshabilitan los
triggers **dentro de una transacción**. Al terminar se verifica: 0 filas `ZZTEST-`, triggers
habilitados y `vw_chequeo_stock` vacía. El endpoint de Infor **siempre se simula** (fixture
`transito`; apagado por defecto) y la limpieza de asignaciones queda restringida a `DocNum`
`ZZTEST-…`: una lista simulada nunca debe borrar asignaciones reales.

## Carga inicial

Excel `C:\Users\andres.garza\Downloads\kardex final.xlsx` (hoja `Sheet1`), columnas por
nombre: `NP ATIM | DESCRIPCION | MARCA/MODELO/PROCESO/USO | UOM | MINIMO | MAXIMO | STOCK | ALMACEN`
(las demás se ignoran, p. ej. las notas de revisión). Reglas del generador:
- `ALMACEN` obligatorio por fila (`ATI`/`MAKRO`); un NP en los dos almacenes → error.
- MINIMO vacío → 0; MAXIMO vacío → sin máximo; STOCK vacío → error.
- NP repetidos en el mismo almacén (`CFB`, `BEND`, …) → `NP-<MARCA/MODELO/USO>`; si así siguen
  repetidos → error: necesitan NP único en el Excel.
- `\xa0`/espacios dobles se limpian; sin descripción → `SIN DESCRIPCION`.
- `OMITIR` (NP + almacén): filas que almacén decidió no cargar. Hoy solo la caja `EMB00124` de
  MAKRO (mismo NP que la etiqueta gris de ATI). Si la fila ya no está en el Excel → error.
- Excel final: 176 items (ATI 144, MAKRO 32), 159 movimientos.
- Ante cualquier error no escribe nada. Si pasa, escribe los VALUES y los conteos esperados
  (items, por almacén y movimientos); el SQL los verifica junto con 0 filas en `vw_chequeo_stock`
  y, si no cuadra, revierte toda la carga.

El script SQL nunca borra: si `kardex` ya tiene objetos, se detiene. **Recarga / cambio de
esquema**: respaldo → `DROP DATABASE kardex` en SSMS → correr `01_crear_kardex.sql`
(el paso de `rol_kardex` en `dossier_local` es idempotente). Se pierden movimientos, bitácora y
asignaciones de tránsito: es empezar de cero.

## Fuera de alcance en v1 (el diseño no lo impide)

Cierre semanal, exportación CSV, gráficas, correos, endpoints para Mercurio, solicitudes desde
producción, login centralizado, imagen del material, pronóstico de consumo, administración de
usuarios en la app (IT da de alta y resetea contraseñas directo en la BD).

## Despliegue

Servidor Windows `10.207.5.9`, PM2 → uvicorn en el **puerto 1003** con 2 workers, sin nginx.

### Primera vez

1. Requisitos: **Python 3.11+** (`python --version`), **Node + PM2** (`npm install -g pm2`) y el
   **puerto 1003/TCP** abierto en el firewall para la red interna. El esquema ya debe existir
   (`sql/01_crear_kardex.sql` corrido en SSMS).
2. Clonar: `git clone https://github.com/AndresGrz/kardex.git kardex` y `cd kardex`.
3. venv + dependencias:
   ```
   python -m venv venv
   venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   `ecosystem.config.js` usa `venv\Scripts\python.exe` si existe (si no, el `python` del PATH).
4. Configuración: `copy .env.example .env` y llenarlo (`DB_URL` con la contraseña `sa`,
   `JWT_SECRET` nuevo). Probar a mano con `deploy\run_server.bat` y abrir
   `http://10.207.5.9:1003`; cortar con Ctrl+C.
5. Levantar con PM2 y persistir:
   ```
   pm2 start ecosystem.config.js
   pm2 save
   ```
   (`pm2 startup` no existe en Windows; si las demás apps ya arrancan solas basta `pm2 save`;
   si no: `npm install -g pm2-windows-startup` + `pm2-startup install`.)
6. Actualizar (deploy normal): `git pull` → si cambió `requirements.txt`,
   `venv\Scripts\python.exe -m pip install -r requirements.txt` → `pm2 restart kardex`.
7. Verificar: `curl http://10.207.5.9:1003/api/health` → `{"ok":true,"app":"kardex",...}`
   (no toca la BD). Si hay dudas: `pm2 logs kardex --lines 50` y `logs\kardex.log`.

### Diagnóstico rápido

| Síntoma | Dónde mirar |
| --- | --- |
| No carga `http://10.207.5.9:1003` | ¿`pm2 list` lo muestra online? ¿firewall 1003 abierto? |
| Arranca y se cae | `pm2 logs kardex`: `.env` ausente, sin `JWT_SECRET`, o falta el esquema (mensaje claro) |
| `/api/health` responde pero el login no | BD: `DB_URL`, columna `rol_kardex`, usuario con `rol_kardex` |
| En tránsito siempre "N/D" | `INFOR_PO_PATH` vacío (apagado) o `/infor/kardex_transit` falla (ver log) |
| No arranca: "falta columna items.almacen" (u otro objeto) | BD con esquema viejo: respaldo → `DROP DATABASE kardex` → `01_crear_kardex.sql` |

Logs de la app en `logs/kardex.log` (rotación 5 MB, 10 archivos). Con 2 workers ambos escriben
el mismo archivo; si una rotación choca en Windows, loguru lo reporta en consola y sigue.
`logs/`, `venv/` y `.env` están en `.gitignore`.
