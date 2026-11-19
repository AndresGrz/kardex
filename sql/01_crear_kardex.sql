/* =====================================================================
   Kardex — 01_crear_kardex.sql
   ---------------------------------------------------------------------
   FUENTE DE VERDAD DEL ESQUEMA. Se corre UNA vez en SSMS, conectado a
   10.207.5.10 como sa. La app NO crea tablas: al arrancar solo verifica
   que existan y, si faltan, se niega a levantar.

   Qué hace, en orden:
     0. Guarda: si la BD kardex ya tiene tablas, se detiene SIN borrar nada.
     1. Crea la BD kardex (si no existe).
     2. Tablas items, movimientos, bitacora y asignacion_transito + índices.
     3. Triggers de inmutabilidad en movimientos y bitacora.
     4. Vistas vw_items_bajo_minimo (la lee Mercurio) y vw_chequeo_stock.
     5. Carga inicial del Excel "stock inicial kardex.xlsx" en UNA
        transacción, con verificación: los items y movimientos que contó el
        generador y 0 filas en vw_chequeo_stock. Si algo no cuadra, se
        revierte completa.
     6. Columna rol_kardex en dossier_local.dbo.usuarios; admin = supervisor.

   Los bloques de la carga inicial (entre los marcadores <<CARGA_INICIAL>>
   y <<CARGA_ESPERADO>>) los genera sql/generar_carga_inicial.py leyendo el
   Excel. No se editan a mano.

   Almacenes: cada item vive en UN almacén (ATI o MAKRO) a la vez. Se fija en
   el alta y solo cambia con un TRASLADO (supervisor, item completo con todo
   su stock, queda en la bitácora). Cada movimiento guarda el almacén en que
   estaba el item en ese momento.

   Recarga (BD que ya existe): el paso 0 se niega a correr sobre una BD con
   objetos. Para empezar de cero: respaldo -> DROP DATABASE kardex -> este
   script. dossier_local.dbo.usuarios (paso 6) no se toca.

   Fechas: todas en UTC (DATETIME2(0)); la app las muestra en hora de Monterrey.
   ===================================================================== */

SET NOCOUNT ON;
GO

/* ---------- 0. Guarda: nunca borrar un intento anterior ---------- */
IF DB_ID(N'kardex') IS NOT NULL
BEGIN
    DECLARE @n INT;
    EXEC sp_executesql
        N'SELECT @n = COUNT(*) FROM kardex.sys.objects WHERE name IN (N''items'', N''movimientos'', N''bitacora'', N''asignacion_transito'', N''vw_items_bajo_minimo'', N''vw_chequeo_stock'')',
        N'@n INT OUTPUT', @n = @n OUTPUT;
    IF @n > 0
    BEGIN
        RAISERROR(N'ALTO: la BD kardex ya tiene objetos de un intento anterior. Este script NO borra nada: avisa antes de continuar.', 16, 1);
        SET NOEXEC ON;
    END
END
GO

/* ---------- 1. Base de datos ---------- */
USE master;
GO
IF DB_ID(N'kardex') IS NULL
    CREATE DATABASE kardex;
GO
USE kardex;
GO

/* ---------- 2. Tablas ---------- */

-- Catálogo de refacciones. stock_activo es un CACHE de SUM(movimientos.cantidad);
-- la app lo actualiza en la misma transacción que inserta el movimiento.
-- Futuro (no implementado): imagen y pronóstico de consumo entran como
-- columnas NULL nuevas o tablas hijas; nada aquí lo impide.
CREATE TABLE dbo.items (
    id                  INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_items PRIMARY KEY,
    numero_parte        NVARCHAR(60)  NOT NULL CONSTRAINT UQ_items_numero_parte UNIQUE,
    almacen             NVARCHAR(10)  NOT NULL,  -- ATI | MAKRO. SIN default: el alta lo exige.
    descripcion         NVARCHAR(200) NOT NULL,
    marca_modelo_uso    NVARCHAR(200) NULL,
    unidad_medida       NVARCHAR(20)  NOT NULL CONSTRAINT DF_items_unidad DEFAULT (N'EACH'),
    inventario_minimo   INT           NOT NULL CONSTRAINT DF_items_minimo DEFAULT (0),
    inventario_maximo   INT           NULL,
    requiere_cambio     BIT           NOT NULL CONSTRAINT DF_items_requiere_cambio DEFAULT (0),
    obsoleto            BIT           NOT NULL CONSTRAINT DF_items_obsoleto DEFAULT (0),
    obsoleto_por        NVARCHAR(50)  NULL,      -- nómina
    obsoleto_en         DATETIME2(0)  NULL,      -- UTC
    stock_activo        INT           NOT NULL CONSTRAINT DF_items_stock DEFAULT (0),
    creado_por          NVARCHAR(50)  NOT NULL,  -- nómina
    creado_en           DATETIME2(0)  NOT NULL CONSTRAINT DF_items_creado_en DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_items_numero_parte CHECK (LEN(numero_parte) > 0),
    CONSTRAINT CK_items_almacen      CHECK (almacen IN (N'ATI', N'MAKRO')),
    CONSTRAINT CK_items_descripcion  CHECK (LEN(descripcion) > 0),
    CONSTRAINT CK_items_minimo       CHECK (inventario_minimo >= 0),
    CONSTRAINT CK_items_maximo       CHECK (inventario_maximo IS NULL OR (inventario_maximo >= 0 AND inventario_maximo >= inventario_minimo)),
    CONSTRAINT CK_items_stock        CHECK (stock_activo >= 0),
    CONSTRAINT CK_items_obsoleto     CHECK (obsoleto = 0 OR (obsoleto_por IS NOT NULL AND obsoleto_en IS NOT NULL))
);  
CREATE INDEX IX_items_descripcion ON dbo.items (descripcion);
GO

