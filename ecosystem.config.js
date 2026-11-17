// Configuración de PM2 para Kardex (servidor Windows 10.207.5.9).
//
// PM2 corre uvicorn directamente: no hay nginx de por medio, uvicorn escucha en
// el 1003 y sirve tanto el API como el frontend.
//
//   pm2 start ecosystem.config.js
//   pm2 restart kardex
//   pm2 logs kardex
//
// No requiere editarse al desplegar: las rutas se derivan de la ubicación de
// este archivo, así que funciona en cualquier carpeta donde se clone el repo.

const path = require('path');
const fs = require('fs');

// Si hay venv en el proyecto se usa su python.exe; si no, el del PATH.
const venvPython = path.join(__dirname, 'venv', 'Scripts', 'python.exe');
const python = fs.existsSync(venvPython) ? venvPython : 'python';

module.exports = {
  apps: [
    {
      name: 'kardex',
      script: python,
      // 2 workers: el servidor es sin estado (JWT) y el stock se protege con un
      // UPDATE atómico en la BD, así que los workers no se pisan.
      args: '-m uvicorn server:app --host 10.207.5.9 --port 1003 --workers 2',
      interpreter: 'none', // PM2 no debe meter node de por medio
      cwd: __dirname, // para que encuentre .env, frontend/ y logs/
      autorestart: true,
      max_restarts: 10,
      min_uptime: '20s', // si truena antes de esto, cuenta como arranque fallido
      restart_delay: 3000,
      time: true, // timestamp en los logs de PM2
    },
  ],
};
