@echo off
REM ============================================================
REM  Kardex - servidor web (FastAPI / uvicorn)
REM  Escucha en 10.207.5.9:1003. Solo red interna.
REM  En produccion lo arranca PM2 con ecosystem.config.js; este
REM  .bat es para levantarlo a mano y ver la salida en consola
REM  (prueba antes de meterlo a PM2, o diagnostico si falla).
REM  Runbook completo: CLAUDE.md, seccion "Despliegue".
REM ============================================================

REM Ir a la raiz del proyecto (este .bat vive en deploy\).
cd /d "%~dp0.."

REM Usa el python del venv si existe; si no, el del PATH.
set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

%PY% -m uvicorn server:app --host 10.207.5.9 --port 1003 --workers 2