-- Movimientos de stock: INMUTABLE (ver trigger). La cantidad lleva signo:
-- ENTRADA > 0, SALIDA < 0, AJUSTE <> 0.
CREATE TABLE dbo.movimientos (
    id                        BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_movimientos PRIMARY KEY,
    item_id                   INT           NOT NULL CONSTRAINT FK_movimientos_items REFERENCES dbo.items (id),
    tipo                      NVARCHAR(10)  NOT NULL,
    almacen                   NVARCHAR(10)  NOT NULL,  -- foto: almacén del item al momento del movimiento
    cantidad                  INT           NOT NULL,
    stock_resultante          INT           NOT NULL,
    usuario_nomina            NVARCHAR(50)  NOT NULL,
    fecha_hora                DATETIME2(0)  NOT NULL CONSTRAINT DF_movimientos_fecha DEFAULT (SYSUTCDATETIME()),
    motivo                    NVARCHAR(500) NULL,      -- comentario (entrada/salida) o motivo (ajuste)
    -- ENTRADA
    orden_compra              NVARCHAR(60)  NULL,
    proveedor                 NVARCHAR(150) NULL,
    -- SALIDA (texto libre: el solicitante no tiene que ser usuario de Kardex)
    departamento_destino      NVARCHAR(100) NULL,
    solicitante_nomina        NVARCHAR(50)  NULL,
    solicitante_nombre        NVARCHAR(150) NULL,
    -- SALIDA de un item con requiere_cambio: pieza devuelta o excepción autorizada
    pieza_devuelta            BIT           NOT NULL CONSTRAINT DF_movimientos_pieza DEFAULT (0),
    excepcion_autorizada_por  NVARCHAR(50)  NULL,      -- nómina del supervisor
    excepcion_motivo          NVARCHAR(500) NULL,
    CONSTRAINT CK_mov_tipo      CHECK (tipo IN (N'ENTRADA', N'SALIDA', N'AJUSTE')),
    CONSTRAINT CK_mov_almacen   CHECK (almacen IN (N'ATI', N'MAKRO')),
    CONSTRAINT CK_mov_cantidad  CHECK ((tipo = N'ENTRADA' AND cantidad > 0)
                                    OR (tipo = N'SALIDA'  AND cantidad < 0)
                                    OR (tipo = N'AJUSTE'  AND cantidad <> 0)),
    CONSTRAINT CK_mov_stock     CHECK (stock_resultante >= 0),
    CONSTRAINT CK_mov_entrada   CHECK (tipo <> N'ENTRADA' OR (LEN(ISNULL(orden_compra, N'')) > 0
                                                          AND LEN(ISNULL(proveedor, N'')) > 0)),
    CONSTRAINT CK_mov_salida    CHECK (tipo <> N'SALIDA' OR (LEN(ISNULL(departamento_destino, N'')) > 0
                                                         AND LEN(ISNULL(solicitante_nomina, N'')) > 0
                                                         AND LEN(ISNULL(solicitante_nombre, N'')) > 0)),
    CONSTRAINT CK_mov_ajuste    CHECK (tipo <> N'AJUSTE' OR LEN(ISNULL(motivo, N'')) > 0),
    CONSTRAINT CK_mov_cambio_solo_salida CHECK (tipo = N'SALIDA'
                                    OR (pieza_devuelta = 0 AND excepcion_autorizada_por IS NULL AND excepcion_motivo IS NULL)),
    -- si hay autorizador, hay motivo (y viceversa)
    CONSTRAINT CK_mov_excepcion CHECK ((excepcion_autorizada_por IS NULL AND excepcion_motivo IS NULL)
                                    OR (excepcion_autorizada_por IS NOT NULL AND LEN(ISNULL(excepcion_motivo, N'')) > 0)),
    CONSTRAINT CK_mov_pieza_o_excepcion CHECK (NOT (pieza_devuelta = 1 AND excepcion_autorizada_por IS NOT NULL))
);
CREATE INDEX IX_movimientos_item_fecha ON dbo.movimientos (item_id, fecha_hora);
CREATE INDEX IX_movimientos_fecha      ON dbo.movimientos (fecha_hora);
GO

-- Bitácora de cambios al catálogo: INMUTABLE (ver trigger). Una fila por       
-- campo editado; los booleanos se guardan como 'Sí' / 'No'.
CREATE TABLE dbo.bitacora (
    id              BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_bitacora PRIMARY KEY,
    item_id         INT           NOT NULL CONSTRAINT FK_bitacora_items REFERENCES dbo.items (id),
    evento          NVARCHAR(20)  NOT NULL,
    campo           NVARCHAR(40)  NULL,
    valor_anterior  NVARCHAR(500) NULL,
    valor_nuevo     NVARCHAR(500) NULL,
    motivo          NVARCHAR(500) NULL,      -- opcional en OBSOLETO / REVERTIR_OBSOLETO; obligatorio en TRASLADO
    usuario_nomina  NVARCHAR(50)  NOT NULL,
    fecha_hora      DATETIME2(0)  NOT NULL CONSTRAINT DF_bitacora_fecha DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_bitacora_evento CHECK (evento IN (N'ALTA_ITEM', N'EDICION_ITEM', N'OBSOLETO', N'REVERTIR_OBSOLETO',
                                                    N'ASIGNAR_TRANSITO', N'QUITAR_TRANSITO', N'TRASLADO')),
    CONSTRAINT CK_bitacora_campo  CHECK (evento <> N'EDICION_ITEM' OR campo IS NOT NULL),
    -- TRASLADO: campo 'almacen', de un almacén a OTRO, con motivo.
    CONSTRAINT CK_bitacora_traslado CHECK (evento <> N'TRASLADO' OR (
        campo = N'almacen'
        AND valor_anterior IN (N'ATI', N'MAKRO') AND valor_nuevo IN (N'ATI', N'MAKRO')
        AND valor_anterior <> valor_nuevo
        AND LEN(ISNULL(motivo, N'')) > 0))
);
CREATE INDEX IX_bitacora_item_fecha ON dbo.bitacora (item_id, fecha_hora);
GO

-- Líneas de OC de Infor relacionadas A MANO con un item (cuando la primera
-- palabra de Reference no empata con ningún numero_parte). NO es inmutable:
-- la app borra las filas de líneas que ya se recibieron o cancelaron; el
-- rastro queda en la bitácora (ASIGNAR_TRANSITO / QUITAR_TRANSITO).
-- En una BD creada antes de este cambio se agrega con sql/02_transito.sql.
CREATE TABLE dbo.asignacion_transito (
    doc_num       NVARCHAR(30)  NOT NULL,      -- DocNum de Infor, p. ej. PEX000977
    line_num      INT           NOT NULL,      -- LineNum de Infor, p. ej. 10
    item_id       INT           NOT NULL CONSTRAINT FK_asignacion_transito_items REFERENCES dbo.items (id),
    referencia    NVARCHAR(500) NULL,          -- copia de Reference al asignar
    asignado_por  NVARCHAR(50)  NOT NULL,      -- nómina del supervisor
    asignado_en   DATETIME2(0)  NOT NULL CONSTRAINT DF_asignacion_transito_en DEFAULT (SYSUTCDATETIME()),  -- UTC
    CONSTRAINT PK_asignacion_transito PRIMARY KEY (doc_num, line_num),  -- una línea, un item
    CONSTRAINT CK_asignacion_transito_doc CHECK (LEN(doc_num) > 0)
);
CREATE INDEX IX_asignacion_transito_item ON dbo.asignacion_transito (item_id);
GO

