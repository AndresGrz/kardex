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
     5. Carga inicial del Excel "tools .xlsx" en UNA transacción, con
        verificación: 72 items, 66 movimientos y 0 filas en vw_chequeo_stock.
        Si algo no cuadra, se revierte completa.
     6. Columna rol_kardex en dossier_local.dbo.usuarios; admin = supervisor.

   El bloque de la carga inicial (entre los marcadores <<CARGA_INICIAL>>)
   lo genera sql/generar_carga_inicial.py leyendo el Excel. No se edita a mano.

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
    motivo          NVARCHAR(500) NULL,      -- motivo opcional de OBSOLETO / REVERTIR_OBSOLETO
    usuario_nomina  NVARCHAR(50)  NOT NULL,
    fecha_hora      DATETIME2(0)  NOT NULL CONSTRAINT DF_bitacora_fecha DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT CK_bitacora_evento CHECK (evento IN (N'ALTA_ITEM', N'EDICION_ITEM', N'OBSOLETO', N'REVERTIR_OBSOLETO',
                                                    N'ASIGNAR_TRANSITO', N'QUITAR_TRANSITO')),
    CONSTRAINT CK_bitacora_campo  CHECK (evento <> N'EDICION_ITEM' OR campo IS NOT NULL)
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
CREATE VIEW dbo.vw_items_bajo_minimo
AS
SELECT
    i.numero_parte,
    i.descripcion,
    i.marca_modelo_uso,
    i.stock_activo       AS activo,
    i.inventario_minimo  AS minimo,
    i.inventario_maximo  AS maximo
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

/* ---------- 5. Carga inicial (Excel "tools .xlsx", hoja Sheet1) ----------
   Cada item entra con creado_por = 'admin' y stock_activo = FISICO; de ahí se
   derivan, en la misma transacción, una fila ALTA_ITEM por item en la bitácora
   y un AJUSTE "Carga inicial" por cada FISICO > 0. */  
