/* =====================================================================
   Kardex — 02: tránsito de Infor (asignación manual de líneas de OC)
   ---------------------------------------------------------------------
   Para la BD kardex que YA existe (creada con 01_crear_kardex.sql antes
   de este cambio). Se corre una vez en SSMS, conectado a 10.207.5.10.
   Una BD nueva no lo necesita: 01_crear_kardex.sql ya trae todo esto.

   Qué hace (todo en UNA transacción; si algo falla, no queda nada a medias):
     1. Tabla asignacion_transito: línea de OC (DocNum + LineNum) -> item.
        NO es inmutable: la app borra solas las filas de líneas que ya se
        recibieron o cancelaron. El rastro queda en la bitácora del item.
     2. Amplía el CHECK de bitacora.evento con ASIGNAR_TRANSITO y
        QUITAR_TRANSITO.

   Idempotente: si ya se corrió, no cambia nada. No borra datos.
   ===================================================================== */

USE kardex;
GO

SET XACT_ABORT ON;
BEGIN TRY
    BEGIN TRANSACTION;

    /* ---------- 1. Tabla asignacion_transito ---------- */
    IF OBJECT_ID(N'dbo.asignacion_transito', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.asignacion_transito (
            doc_num       NVARCHAR(30)  NOT NULL,      -- DocNum de Infor, p. ej. PEX000977
            line_num      INT           NOT NULL,      -- LineNum de Infor, p. ej. 10
            item_id       INT           NOT NULL CONSTRAINT FK_asignacion_transito_items REFERENCES dbo.items (id),
            referencia    NVARCHAR(500) NULL,          -- copia de Reference al asignar
            asignado_por  NVARCHAR(50)  NOT NULL,      -- nómina del supervisor
            asignado_en   DATETIME2(0)  NOT NULL CONSTRAINT DF_asignacion_transito_en DEFAULT (SYSUTCDATETIME()),  -- UTC
            -- Una línea solo puede pertenecer a un item.
            CONSTRAINT PK_asignacion_transito PRIMARY KEY (doc_num, line_num),
            CONSTRAINT CK_asignacion_transito_doc CHECK (LEN(doc_num) > 0)
        );
        CREATE INDEX IX_asignacion_transito_item ON dbo.asignacion_transito (item_id);
        PRINT N'Creada la tabla dbo.asignacion_transito.';
    END
    ELSE
        PRINT N'dbo.asignacion_transito ya existía: no se tocó.';

    /* ---------- 2. Eventos de tránsito en la bitácora ---------- */
    IF NOT EXISTS (SELECT 1 FROM sys.check_constraints
                   WHERE name = N'CK_bitacora_evento'
                     AND parent_object_id = OBJECT_ID(N'dbo.bitacora')
                     AND definition LIKE N'%ASIGNAR_TRANSITO%')
    BEGIN
        IF EXISTS (SELECT 1 FROM sys.check_constraints
                   WHERE name = N'CK_bitacora_evento' AND parent_object_id = OBJECT_ID(N'dbo.bitacora'))
            ALTER TABLE dbo.bitacora DROP CONSTRAINT CK_bitacora_evento;
        ALTER TABLE dbo.bitacora ADD CONSTRAINT CK_bitacora_evento CHECK (evento IN (
            N'ALTA_ITEM', N'EDICION_ITEM', N'OBSOLETO', N'REVERTIR_OBSOLETO',
            N'ASIGNAR_TRANSITO', N'QUITAR_TRANSITO'));
        PRINT N'CK_bitacora_evento ampliado con ASIGNAR_TRANSITO y QUITAR_TRANSITO.';
    END
    ELSE
        PRINT N'CK_bitacora_evento ya aceptaba los eventos de tránsito: no se tocó.';

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    DECLARE @err NVARCHAR(2048) = ERROR_MESSAGE();
    RAISERROR(N'ALTO: no se aplicó ningún cambio. %s', 16, 1, @err);
END CATCH
GO

/* ---------- Verificación ---------- */
SELECT
    CASE WHEN OBJECT_ID(N'dbo.asignacion_transito', N'U') IS NOT NULL THEN N'OK' ELSE N'FALTA' END AS tabla_asignacion_transito,
    (SELECT CASE WHEN definition LIKE N'%QUITAR_TRANSITO%' THEN N'OK' ELSE N'FALTA' END
     FROM sys.check_constraints
     WHERE name = N'CK_bitacora_evento' AND parent_object_id = OBJECT_ID(N'dbo.bitacora')) AS check_bitacora_evento;
GO