/* ---------- 3. Inmutabilidad ----------
   Nadie edita ni borra movimientos o bitácora, ni siquiera desde SSMS por
   accidente. Las pruebas automáticas limpian sus filas ZZTEST- desactivando
   el trigger DENTRO de una transacción (el DDL es transaccional en SQL Server). */
CREATE TRIGGER dbo.tr_movimientos_inmutable ON dbo.movimientos
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    THROW 50001, N'Kardex: la tabla movimientos es inmutable (no se permite UPDATE ni DELETE).', 1;
END
GO

CREATE TRIGGER dbo.tr_bitacora_inmutable ON dbo.bitacora
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    THROW 50001, N'Kardex: la tabla bitacora es inmutable (no se permite UPDATE ni DELETE).', 1;
END
GO

/* ---------- 4. Vistas ---------- */

-- Alerta de mínimos. La consulta Mercurio DIRECTO para su correo y le suma el
-- pendiente de Infor de su lado. Kardex no envía correos.
-- almacen: dónde está el item (ATI | MAKRO), para que el correo diga a qué nave
-- le falta. Va AL FINAL a propósito: las columnas de antes no cambian de lugar.
CREATE VIEW dbo.vw_items_bajo_minimo
AS
SELECT
    i.numero_parte,
    i.descripcion,
    i.marca_modelo_uso,
    i.stock_activo       AS activo,
    i.inventario_minimo  AS minimo,
    i.inventario_maximo  AS maximo,
    i.almacen
FROM dbo.items AS i
WHERE i.obsoleto = 0
  AND i.inventario_minimo > 0
  AND i.stock_activo <= i.inventario_minimo;
GO

-- Items cuyo cache stock_activo no coincide con la suma de sus movimientos.
-- SIEMPRE debe regresar 0 filas.
CREATE VIEW dbo.vw_chequeo_stock
AS
SELECT
    i.id                              AS item_id,
    i.numero_parte,
    i.stock_activo,
    ISNULL(m.suma, 0)                 AS suma_movimientos,
    i.stock_activo - ISNULL(m.suma, 0) AS diferencia
FROM dbo.items AS i
LEFT JOIN (
    SELECT item_id, SUM(CAST(cantidad AS BIGINT)) AS suma
    FROM dbo.movimientos
    GROUP BY item_id
) AS m ON m.item_id = i.id
WHERE i.stock_activo <> ISNULL(m.suma, 0);
GO

/* ---------- 5. Carga inicial (Excel "stock inicial kardex.xlsx", hoja Sheet1) ----------
   Cada item entra con su ALMACEN, creado_por = 'admin' y stock_activo = STOCK;
   de ahí se derivan, en la misma transacción, una fila ALTA_ITEM por item en
   la bitácora y un AJUSTE "Carga inicial" (en el almacén del item) por cada
   STOCK > 0. */