SET XACT_ABORT ON;
BEGIN TRY
    BEGIN TRANSACTION;

    DECLARE @ahora DATETIME2(0) = SYSUTCDATETIME();

    INSERT INTO dbo.items
        (numero_parte, descripcion, marca_modelo_uso, unidad_medida,
         inventario_minimo, inventario_maximo, stock_activo, creado_por, creado_en)
    VALUES
    -- <<CARGA_INICIAL>>
    -- Generado por sql/generar_carga_inicial.py; no editar a mano.
    -- (numero_parte, descripcion, marca_modelo_uso, unidad_medida, minimo, maximo, FISICO, creado_por, creado_en)
    (N'13725037AL', N'0.625 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (5/8)', N'EACH', 30, 60, 25, N'admin', @ahora),
    (N'13432037AL', N'0.750 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (3/4)', N'EACH', 30, 60, 23, N'admin', @ahora),
    (N'18272037AL', N'0.875 X 0.075 X 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (7/8) (FOR .075" THICK)', N'EACH', 10, 20, 57, N'admin', @ahora),
    (N'13734037AL', N'0.875 X 0.065 TUBE 0.355 CHP MANDRELL', N'BALA HOLE PUNCH (7/8)', N'EACH', 30, 60, 54, N'admin', @ahora),
    (N'HER00034', N'PUNZON LPX 50 S300 A2 P.3550', N'PUNZON HOLE PUNCH', N'EACH', 20, 40, 10, N'admin', @ahora),
    (N'MAN00586', N'BLAST TIP PARA CU/AL BRAZER', N'CU/AL TORCH TIPS FOR CU/AL BRAZING', N'EACH', 40, 80, 61, N'admin', @ahora),
    (N'CFB-3/4', N'MANDREL MO1365 3 .667', N'3/4', N'EACH', 2, 6, 13, N'admin', @ahora),
    (N'CFB-3/8', N'MANDREL', N'3/8', N'EACH', 2, 6, 14, N'admin', @ahora),
    (N'CFB-7/8', N'MANDREL', N'7/8', N'EACH', 2, 6, 7, N'admin', @ahora),
    (N'HER00170', N'PUNZON LP X50 S300 P.1340', N'PUNZON HOLE PUNCH', N'EACH', 5, 15, 23, N'admin', @ahora),
    (N'14000000', N'HOLE PUNCH MANDREL', N'3/4', N'EACH', 10, 20, 69, N'admin', @ahora),
    (N'13764037', N'HOLE PUNCH MANDREL', N'5/8', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'18000000', N'HOLE PUNCH MANDREL', N'7/8', N'EACH', 5, 10, 46, N'admin', @ahora),
    (N'MAN00096', N'BROCA 3.50 MM P/INOX. IZAR', N'01-957-010 BROCA 3.5MM IZAR', N'EACH', 50, 100, 96, N'admin', @ahora),
    (N'HER00007', N'15DA30950 1/20', N'NAVAJA CORTADOR DE TUBO', N'EACH', 100, 130, 96, N'admin', @ahora),
    (N'HER00119', N'EXTERNAL CUTTER Φ32.2*Φ19*10.2*18', N'CFB CUTTER', N'EACH', 150, 260, 123, N'admin', @ahora),
    (N'HER00219', N'EXTERNAL CUTTER Φ32.2*Φ15*10.2*18', NULL, N'EACH', 50, 100, 102, N'admin', @ahora),
    (N'HL21110240098001', N'PUNZÓN', N'T-DRILL 3/4', N'EACH', 10, 20, 69, N'admin', @ahora),
    (N'HL21110240099001', N'PUNZÓN', N'T-DRILL 7/8', N'EACH', 10, 20, 6, N'admin', @ahora),
    (N'20000000-0.375" X .028" X 0.281" (0.159")', N'BALA 3/8 PUNCH COLLAR', N'0.375" X .028" X 0.281" (0.159")', N'EACH', 50, 100, 93, N'admin', @ahora),
    (N'MO1650', N'PUNZÓN HOLE PUNCH', N'0.281"', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'20000000-0.500" X 0.028" X 0.281"', N'BALA', N'0.500" X 0.028" X 0.281"', N'EACH', 10, 20, 48, N'admin', @ahora),
    (N'MO1651', N'PUNZÓN HOLE PUNCH', N'0.368"', N'EACH', 10, 20, 54, N'admin', @ahora),
    (N'MO1669', N'PUNZÓN PUNCH COLLAR', N'0.156" X 0.281"', N'EACH', 10, 20, 142, N'admin', @ahora),
    (N'19000000', N'BALA HOLE PUNCH', N'0.875" X 0.045" X 0.281"', N'EACH', 10, 20, 20, N'admin', @ahora),
    (N'20000000-0.750" X 0.035" X 0.281"', N'BALA HOLE PUNCH', N'0.750" X 0.035" X 0.281"', N'EACH', 20, 40, 32, N'admin', @ahora),
    (N'19911037AL', N'MANDRELL GOODMAN 7/8', NULL, N'EACH', 10, 20, 36, N'admin', @ahora),
    (N'15205037AL', N'MANDRELL ALLIED AIR 3/4', NULL, N'EACH', 10, 20, 35, N'admin', @ahora),
    (N'15326037AL', N'MANDRELL ALLIED AIR 5/8', NULL, N'EACH', 10, 20, 2, N'admin', @ahora),
    (N'16815037AL', N'MANDRELL ALLIED AIR 7/8', NULL, N'EACH', 10, 20, 21, N'admin', @ahora),
    (N'17214037AL', N'MANDRELL CVL 7/8', NULL, N'EACH', 10, 20, 47, N'admin', @ahora),
    (N'19529037AL', N'MANDRELL 5/8', NULL, N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'19020037AL', N'MANDRELL 1/2', NULL, N'EACH', 10, 20, 58, N'admin', @ahora),
    (N'BEND-1/2', N'MANDREL', N'1/2', N'EACH', 2, 6, 51, N'admin', @ahora),
    (N'BEND-5/8', N'MANDREL', N'5/8', N'EACH', 2, 6, 14, N'admin', @ahora),
    (N'20682037AL', N'BALA 1/2 PARA HORNO', NULL, N'EACH', 20, 50, 15, N'admin', @ahora),
    (N'MO1533', N'SIN DESCRIPCION', NULL, N'EACH', 10, 10, 20, N'admin', @ahora),
    (N'20682029AL', N'0.27', NULL, N'EACH', 30, 50, 66, N'admin', @ahora),
    (N'100128037AL', N'MANDRELL DE RHEEM', N'PARA HORNO', N'EACH', 20, 30, 54, N'admin', @ahora),
    (N'100129029AL', N'PUNZON PARA RHEEM', N'HORNO', N'EACH', 20, 30, 73, N'admin', @ahora),
    (N'21000000', N'SIN DESCRIPCION', NULL, N'EACH', 20, 20, 55, N'admin', @ahora),
    (N'15431B038', N'PUNCH TOOL FOR TUBE 0.313 (5/16)', NULL, N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'23462B038', N'PUNCH TOOL FOR TUBE 0.500 (1/2)', NULL, N'EACH', 10, 20, 16, N'admin', @ahora),
    (N'23431B139', N'MANDRELL TOOL (BULLET) FOR TUBE 0.313 (5-16)', NULL, N'EACH', 50, 50, 50, N'admin', @ahora),
    (N'23462B139', N'MANDRELL TOOL (BULLET) FOR TUBE 0.500 (1/2)', NULL, N'EACH', 50, 100, 90, N'admin', @ahora),
    (N'20412029CAL', N'TAPER X 0.373 O.D.SWAGE PUNCH', NULL, N'EACH', 5, 10, 30, N'admin', @ahora),
    (N'20412029AL', N'0.350 O.D. ON 0.375 SWAGE PUNCH', NULL, N'EACH', 5, 10, 39, N'admin', @ahora),
    (N'11000000', N'SIN DESCRIPCION', N'MANDRIL', N'EACH', 10, 20, 16, N'admin', @ahora),
    (N'22099037AL', N'BALA PARA HORNO 3/4"', NULL, N'EACH', 10, 20, 16, N'admin', @ahora),
    (N'19032037AL', N'0.375 X 0.042 X 0.256 ALUM. MANDREL', N'MANDREL', N'EACH', 10, 20, 40, N'admin', @ahora),
    (N'BROCA 3.3mm (ST-5-163-095)', N'0.129', N'3.3mm', N'EACH', 10, 20, 11, N'admin', @ahora),
    (N'BROCA #29(ST-5-170-145)', N'0.136', N'#29', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'BROCA #11(ST-5-170-055)', N'0.191', N'#11', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'BROCA #8(ST-5-170-040)', N'0.199', N'#8', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'BROCA 1/4(ST-5-160-080)', N'0.25', N'1/4', N'EACH', 20, 40, 25, N'admin', @ahora),
    (N'BROCA 6.4mm(ST-5-163-188)', N'0.252', N'6.4mm', N'EACH', 10, 20, 8, N'admin', @ahora),
    (N'BROCA 17/64(ST-5-160-085)', N'0.266', N'17/64', N'EACH', 20, 40, 43, N'admin', @ahora),
    (N'BROCA 9/32(ST-5-160-090)', N'0.2812', N'9/32', N'EACH', 15, 30, 25, N'admin', @ahora),
    (N'BROCA O(ST-5-180-075)', N'0.316', N'O', N'EACH', 10, 20, 17, N'admin', @ahora),
    (N'BROCA 21/64(ST-5-160-105)', N'0.3281', N'21/64', N'EACH', 10, 20, 24, N'admin', @ahora),
    (N'BROCA V(ST-5-180-110)', N'0.377', N'V', N'EACH', 15, 30, 15, N'admin', @ahora),
    (N'BROCA 9.7mm', N'0.3819', N'9.7mm', N'EACH', 10, 20, 17, N'admin', @ahora),
    (N'BROCA 6.5mm(ST-5-163-190)', N'0.256', N'6.5mm', N'EACH', 10, 20, 0, N'admin', @ahora),
    (N'BROCA 3/8"(ST-5-160-120)', N'0.375', N'3/8"', N'EACH', 10, 20, 15, N'admin', @ahora),
    (N'21326B037AL', N'Mandrel 5/8', N'Carrier CVL', N'EACH', 10, 20, 12, N'admin', @ahora),
    (N'20365B037AL', N'Mandrel 7/8', N'Carrier CVL', N'EACH', 10, 20, 22, N'admin', @ahora),
    (N'100131B037AL', N'Mandrel 7/8', N'Rheem', N'EACH', 10, 20, 27, N'admin', @ahora),
    (N'ST-5', N'ST-5-180-015', N'Broca letra "C" (0,242") Weston', N'EACH', 10, 20, 0, N'admin', @ahora),
    (N'21670037CU', N'0.381 x 0.875 X 0.045 Hole punch', NULL, N'EACH', 5, 10, 0, N'admin', @ahora),
    (N'21257138CU', N'0.381 Hole Punch', NULL, N'EACH', 2, 5, 0, N'admin', @ahora),
    (N'ALTF-01CU', N'Aguja leakte', NULL, N'EACH', 10, 20, 0, N'admin', @ahora),
    (N'ST-5-170-005', N'broca wenston tools ST-5-170-005', N'Carrier F', N'EACH', 10, 20, 0, N'admin', @ahora)
    -- <</CARGA_INICIAL>>
    ;

    INSERT INTO dbo.bitacora (item_id, evento, valor_nuevo, motivo, usuario_nomina, fecha_hora)
    SELECT id, N'ALTA_ITEM', numero_parte, N'Carga inicial', N'admin', @ahora
    FROM dbo.items;

    INSERT INTO dbo.movimientos (item_id, tipo, cantidad, stock_resultante, usuario_nomina, fecha_hora, motivo)
    SELECT id, N'AJUSTE', stock_activo, stock_activo, N'admin', @ahora, N'Carga inicial'
    FROM dbo.items
    WHERE stock_activo > 0;

    -- Verificación: si no cuadra, se revierte TODA la carga.
    DECLARE @items INT      = (SELECT COUNT(*) FROM dbo.items);
    DECLARE @movs INT       = (SELECT COUNT(*) FROM dbo.movimientos);
    DECLARE @descuadres INT = (SELECT COUNT(*) FROM dbo.vw_chequeo_stock);
    IF @items <> 72 OR @movs <> 66 OR @descuadres <> 0
    BEGIN
        DECLARE @msg NVARCHAR(400) = CONCAT(
            N'Verificación falló: items=', @items, N' (esperado 72), movimientos=', @movs,
            N' (esperado 66), filas en vw_chequeo_stock=', @descuadres, N' (esperado 0).');
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