SET XACT_ABORT ON;
BEGIN TRY
    BEGIN TRANSACTION;

    DECLARE @ahora DATETIME2(0) = SYSUTCDATETIME();
    -- <<CARGA_ESPERADO>>
    -- Generado por sql/generar_carga_inicial.py; no editar a mano.
    DECLARE @esperado_items INT = 176, @esperado_ati INT = 144, @esperado_makro INT = 32, @esperado_movs INT = 159;
    -- <</CARGA_ESPERADO>>

    INSERT INTO dbo.items
        (numero_parte, almacen, descripcion, marca_modelo_uso, unidad_medida,
         inventario_minimo, inventario_maximo, stock_activo, creado_por, creado_en)
    VALUES
    -- <<CARGA_INICIAL>>
    -- Generado por sql/generar_carga_inicial.py; no editar a mano.
    -- (numero_parte, almacen, descripcion, marca_modelo_uso, unidad_medida, minimo, maximo, STOCK, creado_por, creado_en)
    (N'LUB00001', N'MAKRO', N'ENSOLV TAMBOR DE 250.00 KG. C/U', N'BROMURO PARA DEGREASER(VIEJO EN PLANTA AL)', N'EACH', 12, 24, 19, N'admin', @ahora),
    (N'LUB00011', N'MAKRO', N'FINWORKS 3450', N'ACEITE PARA LEG CELL ALUMINIO', N'EACH', 8, 8, 2, N'admin', @ahora),
    (N'LUB00021', N'MAKRO', N'FINWORKS 3470', N'ACEITE PARA TUBO COBRE', N'EACH', 8, 16, 12, N'admin', @ahora),
    (N'LUB00040', N'MAKRO', N'PERCLOROETILENO (PROCE CLEAN K5) TAMBO 200 LITROS', N'PROCE CLEAN PARA DEGREASER(NUEVO)', N'EACH', 16, 32, 10, N'admin', @ahora),
    (N'LUB00025', N'MAKRO', N'FINWORKS ALTL 3100*', N'ACEITE PARA TUBO ALUMINIO', N'EACH', 8, 16, 8, N'admin', @ahora),
    (N'LUB00051', N'MAKRO', N'DISOLVENTE DE LIMPIEZA NICCACLEAR L-189', NULL, N'EACH', 300, 600, 666, N'admin', @ahora),
    (N'EMB00010', N'MAKRO', N'PELICULA ELASTICA CAL 70 15*1500', NULL, N'EACH', 200, 300, 288, N'admin', @ahora),
    (N'EMB00015', N'MAKRO', N'BOLSA TRANSPARENTE BD 105 CM + 56 CM X 76 CM C200 ABIERTA PARTE MAS LARGA', NULL, N'EACH', 6000, 12000, 6200, N'admin', @ahora),
    (N'EMB00023', N'MAKRO', N'FLEJE DE POLIPROPILENO NEGRO 1/2" ANCHO X .015" GROSOR', NULL, N'EACH', 12, 24, 16, N'admin', @ahora),
    (N'EMB00030', N'MAKRO', N'BURBUJA ROLLO C/393 PIE', NULL, N'EACH', 40, 80, 52, N'admin', @ahora),
    (N'EMB00035', N'MAKRO', N'CAJA DE CARTON 12”X12”X9” 51BC', NULL, N'EACH', 100, 200, 104, N'admin', @ahora),
    (N'EMB00056', N'MAKRO', N'ETIQ BLANCA POLIPROPILENO MATTE 2.25” X 1.25” C/3" S/P 4000/RL', NULL, N'EACH', 200, 400, 1240, N'admin', @ahora),
    (N'EMB00065', N'MAKRO', N'CAJA CARTON 51BC HANDHOLE 42.00”X20.50”X7.00”', NULL, N'EACH', 2000, 4000, 4070, N'admin', @ahora),
    (N'EMB00066', N'MAKRO', N'SEPARADOR PAD 29C / 20.08” X40.20”', NULL, N'EACH', 8000, 16000, 1000, N'admin', @ahora),
    (N'EMB00067', N'MAKRO', N'SEPARADOR FORMA Z 29C/ 61.61"X6.38"', NULL, N'EACH', 4000, 8000, 5150, N'admin', @ahora),
    (N'EMB00068', N'MAKRO', N'ESQUINERO 2.5"X2.5"X42.5" CAL 120', NULL, N'EACH', 2500, 5000, 7500, N'admin', @ahora),
    (N'EMB00069', N'MAKRO', N'CAJA DE CARTON 42.00”X20.50”X7.00” 51BC', NULL, N'EACH', 2500, 5000, 1299, N'admin', @ahora),
    (N'EMB00075', N'MAKRO', N'TARIMA 48X48" SIN TRATAMIENTO', NULL, N'EACH', 0, NULL, 0, N'admin', @ahora),
    (N'EMB00078', N'MAKRO', N'TARIMA 42" X 42" SIN TRATAMIENTO', NULL, N'EACH', 400, 800, 0, N'admin', @ahora),
    (N'EMB00080', N'MAKRO', N'CAJA RSC 48"X22"X7" 51KBC', NULL, N'EACH', 50, 100, 150, N'admin', @ahora),
    (N'EMB00082', N'MAKRO', N'TARIMA 48X48" TRATAMIENTO TERMICO', NULL, N'EACH', 30, 60, 106, N'admin', @ahora),
    (N'EMB00088', N'MAKRO', N'CAJA DE CARTON 51BC 24X15X7.5"', NULL, N'EACH', 300, 600, 264, N'admin', @ahora),
    (N'EMB00089', N'MAKRO', N'CAJA DE CARTON 51BC 30X19X6"', NULL, N'EACH', 800, 1600, 0, N'admin', @ahora),
    (N'EMB00095', N'MAKRO', N'CAJA DE CARTON 42X20.5X8" 51BC', NULL, N'EACH', 100, 200, 920, N'admin', @ahora),
    (N'EMB00114', N'MAKRO', N'TARIMA 42" X 42" CON TRATAMIENTO', NULL, N'EACH', 150, 300, 144, N'admin', @ahora),
    (N'EMB00126', N'MAKRO', N'CAJA RSC 30X11X5'''' 51KBC', NULL, N'EACH', 300, 600, 0, N'admin', @ahora),
    (N'EMB00130', N'MAKRO', N'SEPARADOR PAD 50*P” 70*26', NULL, N'EACH', 1500, 3000, 0, N'admin', @ahora),
    (N'EMB00135', N'MAKRO', N'CAJA 48x11x5 INCH,', NULL, N'EACH', 200, 300, 198, N'admin', @ahora),
    (N'EMB00136', N'MAKRO', N'CAJA 66x12x6 INCH.', NULL, N'EACH', 50, 100, 50, N'admin', @ahora),
    (N'EMB00137', N'MAKRO', N'SEPARADOR 47.3x10.3 INCH', NULL, N'EACH', 800, 900, 0, N'admin', @ahora),
    (N'EMB00138', N'MAKRO', N'SEPARADOR 66.3x8 INCH', NULL, N'EACH', 1500, 1600, 1450, N'admin', @ahora),
    (N'EMB00139', N'MAKRO', N'SEPARADOR 65.3x11.3 INCH,', NULL, N'EACH', 200, 300, 200, N'admin', @ahora),
    (N'EMB00001', N'ATI', N'5 X 10" BOLSAS DE POLIETILENO INDUSTRIALES C/1000', NULL, N'EACH', 20, 40, 13, N'admin', @ahora),
    (N'EMB00020', N'ATI', N'ETIQUETA BLANCA 4" X1" SEMI-GLOSS ROLLO C/5000', NULL, N'EACH', 30, 60, 78, N'admin', @ahora),
    (N'EMB00021', N'ATI', N'ETIQUETA BLANCA 4" X6" TRANSFERENCIA TERMICA C/1000', NULL, N'EACH', 48, 96, 140, N'admin', @ahora),
    (N'EMB00022', N'ATI', N'RIBBON CERA 110X450 MTS', NULL, N'EACH', 30, 60, 52, N'admin', @ahora),
    (N'EMB00028', N'ATI', N'CINTA CANELA 48MM*150M MCA TUK 179', NULL, N'EACH', 200, 400, 156, N'admin', @ahora),
    (N'EMB00038', N'ATI', N'RIBBON CERA 60X450 MTS', NULL, N'EACH', 30, 60, 45, N'admin', @ahora),
    (N'EMB00040', N'ATI', N'GRAPAS DE 1/2 PARA FLEJE C/1000', NULL, N'EACH', 5, 10, 3, N'admin', @ahora),
    (N'EMB00104', N'ATI', N'BOLSA 8 X 11"', NULL, N'EACH', 10, 20, 10, N'admin', @ahora),
    (N'EMB00117', N'ATI', N'ETIQUETA BLANCA MEDIDA 1.5"X.5" BOPP MATE C/3" S/P', NULL, N'EACH', 5, 10, 14, N'admin', @ahora),
    (N'EMB00118', N'ATI', N'RIBBON RESINA 110X450 MTS', NULL, N'EACH', 30, 60, 0, N'admin', @ahora),
    (N'EMB00121', N'ATI', N'ETIQUETA 2.25”X1.25” TRANS. TERMICA C/3” C/4000 VERDE', NULL, N'EACH', 12, 24, 19, N'admin', @ahora),
    (N'EMB00122', N'ATI', N'ETIQUETA 2.25”X1.25” TRANS. TERMICA C/3” C/4000 AZUL CELESTE', NULL, N'EACH', 12, 24, 53, N'admin', @ahora),
    (N'EMB00123', N'ATI', N'ETIQUETA 2.25”X1.25” TRANS. TERMICA C/3” C/4000 AMARILLA', NULL, N'EACH', 6, 12, 10, N'admin', @ahora),
    (N'EMB00124', N'ATI', N'ETIQUETA 2.25”X1.25” TRANS. TERMICA C/3” C/4000 GRIS', NULL, N'EACH', 6, 12, 9, N'admin', @ahora),
    (N'EMB00057', N'ATI', N'SELLO DE SEGURIDAD PARA TRAILER (H-435BLU)', N'SELLO DE SEGURIDAD PARA CAJAS CERRADA 53', N'EACH', 100, 150, 100, N'admin', @ahora),
    (N'OFI00151', N'ATI', N'TK-1175', N'Tóner para impresora ECOSYS M2640idw', N'EACH', 4, 8, 6, N'admin', @ahora),
    (N'OFI00401', N'ATI', N'TK-5442 NEGRO', N'Toner para impresora ECOSYS MA2100CWFX', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00402', N'ATI', N'TK-5442 MAGENTA', N'Toner para impresora ECOSYS MA2100CWFX', N'EACH', 2, 4, 1, N'admin', @ahora),
    (N'OFI00403', N'ATI', N'TK-5442 CYAN', N'Toner para impresora ECOSYS MA2100CWFX', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00404', N'ATI', N'TK-5442 AMARILLO', N'Toner para impresora ECOSYS MA2100CWFX', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00370', N'ATI', N'TK-5197 NEGRO', N'Toner para impresora TASKALFA 308ci', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00368', N'ATI', N'TK-5197 MAGENTA', N'Toner para impresora TASKALFA 308ci', N'EACH', 2, 4, 4, N'admin', @ahora),
    (N'OFI00369', N'ATI', N'TK-5197 CYAN', N'Toner para impresora TASKALFA 308ci', N'EACH', 2, 7, 4, N'admin', @ahora),
    (N'OFI00367', N'ATI', N'TK-5197 AMARILLO', N'Toner para impresora TASKALFA 308ci', N'EACH', 2, 4, 4, N'admin', @ahora),
    (N'OFI00306', N'ATI', N'TK-3182', N'Toner para impresora ECOSYS M3655IDN', N'EACH', 3, 6, 3, N'admin', @ahora),
    (N'OFI03432', N'ATI', N'TK-3432', N'Toner para impresora ECOSYS MA5500IX', N'EACH', 3, 6, 3, N'admin', @ahora),
    (N'OFI00358', N'ATI', N'TK-7127', N'Toner para impresora TASKALFA 3112i', N'EACH', 3, 6, 3, N'admin', @ahora),
    (N'OFI00365', N'ATI', N'TK-3402', N'Toner para impresora ECOSYS MA4500IX', N'EACH', 4, 8, 3, N'admin', @ahora),
    (N'OFI00350', N'ATI', N'TK-5242Y', N'Toner para impresora ECOSYS M5526CDW', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00349', N'ATI', N'TK-5242M', N'Toner para impresora ECOSYS M5526CDW', N'EACH', 2, 4, 1, N'admin', @ahora),
    (N'OFI00347', N'ATI', N'TK-5242K NEGRO', N'Toner para impresora ECOSYS M5526CDW', N'EACH', 2, 4, 2, N'admin', @ahora),
    (N'OFI00348', N'ATI', N'TK-5242C', N'Toner para impresora ECOSYS M5526CDW', N'EACH', 2, 4, 0, N'admin', @ahora),
    (N'OFI058D4', N'ATI', N'58D4H00', N'Toner Lexmark Negro alto rendimiento MX826 15k', N'EACH', 1, 2, 3, N'admin', @ahora),
    (N'EMB00076', N'ATI', N'SELLOS DE PLÁSTICO PARA TRÁILERS (ROJO O AZUL)', N'SELLO DE SEGURIDAD ULINE H-543R UNIDADES 3.5 TON', N'EACH', 300, 500, 0, N'admin', @ahora),
    (N'EMB00097', N'ATI', N'PELICULA ELASTICA AMARILLO CAL 60', N'STRETCH FILM COLOR', N'EACH', 4, 12, 6, N'admin', @ahora),
    (N'OFI00204', N'ATI', N'HOJA DE MAQUINA DE COLOR VERDE NEON PAQ 500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 7, N'admin', @ahora),
    (N'OFI00205', N'ATI', N'HOJA DE MAQUINA DE COLOR AMARILLO NEON PAQ 500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 6, N'admin', @ahora),
    (N'OFI00206', N'ATI', N'HOJA DE MAQUINA DE COLOR NARANJADO NEON PAQ500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 5, N'admin', @ahora),
    (N'OFI00207', N'ATI', N'HOJA DE MAQUINA DE COLOR CELESTE PASTEL PAQ 500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 11, N'admin', @ahora),
    (N'OFI00208', N'ATI', N'HOJA DE MAQUINA DE COLOR ROSA PASTEL PAQ 500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 6, N'admin', @ahora),
    (N'OFI00209', N'ATI', N'HOJA DE MAQUINA DE COLOR AMARILLO PASTEL PAQ 500', N'HOJA MAQUINA COLOR', N'PACKS', 4, 8, 8, N'admin', @ahora),
    (N'MAN00150', N'ATI', N'CINCHOS 6"', N'COLGAR ID EN CONTENEDORES', N'PACKS', 500, 1000, 1000, N'admin', @ahora),
    (N'DOT01', N'ATI', N'ETIQUETA TIPO DOT REDONDA AMARILLA', N'ROLLO CON 1000 PZ', N'ROLLO', 1, 3, 10, N'admin', @ahora),
    (N'DOT02', N'ATI', N'ETIQUETA TIPO DOT REDONDA NARANJA', N'ROLLO CON 1000 PZ', N'ROLLO', 1, 3, 2, N'admin', @ahora),
    (N'DOT03', N'ATI', N'ETIQUETA TIPO DOT REDONDA CELESTE', N'ROLLO CON 1000 PZ', N'ROLLO', 1, 3, 2, N'admin', @ahora),
    (N'EMB00140', N'ATI', N'ETIQUETA DE PAPEL TERMICO DIRECTO 4X3”', N'ROLLO C/100', N'ROLLO', 200, 400, 200, N'admin', @ahora),
    (N'EMB00141', N'ATI', N'ETIQUETA BLANCA 4" X3" TRANSFERENCIA TERMICA', N'ROLLO C/2000', N'ROLLO', 6, 12, 6, N'admin', @ahora),
    (N'OFIW1105A', N'ATI', N'Toner', N'HP laser W1105A', N'EACH', 15, 20, 16, N'admin', @ahora),
    (N'DR920', N'ATI', N'TN920UXXL TONER', NULL, N'EACH', 1, 2, 0, N'admin', @ahora),
    (N'13725037AL', N'ATI', N'0.625 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (5/8)', N'EACH', 30, 60, 22, N'admin', @ahora),
    (N'13432037AL', N'ATI', N'0.750 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (3/4)', N'EACH', 30, 60, 22, N'admin', @ahora),
    (N'18272037AL', N'ATI', N'0.875 X 0.075 X 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (7/8) (FOR .075" THICK)', N'EACH', 10, 20, 57, N'admin', @ahora),
    (N'13734037AL', N'ATI', N'0.875 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (7/8)', N'EACH', 30, 60, 54, N'admin', @ahora),
    (N'HER00034', N'ATI', N'PUNZON LPX 50 S300 A2 P.3550', N'PUNZON HOLE PUNCH', N'EACH', 20, 40, 6, N'admin', @ahora),
    (N'MAN00586', N'ATI', N'BLAST TIP PARA CU/AL BRAZER', N'CU/AL TORCH TIPS FOR CU/AL BRAZING', N'EACH', 40, 80, 61, N'admin', @ahora),
    (N'MANDREL 3/4', N'ATI', N'MANDREL MO1365 3 .667', N'3/4', N'EACH', 2, 6, 13, N'admin', @ahora),
    (N'MANDREL3/8', N'ATI', N'MANDREL', N'3/8', N'EACH', 2, 6, 14, N'admin', @ahora),
    (N'MANDREL 7/8', N'ATI', N'MANDREL', N'7/8', N'EACH', 2, 6, 7, N'admin', @ahora),
    (N'HER00170', N'ATI', N'PUNZON LP X50 S300 P.1340', N'PUNZON HOLE PUNCH', N'EACH', 5, 15, 23, N'admin', @ahora),
    (N'13754037', N'ATI', N'HOLE PUNCH MANDREL', N'3/4', N'EACH', 10, 20, 67, N'admin', @ahora),
    (N'13764037', N'ATI', N'HOLE PUNCH MANDREL', N'5/8', N'EACH', 10, 20, 64, N'admin', @ahora),
    (N'18273037', N'ATI', N'HOLE PUNCH MANDREL', N'7/8', N'EACH', 5, 10, 46, N'admin', @ahora),
    (N'MAN00096', N'ATI', N'BROCA 3.50 MM P/INOX. IZAR', N'01-957-010 BROCA 3.5MM IZAR', N'EACH', 50, 100, 96, N'admin', @ahora),
    (N'HER00007', N'ATI', N'15DA30950 1/20', N'NAVAJA CORTADOR DE TUBO', N'EACH', 100, 130, 95, N'admin', @ahora),
    (N'HER00119', N'ATI', N'EXTERNAL CUTTER Φ32.2*Φ19*10.2*18', N'CFB CUTTER', N'EACH', 150, 260, 79, N'admin', @ahora),
    (N'HER00219', N'ATI', N'EXTERNAL CUTTER Φ32.2*Φ15*10.2*18', NULL, N'EACH', 50, 100, 91, N'admin', @ahora),
    (N'HL21110240098001', N'ATI', N'PUNZÓN', N'T-DRILL 3/4', N'EACH', 10, 20, 67, N'admin', @ahora),
    (N'HL21110240099001', N'ATI', N'PUNZÓN', N'T-DRILL 7/8', N'EACH', 10, 20, 6, N'admin', @ahora),
    (N'20320037', N'ATI', N'BALA 3/8 PUNCH COLLAR', N'0.375" X .028" X 0.281" (0.159")', N'EACH', 50, 100, 80, N'admin', @ahora),
    (N'MO1650', N'ATI', N'PUNZÓN HOLE PUNCH', N'0.281"', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'20318037', N'ATI', N'BALA', N'0.500" X 0.028" X 0.281"', N'EACH', 10, 20, 46, N'admin', @ahora),
    (N'MO1651', N'ATI', N'PUNZÓN HOLE PUNCH', N'0.368"', N'EACH', 10, 20, 57, N'admin', @ahora),
    (N'MO1669', N'ATI', N'PUNZÓN PUNCH COLLAR', N'0.156" X 0.281"', N'EACH', 10, 20, 138, N'admin', @ahora),
    (N'19448037', N'ATI', N'BALA HOLE PUNCH', N'0.875" X 0.045" X 0.281"', N'EACH', 10, 20, 20, N'admin', @ahora),
    (N'20273037', N'ATI', N'BALA HOLE PUNCH', N'0.750" X 0.035" X 0.281"', N'EACH', 20, 40, 34, N'admin', @ahora),
    (N'19911037AL', N'ATI', N'MANDRELL GOODMAN 7/8', NULL, N'EACH', 10, 20, 26, N'admin', @ahora),
    (N'15205037AL', N'ATI', N'MANDRELL ALLIED AIR 3/4', NULL, N'EACH', 10, 20, 37, N'admin', @ahora),
    (N'15326037AL', N'ATI', N'MANDRELL ALLIED AIR 5/8', NULL, N'EACH', 10, 20, 1, N'admin', @ahora),
    (N'16815037AL', N'ATI', N'MANDRELL ALLIED AIR 7/8', NULL, N'EACH', 10, 20, 21, N'admin', @ahora),
    (N'17214037AL', N'ATI', N'MANDRELL CVL 7/8', NULL, N'EACH', 10, 20, 47, N'admin', @ahora),
    (N'19529037AL', N'ATI', N'MANDRELL 5/8', NULL, N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'19020037AL', N'ATI', N'MANDRELL 1/2', NULL, N'EACH', 10, 20, 81, N'admin', @ahora),
    (N'MANDREL 1/2', N'ATI', N'MANDREL', N'1/2', N'EACH', 2, 6, 51, N'admin', @ahora),
    (N'MANDRIL 5/8', N'ATI', N'MANDREL', N'5/8', N'EACH', 2, 6, 14, N'admin', @ahora),
    (N'20682037AL', N'ATI', N'BALA 1/2 PARA HORNO', NULL, N'EACH', 20, 50, 0, N'admin', @ahora),
    (N'MO1533', N'ATI', N'FORMADOR', NULL, N'EACH', 10, 10, 20, N'admin', @ahora),
    (N'20682029AL', N'ATI', N'PUNZON 0.269', NULL, N'EACH', 30, 50, 66, N'admin', @ahora),
    (N'100128037AL', N'ATI', N'MANDRELL DE RHEEM', N'PARA HORNO', N'EACH', 20, 30, 53, N'admin', @ahora),
    (N'100129029AL', N'ATI', N'PUNZON PARA RHEEM', N'HORNO', N'EACH', 20, 30, 72, N'admin', @ahora),
    (N'20709029', N'ATI', N'PUNZON', NULL, N'EACH', 20, 20, 55, N'admin', @ahora),
    (N'15431B038', N'ATI', N'PUNCH TOOL FOR TUBE 0.313 (5/16)', NULL, N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'23462B038', N'ATI', N'PUNCH TOOL FOR TUBE 0.500 (1/2)', NULL, N'EACH', 10, 20, 15, N'admin', @ahora),
    (N'23431B139', N'ATI', N'MANDRELL TOOL (BULLET) FOR TUBE 0.313 (5-16)', NULL, N'EACH', 50, 50, 100, N'admin', @ahora),
    (N'23462B139', N'ATI', N'MANDRELL TOOL (BULLET) FOR TUBE 0.500 (1/2)', NULL, N'EACH', 50, 100, 90, N'admin', @ahora),
    (N'20412029CAL', N'ATI', N'TAPER X 0.373 O.D.SWAGE PUNCH', NULL, N'EACH', 5, 10, 30, N'admin', @ahora),
    (N'20412029AL', N'ATI', N'0.350 O.D. ON 0.375 SWAGE PUNCH', NULL, N'EACH', 5, 10, 39, N'admin', @ahora),
    (N'10594037', N'ATI', N'BALA', N'MANDRIL', N'EACH', 10, 20, 25, N'admin', @ahora),
    (N'22099037AL', N'ATI', N'BALA PARA HORNO 3/4"', NULL, N'EACH', 10, 20, 41, N'admin', @ahora),
    (N'19032037AL', N'ATI', N'0.375 X 0.042 X 0.256 ALUM. MANDREL', N'MANDREL', N'EACH', 10, 20, 80, N'admin', @ahora),
    (N'BROCA3.3mm(ST-5-163-095)', N'ATI', N'0.129', N'3.3mm', N'EACH', 10, 20, 10, N'admin', @ahora),
    (N'BROCA#29(ST-5-170-145)', N'ATI', N'BROCA #29 0.136', N'#29', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'BROCA#11(ST-5-170-055)', N'ATI', N'BROCA #11 0.191', N'#11', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'BROCA#8(ST-5-170-040)', N'ATI', N'BROCA #8 0.199', N'#8', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'BROCA1/4(ST-5-160-080)', N'ATI', N'BROCA 1/4 0.25', N'1/4', N'EACH', 20, 40, 22, N'admin', @ahora),
    (N'BROCA6.4mm(ST-5-163-188)', N'ATI', N'BROCA 6.4 MM 0.252', N'6.4mm', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'BROCA17/64(ST-5-160-085)', N'ATI', N'BROCA 17/64 0.266', N'17/64', N'EACH', 20, 40, 41, N'admin', @ahora),
    (N'BROCA9/32(ST-5-160-090)', N'ATI', N'BROCA 9/32 0.2812', N'9|32', N'EACH', 15, 30, 25, N'admin', @ahora),
    (N'BROCAO(ST-5-180-075)', N'ATI', N'BROCA 0 0.316', N'O', N'EACH', 10, 20, 17, N'admin', @ahora),
    (N'BROCA21/64(ST-5-160-105)', N'ATI', N'BROCA 21/64 0.3281', N'21/64', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'BROCAV(ST-5-180-110)', N'ATI', N'BROCA V 0.377', N'V', N'EACH', 15, 30, 15, N'admin', @ahora),
    (N'BROCA9.7mm', N'ATI', N'BROCA 9.7MM 0.3819', N'9.7mm', N'EACH', 10, 20, 17, N'admin', @ahora),
    (N'BROCA6.5mm(ST-5-163-190)', N'ATI', N'BROCA 6.5MM 0.256', N'6.5mm', N'EACH', 10, 20, 4, N'admin', @ahora),
    (N'BROCA3/8"(ST-5-160-120)', N'ATI', N'BROCA 3/8 0.375', N'3/8"', N'EACH', 10, 20, 15, N'admin', @ahora),
    (N'21326B037AL', N'ATI', N'Mandrel 5/8', N'Carrier CVL', N'EACH', 10, 20, 6, N'admin', @ahora),
    (N'20365B037AL', N'ATI', N'Mandrel 7/8', N'Carrier CVL', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'100131B037AL', N'ATI', N'Mandrel 7/8', N'Rheem', N'EACH', 10, 20, 27, N'admin', @ahora),
    (N'BROCA5/16 7.93mm', N'ATI', N'BROCA 5/16 ST-5-180-015 (0,242") Weston', NULL, N'EACH', 10, 20, 0, N'admin', @ahora),
    (N'21670037CU', N'ATI', N'0.381 x 0.875 X 0.045 Hole punch', NULL, N'EACH', 5, 10, 0, N'admin', @ahora),
    (N'21257138CU', N'ATI', N'0.381 Hole Punch', NULL, N'EACH', 2, 5, 0, N'admin', @ahora),
    (N'ALTF-01CU', N'ATI', N'Aguja leakte', NULL, N'EACH', 10, 20, 0, N'admin', @ahora),
    (N'ST-5-170-005', N'ATI', N'broca wenston tools ST-5-170-005', N'Carrier F', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'MAN00142', N'ATI', N'FILTER CARTRIDGE FOR INFICON SL3000XL C/25', N'INFICON PART NUMBER 521-023', N'PACKS', 2, 4, 2, N'admin', @ahora),
    (N'MAN00738', N'ATI', N'CONTACT TIP (0.035 - 0.040) MILLER 206187', N'END WELDER 3 Y 4', N'EACH', 100, 200, 199, N'admin', @ahora),
    (N'MAN00739', N'ATI', N'CONTACT TIP TWECO 1452', N'END WELDER 1 Y 2', N'EACH', 20, 40, 0, N'admin', @ahora),
    (N'MAN00293', N'ATI', N'NOZZLE 5/8" TWECO WS 24CT62S', N'NOZZLE END WELDER 1 Y 2', N'EACH', 15, 30, 46, N'admin', @ahora),
    (N'MAN00620', N'ATI', N'TOBERA MILLER 198855', N'NOZZLE END WELDER 3 Y 4', N'EACH', 10, 20, 39, N'admin', @ahora),
    (N'MAN01095', N'ATI', N'DIFUSOR M25 GENERICO 169-728', N'169728', N'EACH', 5, 10, 9, N'admin', @ahora),
    (N'FLUX0001', N'ATI', N'FLUX 028/55 CS2 PARA CARRIER', NULL, N'EACH', 20, 40, 83, N'admin', @ahora),
    (N'FLUX0002', N'ATI', N'FLUX 028 CS/D PARA LENNOX', NULL, N'EACH', 15, 30, 22, N'admin', @ahora),
    (N'OFI00004', N'ATI', N'CAJA DE PAPEL BLANCO CARTA 75GR PAQ DE 500', N'HOJA CARTA PARA IMPRESORA', N'BOX', 20, 40, 6, N'admin', @ahora),
    (N'GEN00001', N'ATI', N'TRAPO INDUSTRIAL MULTICOLOR', N'COSTAL DE TRAPOS BOLSA DE 20KGS', N'KGS', 800, 1600, 690, N'admin', @ahora),
    (N'GEN00002', N'ATI', N'TOALLA DE TALLER (AZUL)', N'ROLLOS SERVILLETAS AZULES', N'BOX', 15, 30, 13, N'admin', @ahora),
    (N'LUB00008', N'ATI', N'ALCOHOL DESNATURALIZADO', N'ALCOHOL MEDICO 96% BOTELLA DE 1L', N'EACH', 120, 240, 120, N'admin', @ahora),
    (N'SOL00025', N'ATI', N'GAS FLUX FUNDENTE LIQUIDO CUBETA 19L', N'GASFLUX PARA SOLDADURA MANUAL DE COBRE', N'EACH', 30, 60, 7, N'admin', @ahora),
    (N'MAN00509', N'ATI', N'PUNTA MULTIFLAMA UNIMETAL # 13715', N'UNIMETAL/SOLDADURA MANUAL', N'EACH', 20, 40, 15, N'admin', @ahora),
    (N'MAN00970', N'ATI', N'9452K167 MC MASTER', N'O-RING 3/32 PARA TXV FIXTURE', N'EACH', 200, 400, 300, N'admin', @ahora),
    (N'LUB00039', N'ATI', N'LOCTITE 8009 ANTIAFERRANTE PARA TRABAJO PESADO', N'LOCTITE LB 8009', N'EACH', 3, 6, 5, N'admin', @ahora),
    (N'MAN01831', N'ATI', N'WELDING NOZZLE LPW-4 (PRESENTACION CHINA)', N'PUNTA PARA AUTOBRAZER EN COBRE', N'EACH', 10, 30, 0, N'admin', @ahora),
    (N'MAN01825', N'ATI', N'WELDING TORCH KIT M8 DOUBLE HEADED WITH HOSE', N'JUEGO COMPLETO DE ANTORCHA PARA SOLDAR', N'EACH', 10, 20, 2, N'admin', @ahora),
    (N'MAN00120', N'ATI', N'BLAST TIP 1542D', N'PUNTA PARA AUTOBRAZER EN ALUMINIO', N'EACH', 150, 300, 315, N'admin', @ahora),
    (N'LUB00052', N'ATI', N'INHIBIDOR DE INSCRUSTACIONES ZW-0100 (galon)', NULL, N'EACH', 75, 150, 60, N'admin', @ahora),
    (N'GEN00025', N'ATI', N'HOJA DE SILICON', N'PARA LEAK TESTER', N'EACH', 30, 60, 54, N'admin', @ahora),
    (N'FLUX NOCOLOK', N'ATI', N'Nocolok wet stantard flux 25 kg/ bolsa', NULL, N'KGS', 50, 250, 150, N'admin', @ahora),
    (N'FLUX FB401L', N'ATI', N'FLUX FB401L (USO EN TXV', NULL, N'EACH', 16, 30, 58, N'admin', @ahora)
    -- <</CARGA_INICIAL>>
    ;

    INSERT INTO dbo.bitacora (item_id, evento, valor_nuevo, motivo, usuario_nomina, fecha_hora)
    SELECT id, N'ALTA_ITEM', numero_parte, N'Carga inicial', N'admin', @ahora
    FROM dbo.items;

    INSERT INTO dbo.movimientos (item_id, tipo, almacen, cantidad, stock_resultante, usuario_nomina, fecha_hora, motivo)
    SELECT id, N'AJUSTE', almacen, stock_activo, stock_activo, N'admin', @ahora, N'Carga inicial'
    FROM dbo.items
    WHERE stock_activo > 0;

    -- Verificación contra lo que contó el generador: si no cuadra, se revierte TODA la carga.
    DECLARE @items INT      = (SELECT COUNT(*) FROM dbo.items);
    DECLARE @ati INT        = (SELECT COUNT(*) FROM dbo.items WHERE almacen = N'ATI');
    DECLARE @makro INT      = (SELECT COUNT(*) FROM dbo.items WHERE almacen = N'MAKRO');
    DECLARE @movs INT       = (SELECT COUNT(*) FROM dbo.movimientos);
    DECLARE @descuadres INT = (SELECT COUNT(*) FROM dbo.vw_chequeo_stock);
    IF @items <> @esperado_items OR @ati <> @esperado_ati OR @makro <> @esperado_makro
       OR @movs <> @esperado_movs OR @descuadres <> 0
    BEGIN
        DECLARE @msg NVARCHAR(400) = CONCAT(
            N'Verificación falló: items=', @items, N' (esperado ', @esperado_items,
            N'; ATI=', @ati, N'/', @esperado_ati, N', MAKRO=', @makro, N'/', @esperado_makro,
            N'), movimientos=', @movs, N' (esperado ', @esperado_movs,
            N'), filas en vw_chequeo_stock=', @descuadres, N' (esperado 0).');
        THROW 50002, @msg, 1;
    END

    COMMIT TRANSACTION;
    PRINT CONCAT(N'OK carga inicial: ', @items, N' items, ', @movs, N' movimientos, ',
                 @descuadres, N' filas en vw_chequeo_stock.');
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    DECLARE @err NVARCHAR(2048) = ERROR_MESSAGE();
    RAISERROR(N'ALTO: la carga inicial se revirtió completa. %s', 16, 1, @err);
    SET NOEXEC ON;  -- no seguir con los pasos siguientes
END CATCH
GO

/* ---------- 6. Usuarios: se reusa la tabla de Dossier ----------  
   rol_kardex NULL = sin acceso a Kardex. IT da de alta usuarios y resetea
   contraseñas directo en la BD. Idempotente: si la columna o el CHECK ya
   existen (intento anterior), no se tocan. Va con EXEC() para que el batch
   compile aunque la columna todavía no exista. */
USE dossier_local;
GO
IF COL_LENGTH(N'dbo.usuarios', N'rol_kardex') IS NULL
    ALTER TABLE dbo.usuarios ADD rol_kardex NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints
               WHERE parent_object_id = OBJECT_ID(N'dbo.usuarios') AND name = N'CK_usuarios_rol_kardex')
    EXEC (N'ALTER TABLE dbo.usuarios ADD CONSTRAINT CK_usuarios_rol_kardex
            CHECK (rol_kardex IS NULL OR rol_kardex IN (N''almacenista'', N''supervisor''))');
GO
EXEC (N'UPDATE dbo.usuarios SET rol_kardex = N''supervisor'' WHERE username = N''admin''');
IF NOT EXISTS (SELECT 1 FROM dbo.usuarios WHERE username = N'admin')
    RAISERROR(N'AVISO: no existe el usuario admin en dossier_local.dbo.usuarios; nadie es supervisor todavía.', 10, 1);
GO

/* ---------- Resumen final (solo corre si todo lo anterior salió bien) ---------- */
EXEC (N'SELECT
    (SELECT COUNT(*) FROM kardex.dbo.items)            AS items,
    (SELECT COUNT(*) FROM kardex.dbo.movimientos)      AS movimientos,
    (SELECT COUNT(*) FROM kardex.dbo.bitacora)         AS bitacora,
    (SELECT COUNT(*) FROM kardex.dbo.vw_chequeo_stock) AS filas_chequeo_stock,
    (SELECT COUNT(*) FROM dossier_local.dbo.usuarios WHERE rol_kardex IS NOT NULL) AS usuarios_con_kardex');
GO

SET NOEXEC OFF;
GO
