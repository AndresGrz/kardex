/* ============================================================
   KARDEX — app.js (frontend sin framework; backend FastAPI)
   Rutas por hash:
     #/buscar            inventario (búsqueda)
     #/item/<id>         kardex del item (movimientos + bitácora)
     #/entrada[?item=]   #/salida[?item=]   #/ajuste[?item=]  (capturas)
     #/nuevo  #/chequeo  #/transito  (solo supervisor)
   Todo texto que viene del servidor pasa por esc() antes de entrar al HTML.
   La UI oculta lo que el rol no puede hacer, pero quien decide es la API (403).
   ============================================================ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const I = window.icon;

/* ---------- sesión (JWT de 8 h en el navegador) ---------- */
const TOKEN_KEY = 'kardex_token';
function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; }
}
function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (_) {}
}

/* ---------- HTTP ---------- */
class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function http(method, path, body, { silent401 = false } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (getToken()) headers.Authorization = 'Bearer ' + getToken();
  let res;
  try {
    res = await fetch('/api' + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (_) {
    throw new ApiError('Sin conexión con el servidor de Kardex.', 0);
  }
  let data = null;
  try { data = await res.json(); } catch (_) {}
  const detail = (data && data.detail) || `Error ${res.status}`;
  if (res.status === 401 && path !== '/login' && !silent401) sesionExpirada(detail);
  if (!res.ok) throw new ApiError(detail, res.status);
  return data;
}

// Igual que http() pero, si falla, avisa con un toast y regresa null.
async function call(method, path, body) {
  try { return await http(method, path, body); }
  catch (e) {
    if (e.status !== 401) toast('err', 'No se pudo completar', e.message);
    return null;
  }
}

const state = {
  user: null,
  version: '',
  route: { name: 'buscar', id: null, query: {} },
  // Buscar
  q: '', obsoletos: false, filtro: 'todos', items: [], transDisp: true, sel: 0,
  // Kardex del item
  kx: null, kxTab: 'mov',
  // Tránsito por asignar (supervisor): líneas sin item y catálogo para el selector
  tr: null, trItems: [],
  // Capturas hechas en esta sesión del navegador (solo para mostrar)
  sesion: [],
};

/* ---------- helpers ---------- */
const esc = (v) =>
  String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString('es-MX'));
const signed = (n) => (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n));
const esSup = () => !!state.user && state.user.rol === 'supervisor';
const val = (id) => ($('#' + id) || {}).value || '';
const norm = (s) => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
function parseEntero(v) {
  const s = String(v ?? '').trim();
  return /^\d+$/.test(s) ? Number(s) : null;
}
function rolLabel(rol) {
  return { almacenista: 'Almacenista', supervisor: 'Supervisor' }[rol] || rol || '';
}
function initials(nombre) {
  return (nombre || '?').split(' ').filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
}
function recalcBajoMinimo(it) {
  // Mismo criterio que vw_items_bajo_minimo.
  it.bajo_minimo = !it.obsoleto && it.inventario_minimo > 0 && it.stock_activo <= it.inventario_minimo;
}

function itemChips(it) {
  const c = [];
  if (it.obsoleto) c.push(`<span class="chip danger sm">${I('archive')}Obsoleto</span>`);
  if (it.bajo_minimo) c.push(`<span class="chip warn sm">${I('trendDown')}Bajo mínimo</span>`);
  if (it.requiere_cambio) c.push(`<span class="chip info sm">${I('swap')}Requiere cambio</span>`);
  return c.join('');
}
const TIPO = {
  ENTRADA: ['ok', 'arrowIn', 'Entrada'],
  SALIDA: ['', 'arrowOut', 'Salida'],
  AJUSTE: ['info', 'sliders', 'Ajuste'],
};
function tipoChip(t) {
  const [cls, ic, label] = TIPO[t] || ['', 'dots', t];
  return `<span class="chip sm ${cls}">${I(ic)}${label}</span>`;
}
const EVENTO = {
  ALTA_ITEM: ['info', 'plus', 'Alta'],
  EDICION_ITEM: ['', 'edit', 'Edición'],
  OBSOLETO: ['danger', 'archive', 'Obsoleto'],
  REVERTIR_OBSOLETO: ['ok', 'undo', 'Reactivado'],
  ASIGNAR_TRANSITO: ['info', 'truck', 'Tránsito asignado'],
  QUITAR_TRANSITO: ['', 'truck', 'Tránsito quitado'],
};
const CAMPOS = {
  numero_parte: 'Número de parte', descripcion: 'Descripción', marca_modelo_uso: 'Marca/modelo/uso',
  unidad_medida: 'Unidad', inventario_minimo: 'Mínimo', inventario_maximo: 'Máximo',
  requiere_cambio: 'Requiere cambio', obsoleto: 'Obsoleto',
};

/* Tarjeta de una línea de OC en tránsito (kardex del item y pestaña Tránsito).
   foot: HTML de la parte de abajo (origen de la asignación o botones). */
function tarjetaTransito(l, foot = '') {
  return `
    <div class="tr-card ${l.vencida ? 'late' : ''}">
      <div class="tr-top">
        <span class="tr-doc">${esc(l.doc_num)} <span class="muted">· línea ${esc(l.line_num)}</span></span>
        <span class="tr-qty">${fmt(l.cantidad)} <small>${esc(l.uom)}</small></span>
      </div>
      <div class="tr-ref">${l.referencia ? esc(l.referencia) : '<span class="muted">Sin referencia en Infor</span>'}</div>
      <div class="tr-dates">
        <span>Creada ${esc(l.creada || '—')} · Entrega esperada <b>${esc(l.entrega || '—')}</b></span>
        ${l.vencida ? `<span class="chip warn sm">${I('clock')}Vencida</span>` : ''}
      </div>
      ${foot ? `<div class="tr-foot">${foot}</div>` : ''}
    </div>`;
}
const claveLinea = (l) => `${l.doc_num}-${l.line_num}`;

/* ---------- sonidos de toast (igual que Dossier) ---------- */
let _audioCtx = null;
function _ctx() {
  if (!_audioCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    _audioCtx = new AC();
  }
  if (_audioCtx.state === 'suspended') _audioCtx.resume();
  return _audioCtx;
}
function _note(ctx, freq, at, dur, type) {
  const t = ctx.currentTime + at;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t);
  gain.gain.setValueAtTime(0.0001, t);
  gain.gain.exponentialRampToValueAtTime(0.08, t + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  osc.connect(gain).connect(ctx.destination);
  osc.start(t);
  osc.stop(t + dur + 0.02);
}
function playTone(kind) {
  try {
    const ctx = _ctx();
    if (!ctx) return;
    if (kind === 'ok') {
      _note(ctx, 523.25, 0, 0.1, 'sine');
      _note(ctx, 659.25, 0.07, 0.12, 'sine');
    } else if (kind === 'err') {
      _note(ctx, 220.0, 0, 0.14, 'sawtooth');
      _note(ctx, 164.81, 0.1, 0.16, 'sawtooth');
    } else {
      _note(ctx, 440.0, 0, 0.14, 'triangle');
    }
  } catch (_) {
    /* el audio nunca debe romper el toast */
  }
}

/* ---------- toasts ---------- */
function toast(kind, title, body) {
  playTone(kind);
  const ic = kind === 'err' ? 'xCircle' : kind === 'ok' ? 'checkCircle' : 'alert';
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.innerHTML = `<div class="ic">${I(ic)}</div><div><b>${esc(title)}</b><p>${esc(body)}</p></div>`;
  $('#toasts').appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .3s, transform .3s';
    t.style.opacity = '0';
    t.style.transform = 'translateX(8px)';
    setTimeout(() => t.remove(), 320);
  }, 3800);
}

/* ---------- modal ---------- */
let _modalOnClose = null;
function openModal(html, onClose = null) {
  _modalOnClose = onClose;
  $('#modalHost').innerHTML = html;
  $('#scrim').classList.add('show');
  setTimeout(() => {
    const f = $('#modalHost [data-autofocus]') || $('#modalHost input');
    if (f) f.focus();
  }, 60);
}
function closeModal() {
  if (!$('#scrim').classList.contains('show')) return;
  const f = _modalOnClose;
  _modalOnClose = null;
  $('#scrim').classList.remove('show');
  setTimeout(() => { if (!$('#scrim').classList.contains('show')) $('#modalHost').innerHTML = ''; }, 180);
  if (f) f();
}
// Promesa: true si confirma, false si cancela / Esc.
function confirmar({ title, sub = '', body = '', ok, okClass = 'primary', icon = 'checkCircle', wide = false }) {
  return new Promise((resolve) => {
    openModal(`
      <div class="modal ${wide ? 'wide' : ''}">
        <div class="mh"><h3>${title}</h3>${sub ? `<p>${sub}</p>` : ''}</div>
        ${body ? `<div class="mb">${body}</div>` : '<div style="height:16px"></div>'}
        <div class="mf">
          <button class="btn ghost" data-close>Cancelar</button>
          <button class="btn ${okClass}" id="mOk" data-autofocus>${I(icon)} ${ok}</button>
        </div>
      </div>`, () => resolve(false));
    $('#mOk').onclick = () => {
      _modalOnClose = null;
      closeModal();
      resolve(true);
    };
  });
}

/* Enter pasa al siguiente campo [data-f]; en el último, envía. */
function enterAvanza(root, enviar) {
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !e.target.matches('[data-f]')) return;
    e.preventDefault();
    const fs = $$('[data-f]', root).filter((x) => !x.disabled && x.offsetParent !== null);
    const i = fs.indexOf(e.target);
    if (i >= 0 && i < fs.length - 1) fs[i + 1].focus();
    else enviar();
  });
}

function campo({ id, label, req = false, cls = '', type = 'text', ph = '', span = false, hint = '', mono = false, attrs = '', value = '' }) {
  return `
    <div class="field ${span ? 'span-2' : ''}">
      <label for="${id}">${label}${req ? '<span class="req">*</span>' : ''}</label>
      <input class="input big ${mono ? 'mono' : ''} ${cls}" id="${id}" type="${type}" placeholder="${esc(ph)}"
        value="${esc(value ?? '')}" autocomplete="off" spellcheck="false" data-f ${attrs} />
      ${hint ? `<span class="hint">${hint}</span>` : ''}
    </div>`;
}

// Marca el campo inválido, enfoca y deja el mensaje. Regresa null para cortar.
function fallo(msgEl, id, texto) {
  if (msgEl) msgEl.textContent = texto;
  const el = id && $('#' + id);
  if (el) {
    el.classList.add('invalid');
    el.addEventListener('input', () => el.classList.remove('invalid'), { once: true });
    el.focus();
  }
  return null;
}

/* ============================================================
   LOGIN
   ============================================================ */
function initLogin() {
  $('#loginLogo').innerHTML = I('clipboard');
  $('#loginLogo2').innerHTML = I('clipboard');
  $('#loginTags').innerHTML = [
    ['arrowIn', 'Entradas'],
    ['arrowOut', 'Salidas'],
    ['sliders', 'Ajustes'],
    ['lock', 'Historial inmutable'],
  ].map(([ic, t]) => `<span class="t">${I(ic)}${t}</span>`).join('');

  const err = (m) => ($('#loginError').textContent = m || '');
  $('#loginUser').addEventListener('input', () => err(''));
  $('#loginPass').addEventListener('input', () => err(''));
  $('#loginUser').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#loginPass').focus(); });
  $('#loginPass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#loginBtn').click(); });

  $('#loginBtn').onclick = async () => {
    const nomina = $('#loginUser').value.trim();
    const password = $('#loginPass').value;
    err('');
    if (!nomina || !password) return err('Nómina y contraseña son obligatorias.');
    const btn = $('#loginBtn');
    btn.disabled = true;
    btn.textContent = 'Entrando…';
    try {
      const res = await http('POST', '/login', { nomina, password });
      setToken(res.token);
      state.user = res.user;
      $('#loginPass').value = '';
      entrarApp();
    } catch (e) {
      err(e.message);
      $('#loginPass').select();
    } finally {
      btn.disabled = false;
      btn.textContent = 'Entrar';
    }
  };
}

function mostrarLogin() {
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#login').style.opacity = '1';
  setTimeout(() => ($('#loginUser').value ? $('#loginPass') : $('#loginUser')).focus(), 50);
}

function entrarApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  if (!location.hash || location.hash === '#' || location.hash === '#/') location.hash = '#/buscar';
  else route();
}

function sesionExpirada(detail) {
  if (!state.user) return;
  setToken(null);
  state.user = null;
  closeModal();
  toast('err', 'Sesión expirada', detail || 'Vuelve a iniciar sesión.');
  mostrarLogin();
}

function cerrarSesion() {
  setToken(null);
  state.user = null;
  state.sesion = [];
  $('#loginUser').value = '';
  mostrarLogin();
}

/* ---------- cambiar contraseña (es la misma de Dossier) ---------- */
function abrirCambiarPassword() {
  openModal(`
    <div class="modal">
      <div class="mh"><h3>${I('shield')} Cambiar contraseña</h3>
        <p>Ingresa tu contraseña actual y la nueva.</p></div>
      <div class="mb" style="display:flex;flex-direction:column;gap:12px;" id="cpForm">
        <div class="banner warn">${I('alert')}<span>También cambia tu contraseña de <b>Dossier</b>: es el mismo usuario.</span></div>
        <div class="field"><label>Usuario</label>
          <input class="input mono" value="${esc(state.user.nomina)}" readonly /></div>
        <div class="field"><label for="cpActual">Contraseña actual</label>
          <input class="input" type="password" id="cpActual" data-f /></div>
        <div class="field"><label for="cpNueva">Contraseña nueva</label>
          <input class="input" type="password" id="cpNueva" data-f /></div>
        <div class="field"><label for="cpConfirm">Confirmar contraseña nueva</label>
          <input class="input" type="password" id="cpConfirm" data-f /></div>
        <p class="form-msg" id="cpMsg"></p>
      </div>
      <div class="mf">
        <button class="btn ghost" data-close>Cancelar</button>
        <button class="btn primary" id="cpGuardar">${I('checkCircle')} Guardar</button>
      </div>
    </div>`);
  const m = $('#cpMsg');
  enterAvanza($('#cpForm'), () => $('#cpGuardar').click());
  $('#cpGuardar').onclick = async () => {
    m.textContent = '';
    const actual = val('cpActual'), nueva = val('cpNueva'), conf = val('cpConfirm');
    if (!actual) return fallo(m, 'cpActual', 'Escribe tu contraseña actual.');
    if (!nueva) return fallo(m, 'cpNueva', 'Escribe la contraseña nueva.');
    if (nueva !== conf) return fallo(m, 'cpConfirm', 'La confirmación no coincide con la contraseña nueva.');
    $('#cpGuardar').disabled = true;
    try {
      await http('POST', '/password', { actual, nueva });
      closeModal();
      toast('ok', 'Contraseña actualizada', 'Úsala también para entrar a Dossier.');
    } catch (e) {
      m.textContent = e.message;
      $('#cpGuardar').disabled = false;
    }
  };
}

/* ============================================================
   RUTAS
   ============================================================ */
const ROUTES = {
  buscar: { nav: 'buscar', title: 'Inventario', sub: 'Busca por número de parte, descripción o uso', mount: mountBuscar },
  item: { nav: 'buscar', title: 'Kardex del item', sub: 'Datos, movimientos y bitácora. Nada se edita ni se borra.', mount: mountItem },
  entrada: { nav: 'entrada', title: 'Registrar entrada', sub: 'Material que llega al almacén: suma al stock', mount: mountEntrada },
  salida: { nav: 'salida', title: 'Registrar salida', sub: 'Entrega de material: resta del stock y se cierra al momento', mount: mountSalida },
  ajuste: { nav: 'ajuste', sup: true, title: 'Ajuste de inventario', sub: 'Corrige el stock contra el conteo físico', mount: mountAjuste },
  nuevo: { nav: 'nuevo', sup: true, title: 'Nuevo item', sub: 'Alta de un número de parte en el catálogo', mount: mountNuevo },
  chequeo: { nav: 'chequeo', sup: true, title: 'Chequeo de stock', sub: 'Stock activo contra la suma de sus movimientos', mount: mountChequeo },
  transito: { nav: 'transito', sup: true, title: 'Tránsito por asignar', sub: 'Líneas de OC de Infor que no empatan solas con un número de parte', mount: mountTransito },
};

function parseHash() {
  const h = (location.hash || '').replace(/^#\/?/, '');
  const [path, qs = ''] = h.split('?');
  const [name, id] = path.split('/');
  return { name: name || 'buscar', id: id ? Number(id) : null, query: Object.fromEntries(new URLSearchParams(qs)) };
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  if (!state.user) return;
  const r = parseHash();
  const def = ROUTES[r.name];
  if (!def) return go('#/buscar');
  if (def.sup && !esSup()) {
    toast('warn', 'Solo supervisor', 'Esa pantalla es solo para supervisores.');
    return go('#/buscar');
  }
  state.route = r;
  closeModal();
  renderSide();
  renderTop(def);
  $('#view').scrollTop = 0;
  await def.mount(r);
}

/* ============================================================
   SIDEBAR / TOPBAR
   ============================================================ */
function renderSide() {
  const u = state.user;
  const cur = (ROUTES[state.route.name] || {}).nav;
  const item = (id, ic, label) => `
    <button class="item ${cur === id ? 'active' : ''}" data-go="#/${id}">${I(ic)}<span>${label}</span></button>`;
  $('#side').innerHTML = `
    <div class="brand"><div class="logo">${I('clipboard')}</div><div class="name">Kardex</div>
      ${state.version ? `<span class="ver-badge">v${esc(state.version)}</span>` : ''}</div>
    <div class="user">
      <div class="avatar">${esc(initials(u.nombre))}</div>
      <div class="meta"><b>${esc(u.nombre)}</b><small>${rolLabel(u.rol)} · ${esc(u.nomina)}</small></div>
    </div>
    <div class="nav-label">Almacén</div>
    <nav class="nav">
      ${item('buscar', 'search', 'Inventario')}
      ${item('entrada', 'arrowIn', 'Entrada')}
      ${item('salida', 'arrowOut', 'Salida')}
    </nav>
    ${esSup() ? `
      <div class="nav-label" style="margin-top:14px;">Supervisor</div>
      <nav class="nav">
        ${item('ajuste', 'sliders', 'Ajuste')}
        ${item('nuevo', 'plus', 'Nuevo item')}
        ${item('transito', 'truck', 'Tránsito')}
        ${item('chequeo', 'shield', 'Chequeo de stock')}
      </nav>` : ''}
    <div class="grow"></div>
    <div class="foot">
      <button class="cpass" id="btnCambiarPass">${I('key')} Contraseña</button>
      <button class="logout" id="btnLogout">${I('logout')} Cerrar sesión</button>
    </div>`;
  $('#btnCambiarPass').onclick = abrirCambiarPassword;
  $('#btnLogout').onclick = cerrarSesion;
}

function renderTop(def) {
  let actions = '';
  if (state.route.name === 'buscar') {
    actions = `
      <label class="switch">
        <input type="checkbox" id="chkObs" ${state.obsoletos ? 'checked' : ''} /><span class="track"></span>Incluir obsoletos
      </label>
      <div class="search wide">${I('search')}<input id="topSearch" placeholder="Número de parte, descripción o uso…"
        value="${esc(state.q)}" autocomplete="off" spellcheck="false" /><kbd>↑↓</kbd></div>
      ${esSup() ? `<button class="btn primary" data-go="#/nuevo">${I('plus')} Nuevo item</button>` : ''}`;
  }
  $('#topbar').innerHTML = `
    <div class="page-title"><h1>${def.title}</h1><p>${def.sub}</p></div>
    <div class="spacer"></div>
    ${actions}`;
}

/* ============================================================
   BUSCAR (inventario)
   ============================================================ */
let _buscarTimer = null;
let _buscarSeq = 0;

async function mountBuscar() {
  $('#view').innerHTML = `<div id="buscarBody"><div class="loading">Cargando inventario…</div></div>`;
  const inp = $('#topSearch');
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
  inp.addEventListener('input', () => {
    state.q = inp.value;
    clearTimeout(_buscarTimer);
    _buscarTimer = setTimeout(cargarItems, 180);
  });
  inp.addEventListener('keydown', (e) => {
    const rows = itemsFiltrados();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!rows.length) return;
      state.sel = Math.max(0, Math.min(rows.length - 1, state.sel + (e.key === 'ArrowDown' ? 1 : -1)));
      marcarSel();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const it = rows[state.sel];
      if (it) go(`#/item/${it.id}`);
    } else if (e.key === 'Escape' && inp.value) {
      inp.value = '';
      state.q = '';
      cargarItems();
    }
  });
  $('#chkObs').addEventListener('change', (e) => {
    state.obsoletos = e.target.checked;
    cargarItems();
    inp.focus();
  });
  await cargarItems();
}

async function cargarItems() {
  const seq = ++_buscarSeq;
  const qs = new URLSearchParams({ q: state.q.trim(), obsoletos: state.obsoletos ? 'true' : 'false' });
  const res = await call('GET', `/items?${qs}`);
  if (!res || seq !== _buscarSeq || state.route.name !== 'buscar') return;
  state.items = res.items;
  state.transDisp = res.transito_disponible;
  state.sel = 0;
  renderBuscar();
}

function itemsFiltrados() {
  return state.filtro === 'bajo' ? state.items.filter((i) => i.bajo_minimo) : state.items;
}

function marcarSel() {
  $$('#tblItems tbody tr').forEach((tr, i) => tr.classList.toggle('sel', i === state.sel));
  const tr = $$('#tblItems tbody tr')[state.sel];
  if (tr) tr.scrollIntoView({ block: 'nearest' });
}

function renderBuscar() {
  const todos = state.items;
  const rows = itemsFiltrados();
  const bajo = todos.filter((i) => i.bajo_minimo).length;
  const cambio = todos.filter((i) => i.requiere_cambio).length;
  const unidades = todos.reduce((a, i) => a + i.stock_activo, 0);
  const q = state.q.trim();

  $('#buscarBody').innerHTML = `
    <div class="stat-row" style="margin-bottom:14px;">
      <div class="stat"><div class="k">Items</div><div class="v">${fmt(todos.length)}</div><div class="d">${q ? 'en esta búsqueda' : state.obsoletos ? 'incluye obsoletos' : 'activos'}</div></div>
      <div class="stat ${bajo ? 'warn' : ''}"><div class="k">Bajo mínimo</div><div class="v">${fmt(bajo)}</div><div class="d">stock ≤ mínimo</div></div>
      <div class="stat"><div class="k">Requieren cambio</div><div class="v">${fmt(cambio)}</div><div class="d">se entregan contra pieza usada</div></div>
      <div class="stat"><div class="k">Unidades en stock</div><div class="v">${fmt(unidades)}</div><div class="d">suma de activos</div></div>
    </div>

    ${state.transDisp ? '' : `<div class="banner lock" style="margin-bottom:14px;">${I('info')}<span><b>Tránsito de Infor no disponible.</b> La columna En tránsito muestra N/D hasta que el API de órdenes de compra responda.</span></div>`}

    <div class="card">
      <div class="card-h">
        <div class="seg">
          <button class="${state.filtro === 'todos' ? 'active' : ''}" data-filtro="todos">${I('list')} Todos <span class="badge-n">${todos.length}</span></button>
          <button class="${state.filtro === 'bajo' ? 'active' : ''}" data-filtro="bajo">${I('trendDown')} Bajo mínimo <span class="badge-n">${bajo}</span></button>
        </div>
        <span class="sub">${rows.length} resultado(s) · <kbd>↑</kbd><kbd>↓</kbd> y <kbd>Enter</kbd> abre el kardex</span>
      </div>
      <div class="card-b" style="padding:8px 6px 10px;overflow:auto;">
        ${rows.length ? `
        <table class="tbl" id="tblItems">
          <thead><tr>
            <th>Número de parte</th><th>Descripción</th><th>Uso / medida</th>
            <th class="r">Activo</th><th class="r">En tránsito</th><th class="r">Mín.</th><th class="r">Máx.</th><th>Etiquetas</th>
          </tr></thead>
          <tbody>
            ${rows.map((it, i) => `
            <tr class="click ${i === state.sel ? 'sel' : ''}" data-go="#/item/${it.id}">
              <td class="np">${esc(it.numero_parte)}</td>
              <td>${esc(it.descripcion)}</td>
              <td class="muted">${it.marca_modelo_uso ? esc(it.marca_modelo_uso) : '—'}</td>
              <td class="num r" title="${esc(it.unidad_medida)}"><b>${fmt(it.stock_activo)}</b></td>
              <td class="num r">${it.en_transito === null ? '<span class="nd">N/D</span>' : fmt(it.en_transito)}</td>
              <td class="num r">${fmt(it.inventario_minimo)}</td>
              <td class="num r">${fmt(it.inventario_maximo)}</td>
              <td><div class="chips">${itemChips(it)}</div></td>
            </tr>`).join('')}
          </tbody>
        </table>` : `
        <div class="empty"><div class="ic">${I('search')}</div>
          <b>${q ? `Sin resultados para “${esc(q)}”` : 'Sin items'}</b>
          ${state.filtro === 'bajo' ? 'Ningún item está bajo su mínimo.' : state.obsoletos ? '' : 'Activa “Incluir obsoletos” si buscas un item dado de baja.'}
        </div>`}
      </div>
    </div>`;

  $$('[data-filtro]').forEach((b) => (b.onclick = () => {
    state.filtro = b.dataset.filtro;
    state.sel = 0;
    renderBuscar();
    $('#topSearch').focus();
  }));
}

/* ============================================================
   KARDEX DEL ITEM
   ============================================================ */
async function mountItem(r) {
  $('#view').innerHTML = `<div class="loading">Cargando kardex…</div>`;
  const res = await call('GET', `/items/${r.id}`);
  if (!res) return go('#/buscar');
  if (state.route.name !== 'item' || state.route.id !== r.id) return;
  state.kx = res;
  renderItem();
}

async function recargarItem() {
  const res = await call('GET', `/items/${state.kx.item.id}`);
  if (res) {
    state.kx = res;
    renderItem();
  }
}

function renderItem() {
  const { item: it, movimientos: movs, bitacora: bits, transito, transito_disponible } = state.kx;
  const sup = esSup();
  const trans = it.en_transito;

  $('#view').innerHTML = `
  <div class="row" style="margin-bottom:14px;gap:12px;flex-wrap:wrap;">
    <button class="btn sm ghost" data-go="#/buscar">${I('chevL')} Inventario</button>
    <h2 class="mono" style="margin:0;font-size:20px;font-weight:700;letter-spacing:-.02em;">${esc(it.numero_parte)}</h2>
    <div class="row" style="gap:6px;">${itemChips(it)}</div>
  </div>

  ${it.obsoleto ? `<div class="banner lock" style="margin-bottom:14px;">${I('archive')}<span><b>Item obsoleto</b> desde el ${esc(it.obsoleto_en)} (marcó ${esc(it.obsoleto_por)}). Conserva su stock; no acepta entradas ni salidas.${sup ? '' : ' Solo un supervisor puede revertirlo.'}</span></div>` : ''}

  <div class="grid-2 split" style="grid-template-columns:minmax(0,1fr) 320px;align-items:start;gap:14px;">
    <div class="col" style="gap:14px;min-width:0;">
      <div class="stat-row">
        <div class="stat ${it.bajo_minimo ? 'warn' : ''}"><div class="k">Activo</div><div class="v">${fmt(it.stock_activo)} <small>${esc(it.unidad_medida)}</small></div><div class="d">${it.bajo_minimo ? 'bajo su mínimo' : 'en almacén'}</div></div>
        <div class="stat"><div class="k">En tránsito</div><div class="v ${trans === null ? 'nd' : ''}">${trans === null ? 'N/D' : fmt(trans)}</div><div class="d">${transito_disponible ? 'en OC abiertas (Infor)' : 'tránsito no disponible'}</div></div>
        <div class="stat"><div class="k">Mínimo</div><div class="v">${fmt(it.inventario_minimo)}</div><div class="d">${it.inventario_minimo > 0 ? 'alerta de mínimos' : 'sin alerta'}</div></div>
        <div class="stat"><div class="k">Máximo</div><div class="v">${fmt(it.inventario_maximo)}</div><div class="d">${it.inventario_maximo === null ? 'sin máximo' : 'tope de stock'}</div></div>
      </div>

      <div class="card">
        <div class="card-h"><h3>${I('fileText')} Datos del item</h3></div>
        <div class="card-b">
          <div class="grid-2" style="gap:18px 28px;">
            <dl class="kv">
              <dt>Descripción</dt><dd>${esc(it.descripcion)}</dd>
              <dt>Marca / modelo / uso</dt><dd>${it.marca_modelo_uso ? esc(it.marca_modelo_uso) : '—'}</dd>
              <dt>Unidad</dt><dd>${esc(it.unidad_medida)}</dd>
            </dl>
            <dl class="kv">
              <dt>Requiere cambio</dt><dd>${it.requiere_cambio ? 'Sí, contra pieza usada' : 'No'}</dd>
              <dt>Alta</dt><dd>${esc(it.creado_en)} · <span class="mono">${esc(it.creado_por)}</span></dd>
              <dt>Estado</dt><dd>${it.obsoleto ? 'Obsoleto' : 'Activo'}</dd>
            </dl>
          </div>
        </div>
      </div>

      ${seccionTransito(transito, transito_disponible, sup)}

      <div class="card">
        <div class="card-h">
          <div class="seg">
            <button class="${state.kxTab === 'mov' ? 'active' : ''}" data-kxtab="mov">${I('swap')} Movimientos <span class="badge-n">${movs.length}</span></button>
            <button class="${state.kxTab === 'bit' ? 'active' : ''}" data-kxtab="bit">${I('history')} Bitácora <span class="badge-n">${bits.length}</span></button>
          </div>
          <span class="sub">Del más reciente al más antiguo</span>
        </div>
        <div class="card-b" style="padding:8px 6px 10px;overflow:auto;" id="kxTab">
          ${state.kxTab === 'mov' ? tablaMovimientos(movs) : tablaBitacora(bits)}
        </div>
      </div>
    </div>

    <div class="aside">
      <div class="card">
        <div class="card-h"><h3>Acciones</h3></div>
        <div class="card-b actions">
          <button class="btn" data-go="#/entrada?item=${it.id}" ${it.obsoleto ? 'disabled' : ''}>${I('arrowIn')} Registrar entrada</button>
          <button class="btn" data-go="#/salida?item=${it.id}" ${it.obsoleto ? 'disabled' : ''}>${I('arrowOut')} Registrar salida</button>
          ${sup ? `
            <div class="divider"></div>
            <button class="btn" data-go="#/ajuste?item=${it.id}">${I('sliders')} Ajuste de inventario</button>
            <button class="btn" id="btnEditar">${I('edit')} Editar item</button>` : ''}
          <div class="divider"></div>
          ${it.obsoleto
            ? sup
              ? `<button class="btn" id="btnReactivar">${I('undo')} Revertir obsoleto</button>`
              : `<p class="note">${I('lock')} Solo un supervisor puede revertir el obsoleto.</p>`
            : `<button class="btn danger" id="btnObsoleto">${I('archive')} Marcar como obsoleto</button>`}
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>${I('lock')} Trazabilidad</h3></div>
        <div class="card-b" style="font-size:12.5px;color:var(--ink-2);display:flex;flex-direction:column;gap:8px;">
          <span>Los movimientos y la bitácora <b>no se editan ni se borran</b>.</span>
          <span>Un error se corrige con un <b>ajuste</b> de supervisor, con motivo.</span>
        </div>
      </div>
    </div>
  </div>`;

  $$('[data-kxtab]').forEach((b) => (b.onclick = () => {
    state.kxTab = b.dataset.kxtab;
    $$('[data-kxtab]').forEach((x) => x.classList.toggle('active', x === b));
    $('#kxTab').innerHTML = state.kxTab === 'mov' ? tablaMovimientos(movs) : tablaBitacora(bits);
  }));
  const bo = $('#btnObsoleto');
  if (bo) bo.onclick = abrirObsoleto;
  const br = $('#btnReactivar');
  if (br) br.onclick = abrirReactivar;
  const be = $('#btnEditar');
  if (be) be.onclick = abrirEditar;
  $$('[data-quitar]').forEach((b) => (b.onclick = () => quitarTransito(transito[Number(b.dataset.quitar)])));
}

/* ---------- tránsito del item (tarjetas) ---------- */
function seccionTransito(lineas, disponible, sup) {
  const total = lineas.reduce((a, l) => a + l.cantidad, 0);
  let body;
  if (!disponible) {
    body = `<div class="banner lock">${I('wifiOff')}<span><b>Tránsito no disponible.</b> No se pudo consultar Infor; se muestra N/D hasta que responda.</span></div>`;
  } else if (!lineas.length) {
    body = `<div class="empty" style="padding:22px 12px;"><div class="ic">${I('truck')}</div><b>Sin líneas en tránsito</b>
      ${sup ? 'Si falta una OC, asígnala desde <a href="#/transito">Tránsito</a>.' : 'Ninguna OC abierta de Infor es de este item.'}</div>`;
  } else {
    body = `<div class="tr-grid">${lineas.map((l, i) => tarjetaTransito(l, l.origen === 'auto'
      ? `<span class="chip info sm">${I('spark')}Automática</span><span class="muted">El código de la referencia es el número de parte</span>`
      : `<span class="chip sm">${I('user')}Asignada · ${esc(l.asignado_por)}</span><span class="muted">${esc(l.asignado_en || '')}</span>
         ${sup ? `<button class="btn sm danger tr-act" data-quitar="${i}">${I('x')} Quitar</button>` : ''}`)).join('')}</div>`;
  }
  return `
    <div class="card">
      <div class="card-h"><h3>${I('truck')} En tránsito</h3>
        <span class="sub">${disponible && lineas.length ? `${lineas.length} línea(s) · ${fmt(total)} en total` : 'OC abiertas en Infor'}</span>
        ${sup ? `<div class="right"><button class="btn sm ghost" data-go="#/transito">${I('list')} Por asignar</button></div>` : ''}</div>
      <div class="card-b">${body}</div>
    </div>`;
}

async function quitarTransito(l) {
  const ok = await confirmar({
    title: `${I('truck')} Quitar asignación`,
    sub: `<span class="mono">${esc(claveLinea(l))}</span> · ${fmt(l.cantidad)} ${esc(l.uom)}`,
    body: `${tarjetaTransito(l)}
      <p class="muted" style="font-size:12.5px;margin:12px 0 0;">La línea deja de sumar al tránsito de este item y regresa a <b>Tránsito por asignar</b>. Queda en la bitácora.</p>`,
    ok: 'Quitar asignación', okClass: 'danger', icon: 'x',
  });
  if (!ok) return;
  const res = await call('POST', '/transito/quitar', { doc_num: l.doc_num, line_num: l.line_num });
  if (!res) return;
  toast('ok', 'Asignación quitada', `${claveLinea(l)} ya no suma al tránsito de ${state.kx.item.numero_parte}.`);
  await recargarItem();
}

function detalleMovimiento(m) {
  let main = '';
  const extra = [];
  if (m.tipo === 'ENTRADA') {
    main = `OC <span class="mono">${esc(m.orden_compra)}</span> · ${esc(m.proveedor)}`;
  } else if (m.tipo === 'SALIDA') {
    main = `${esc(m.departamento_destino)} · ${esc(m.solicitante_nombre)} <span class="mono muted">(${esc(m.solicitante_nomina)})</span>`;
    if (m.pieza_devuelta) main += ` <span class="chip ok sm">${I('swap')}Pieza devuelta</span>`;
    if (m.excepcion_autorizada_por) {
      main += ` <span class="chip warn sm">${I('key')}Sin pieza · autorizó ${esc(m.excepcion_autorizada_por)}</span>`;
      extra.push(`Excepción: ${esc(m.excepcion_motivo)}`);
    }
  } else {
    main = esc(m.motivo);
  }
  if (m.tipo !== 'AJUSTE' && m.motivo) extra.push(esc(m.motivo));
  return main + extra.map((x) => `<span class="sub">${x}</span>`).join('');
}

function tablaMovimientos(movs) {
  if (!movs.length)
    return `<div class="empty"><div class="ic">${I('swap')}</div><b>Sin movimientos</b>Aún no hay entradas, salidas ni ajustes.</div>`;
  return `
    <table class="tbl">
      <thead><tr><th>Fecha</th><th>Tipo</th><th class="r">Cantidad</th><th class="r">Stock</th><th>Detalle</th><th>Usuario</th></tr></thead>
      <tbody>
        ${movs.map((m) => `
        <tr>
          <td class="muted" style="white-space:nowrap;">${esc(m.fecha)}</td>
          <td>${tipoChip(m.tipo)}</td>
          <td class="num r ${m.cantidad > 0 ? 'pos' : 'neg'}">${signed(m.cantidad)}</td>
          <td class="num r"><b>${fmt(m.stock_resultante)}</b></td>
          <td>${detalleMovimiento(m)}</td>
          <td class="mono">${esc(m.usuario_nomina)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function tablaBitacora(bits) {
  if (!bits.length)
    return `<div class="empty"><div class="ic">${I('history')}</div><b>Sin eventos</b></div>`;
  const cambio = (b) => {
    if (b.evento === 'ALTA_ITEM') return `Alta de <span class="mono">${esc(b.valor_nuevo)}</span>`;
    if (b.evento === 'OBSOLETO') return 'Marcado como obsoleto';
    if (b.evento === 'REVERTIR_OBSOLETO') return 'Se revirtió el obsoleto';
    if (b.evento === 'ASIGNAR_TRANSITO') return `Línea en tránsito asignada: <span class="mono">${esc(b.valor_nuevo)}</span>`;
    if (b.evento === 'QUITAR_TRANSITO') return `Se quitó la línea en tránsito: <span class="mono">${esc(b.valor_anterior)}</span>`;
    const v = (x) => (x === null || x === undefined ? '<span class="muted">—</span>' : `<b>${esc(x)}</b>`);
    return `${esc(CAMPOS[b.campo] || b.campo)}: ${v(b.valor_anterior)} <span class="muted">→</span> ${v(b.valor_nuevo)}`;
  };
  return `
    <table class="tbl">
      <thead><tr><th>Fecha</th><th>Evento</th><th>Cambio</th><th>Usuario</th></tr></thead>
      <tbody>
        ${bits.map((b) => {
          const [cls, ic, label] = EVENTO[b.evento] || ['', 'dots', b.evento];
          return `
          <tr>
            <td class="muted" style="white-space:nowrap;">${esc(b.fecha)}</td>
            <td><span class="chip sm ${cls}">${I(ic)}${label}</span></td>
            <td>${cambio(b)}${b.motivo ? `<span class="sub">Motivo: ${esc(b.motivo)}</span>` : ''}</td>
            <td class="mono">${esc(b.usuario_nomina)}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

/* ---------- obsoleto / revertir ---------- */
function abrirObsoleto() {
  const it = state.kx.item;
  openModal(`
    <div class="modal">
      <div class="mh"><h3>${I('archive')} Marcar como obsoleto</h3>
        <p class="mono">${esc(it.numero_parte)}</p></div>
      <div class="mb" style="display:flex;flex-direction:column;gap:12px;" id="obsForm">
        <div class="banner warn">${I('alert')}<span>Conserva su stock (<b>${fmt(it.stock_activo)}</b>), deja de aceptar entradas y salidas, sale de la alerta de mínimos y de la búsqueda. Solo un supervisor puede revertirlo.</span></div>
        <div class="field"><label for="obsMotivo">Motivo <span class="muted">(opcional)</span></label>
          <input class="input" id="obsMotivo" data-f placeholder="Ej. ya no se usa en la línea" /></div>
        <p class="form-msg" id="obsMsg"></p>
      </div>
      <div class="mf">
        <button class="btn ghost" data-close>Cancelar</button>
        <button class="btn danger" id="obsOk">${I('archive')} Marcar obsoleto</button>
      </div>
    </div>`);
  enterAvanza($('#obsForm'), () => $('#obsOk').click());
  $('#obsOk').onclick = async () => {
    $('#obsOk').disabled = true;
    try {
      await http('POST', `/items/${it.id}/obsoleto`, { motivo: val('obsMotivo') || null });
      closeModal();
      toast('ok', 'Item obsoleto', `${it.numero_parte} ya no acepta entradas ni salidas.`);
      await recargarItem();
    } catch (e) {
      $('#obsMsg').textContent = e.message;
      $('#obsOk').disabled = false;
    }
  };
}

function abrirReactivar() {
  const it = state.kx.item;
  openModal(`
    <div class="modal">
      <div class="mh"><h3>${I('undo')} Revertir obsoleto</h3>
        <p class="mono">${esc(it.numero_parte)}</p></div>
      <div class="mb" style="display:flex;flex-direction:column;gap:12px;" id="reaForm">
        <div class="banner info">${I('info')}<span>El item vuelve a aceptar entradas y salidas y a aparecer en la búsqueda y en la alerta de mínimos.</span></div>
        <div class="field"><label for="reaMotivo">Motivo <span class="muted">(opcional)</span></label>
          <input class="input" id="reaMotivo" data-f /></div>
        <p class="form-msg" id="reaMsg"></p>
      </div>
      <div class="mf">
        <button class="btn ghost" data-close>Cancelar</button>
        <button class="btn primary" id="reaOk">${I('undo')} Revertir obsoleto</button>
      </div>
    </div>`);
  enterAvanza($('#reaForm'), () => $('#reaOk').click());
  $('#reaOk').onclick = async () => {
    $('#reaOk').disabled = true;
    try {
      await http('POST', `/items/${it.id}/reactivar`, { motivo: val('reaMotivo') || null });
      closeModal();
      toast('ok', 'Item reactivado', `${it.numero_parte} vuelve a estar activo.`);
      await recargarItem();
    } catch (e) {
      $('#reaMsg').textContent = e.message;
      $('#reaOk').disabled = false;
    }
  };
}

/* ---------- alta / edición de items ---------- */
function itemFormHTML(it, nuevo) {
  return `
    <div class="form-grid" id="itemForm">
      ${campo({ id: 'iNP', label: 'Número de parte', req: true, mono: true, value: it.numero_parte })}
      ${campo({ id: 'iUM', label: 'Unidad de medida', value: it.unidad_medida ?? 'EACH' })}
      ${campo({ id: 'iDesc', label: 'Descripción', req: true, span: true, value: it.descripcion })}
      ${campo({ id: 'iUso', label: 'Marca / modelo / uso', span: true, ph: 'Opcional (medida, máquina, proceso…)', value: it.marca_modelo_uso })}
      ${campo({ id: 'iMin', label: 'Inventario mínimo', type: 'number', attrs: 'min="0" step="1" inputmode="numeric"', value: it.inventario_minimo ?? 0, hint: '0 = no entra a la alerta de mínimos' })}
      ${campo({ id: 'iMax', label: 'Inventario máximo', type: 'number', attrs: 'min="0" step="1" inputmode="numeric"', ph: 'Sin máximo', value: it.inventario_maximo })}
      ${nuevo ? campo({ id: 'iStock', label: 'Stock inicial', type: 'number', attrs: 'min="0" step="1" inputmode="numeric"', ph: '0', hint: 'Entra como ajuste “Carga inicial” en el kardex.' }) : ''}
      <div class="field ${nuevo ? '' : 'span-2'}">
        <label>Cambio de pieza</label>
        <label class="switch" style="padding:11px 0;">
          <input type="checkbox" id="iCambio" ${it.requiere_cambio ? 'checked' : ''} /><span class="track"></span>
          Requiere entregar la pieza usada
        </label>
      </div>
    </div>`;
}

// Lee y valida el formulario de item. null si algo no es válido.
function leerItemForm(msgEl, nuevo) {
  msgEl.textContent = '';
  const np = val('iNP').trim();
  const desc = val('iDesc').trim();
  if (!np) return fallo(msgEl, 'iNP', 'El número de parte es obligatorio.');
  if (!desc) return fallo(msgEl, 'iDesc', 'La descripción es obligatoria.');
  const minTxt = val('iMin').trim();
  const min = minTxt === '' ? 0 : parseEntero(minTxt);
  if (min === null) return fallo(msgEl, 'iMin', 'El mínimo debe ser un entero de 0 en adelante.');
  const maxTxt = val('iMax').trim();
  const max = maxTxt === '' ? null : parseEntero(maxTxt);
  if (maxTxt !== '' && max === null) return fallo(msgEl, 'iMax', 'El máximo debe ser un entero de 0 en adelante.');
  if (max !== null && max < min) return fallo(msgEl, 'iMax', `El máximo (${max}) no puede ser menor al mínimo (${min}).`);
  const datos = {
    numero_parte: np,
    descripcion: desc,
    marca_modelo_uso: val('iUso').trim() || null,
    unidad_medida: val('iUM').trim() || 'EACH',
    inventario_minimo: min,
    inventario_maximo: max,
    requiere_cambio: $('#iCambio').checked,
  };
  if (nuevo) {
    const st = val('iStock').trim();
    const stock = st === '' ? 0 : parseEntero(st);
    if (stock === null) return fallo(msgEl, 'iStock', 'El stock inicial debe ser un entero de 0 en adelante.');
    datos.stock_inicial = stock;
  }
  return datos;
}

function abrirEditar() {
  const it = state.kx.item;
  openModal(`
    <div class="modal wide">
      <div class="mh"><h3>${I('edit')} Editar item</h3>
        <p>Cada campo que cambies queda en la bitácora con su valor anterior y el nuevo.</p></div>
      <div class="mb" style="display:flex;flex-direction:column;gap:12px;">
        ${itemFormHTML(it, false)}
        <p class="form-msg" id="edMsg"></p>
      </div>
      <div class="mf">
        <button class="btn ghost" data-close>Cancelar</button>
        <button class="btn primary" id="edOk">${I('save')} Guardar cambios</button>
      </div>
    </div>`);
  enterAvanza($('#itemForm'), () => $('#edOk').click());
  $('#edOk').onclick = async () => {
    const datos = leerItemForm($('#edMsg'), false);
    if (!datos) return;
    const cambios = {};
    for (const [k, v] of Object.entries(datos)) if (v !== it[k]) cambios[k] = v;
    if (!Object.keys(cambios).length) return ($('#edMsg').textContent = 'No hay cambios que guardar.');
    $('#edOk').disabled = true;
    try {
      await http('PATCH', `/items/${it.id}`, cambios);
      closeModal();
      const n = Object.keys(cambios).length;
      toast('ok', 'Item actualizado', `${n} campo(s) registrado(s) en la bitácora.`);
      state.kxTab = 'bit';
      await recargarItem();
    } catch (e) {
      $('#edMsg').textContent = e.message;
      $('#edOk').disabled = false;
    }
  };
}

function mountNuevo() {
  $('#view').innerHTML = `
  <div class="grid-2 split" style="grid-template-columns:minmax(0,1fr) 340px;align-items:start;gap:14px;" id="nuevoRoot">
    <div class="card">
      <div class="card-h"><h3>${I('plus')} Datos del item</h3><span class="sub"><kbd>Enter</kbd> pasa al siguiente campo</span></div>
      <div class="card-b">${itemFormHTML({}, true)}</div>
    </div>
    <div class="aside">
      <div class="card">
        <div class="card-h"><h3>Antes de dar de alta</h3></div>
        <div class="card-b checklist">
          <span>${I('checkCircle')} El número de parte no puede repetirse.</span>
          <span>${I('checkCircle')} El stock inicial queda en el kardex como ajuste “Carga inicial”.</span>
          <span>${I('checkCircle')} El alta queda en la bitácora a tu nombre.</span>
        </div>
        <div class="card-b" style="border-top:1px solid var(--line);display:flex;flex-direction:column;gap:10px;">
          <button class="btn primary lg block" id="nuevoOk">${I('plus')} Dar de alta</button>
          <p class="form-msg" id="nuevoMsg"></p>
        </div>
      </div>
    </div>
  </div>`;
  $('#iNP').focus();
  enterAvanza($('#nuevoRoot'), () => $('#nuevoOk').click());
  $('#nuevoOk').onclick = async () => {
    const datos = leerItemForm($('#nuevoMsg'), true);
    if (!datos) return;
    $('#nuevoOk').disabled = true;
    try {
      const it = await http('POST', '/items', datos);
      toast('ok', 'Item dado de alta', `${it.numero_parte} con stock ${fmt(it.stock_activo)}.`);
      state.kxTab = 'mov';
      go(`#/item/${it.id}`);
    } catch (e) {
      $('#nuevoMsg').textContent = e.message;
      if (e.status === 409) fallo(null, 'iNP', '');
      $('#nuevoOk').disabled = false;
    }
  };
}

/* ============================================================
   CAPTURAS: selector de item (flechas + Enter)
   ============================================================ */
function buscarLocal(items, q) {
  const nq = norm(q).trim();
  const terms = nq.split(/\s+/).filter(Boolean);
  if (!terms.length) return items.slice();
  const hits = [];
  for (const it of items) {
    const np = norm(it.numero_parte);
    const hay = `${np} ${norm(it.descripcion)} ${norm(it.marca_modelo_uso)}`;
    if (!terms.every((t) => hay.includes(t))) continue;
    const score = np === nq ? 0 : np.startsWith(nq) ? 1 : np.includes(nq) ? 2 : 3;
    hits.push([score, it]);
  }
  hits.sort((a, b) => a[0] - b[0] || a[1].numero_parte.localeCompare(b[1].numero_parte));
  return hits.map((h) => h[1]);
}

function mountPicker(el, items, onChange) {
  let sel = null;
  let matches = [];
  let idx = 0;

  function render() {
    if (sel) {
      el.innerHTML = `
        <div class="pk-sel">
          <div class="no">${I('box')}</div>
          <div class="info">
            <div class="np">${esc(sel.numero_parte)}</div>
            <div class="desc">${esc(sel.descripcion)}${sel.marca_modelo_uso ? ' · ' + esc(sel.marca_modelo_uso) : ''}</div>
            <div class="tags">${itemChips(sel)}</div>
          </div>
          <div class="right">
            <div class="stk-big"><small>Activo</small><b>${fmt(sel.stock_activo)}<span>${esc(sel.unidad_medida)}</span></b></div>
            <button class="btn sm" data-pk-clear>${I('x')} Cambiar</button>
          </div>
        </div>`;
      el.querySelector('[data-pk-clear]').onclick = () => { set(null); focus(); };
      return;
    }
    el.innerHTML = `
      <div class="picker">
        <div class="scan">
          <div class="qr">${I('search')}</div>
          <div class="body">
            <div class="lbl">Selecciona el item</div>
            <input id="pkInput" placeholder="Escribe o escanea el número de parte…" autocomplete="off" spellcheck="false" />
          </div>
          <div class="keys"><kbd>↑</kbd><kbd>↓</kbd><kbd>Enter</kbd></div>
        </div>
        <div class="pk-list hidden"></div>
      </div>`;
    const inp = el.querySelector('#pkInput');
    const list = el.querySelector('.pk-list');
    const update = (mostrarVacio = false) => {
      const q = inp.value;
      if (!q.trim() && !mostrarVacio) return list.classList.add('hidden');
      matches = buscarLocal(items, q).slice(0, 8);
      idx = Math.min(idx, Math.max(0, matches.length - 1));
      list.innerHTML = matches.length
        ? matches.map((it, i) => `
          <button class="pk-opt ${i === idx ? 'sel' : ''}" data-pk="${i}" tabindex="-1">
            <span class="np">${esc(it.numero_parte)}</span>
            <span class="desc">${esc(it.descripcion)}${it.marca_modelo_uso ? ` <small>· ${esc(it.marca_modelo_uso)}</small>` : ''}</span>
            <span class="stk">${itemChips(it)}${fmt(it.stock_activo)} <span class="muted" style="font-weight:600;">${esc(it.unidad_medida)}</span></span>
          </button>`).join('')
        : `<div class="pk-empty">Sin coincidencias para “${esc(q)}”.</div>`;
      list.classList.remove('hidden');
    };
    inp.addEventListener('input', () => { idx = 0; update(); });
    inp.addEventListener('focus', () => update());
    inp.addEventListener('blur', () => setTimeout(() => list.classList.add('hidden'), 120));
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.classList.contains('hidden')) return update(true);
        if (!matches.length) return;
        idx = (idx + (e.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
        $$('.pk-opt', list).forEach((b, i) => b.classList.toggle('sel', i === idx));
        const b = $$('.pk-opt', list)[idx];
        if (b) b.scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        if (!list.classList.contains('hidden') && matches[idx]) set(matches[idx]);
        else if (inp.value.trim()) update();
      } else if (e.key === 'Escape') {
        list.classList.add('hidden');
      }
    });
    list.addEventListener('mousedown', (e) => {
      const b = e.target.closest('[data-pk]');
      if (!b) return;
      e.preventDefault();
      set(matches[Number(b.dataset.pk)]);
    });
  }
  function set(it) {
    sel = it || null;
    idx = 0;
    render();
    onChange(sel);
  }
  function focus() {
    const inp = el.querySelector('#pkInput');
    if (inp) inp.focus();
  }
  render();
  return { get: () => sel, set, focus, refresh: render };
}

/* ---------- piezas comunes de las capturas ---------- */
async function cargarCatalogo(obsoletos) {
  $('#view').innerHTML = `<div class="loading">Cargando catálogo…</div>`;
  const res = await call('GET', `/items?obsoletos=${obsoletos ? 'true' : 'false'}`);
  return res ? res.items : null;
}

function capturaHTML({ titulo, campos, extra = '', boton, icono }) {
  return `
  <div class="grid-2 split" style="grid-template-columns:minmax(0,1fr) 340px;align-items:start;gap:14px;" id="capRoot">
    <div class="col" style="gap:14px;min-width:0;">
      <div class="card">
        <div class="card-h"><h3><span class="step">1</span> Item</h3><span class="sub">Busca por número de parte o descripción</span></div>
        <div class="card-b"><div id="picker"></div></div>
      </div>
      <div class="card">
        <div class="card-h"><h3><span class="step">2</span> ${titulo}</h3><span class="sub"><kbd>Enter</kbd> pasa al siguiente campo</span></div>
        <div class="card-b"><div class="form-grid" id="frm">${campos}</div></div>
      </div>
      ${extra}
    </div>
    <div class="aside">
      <div class="card">
        <div class="card-h"><h3>Resumen</h3></div>
        <div class="card-b" id="resumen"></div>
        <div class="card-b" style="border-top:1px solid var(--line);display:flex;flex-direction:column;gap:10px;">
          <button class="btn primary lg block" id="btnGuardar">${I(icono)} ${boton}</button>
          <p class="form-msg" id="frmMsg"></p>
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>${I('history')} En esta sesión</h3><span class="sub">tus últimas capturas</span></div>
        <div class="card-b" id="sesList" style="display:flex;flex-direction:column;gap:8px;"></div>
      </div>
    </div>
  </div>`;
}

function renderSesion() {
  const el = $('#sesList');
  if (!el) return;
  el.innerHTML = state.sesion.length
    ? state.sesion.slice(0, 6).map((s) => `
      <div class="sess-row" data-go="#/item/${s.item_id}">
        ${tipoChip(s.tipo)}
        <div style="min-width:0;"><span class="np">${esc(s.np)}</span><small>${esc(s.fecha)}</small></div>
        <div class="right"><span class="${s.cantidad > 0 ? 'pos' : 'neg'}">${signed(s.cantidad)}</span><small>stock ${fmt(s.stock)}</small></div>
      </div>`).join('')
    : `<span class="muted" style="font-size:12.5px;">Aquí aparecen los movimientos que registres.</span>`;
}

function flowHTML(item, delta) {
  if (!item)
    return `<div class="empty" style="padding:16px 8px;"><div class="ic">${I('box')}</div>Selecciona un item para ver su stock.</div>`;
  const fin = delta === null ? null : item.stock_activo + delta;
  const bad = fin !== null && fin < 0;
  return `
    <div class="flow">
      <div class="box"><small>Actual</small><b>${fmt(item.stock_activo)}</b></div>
      <div class="arrow">${I('arrowR')}<span class="${delta > 0 ? 'pos' : delta < 0 ? 'neg' : ''}">${delta ? signed(delta) : ''}</span></div>
      <div class="box ${bad ? 'bad' : ''}"><small>Después</small><b>${fin === null ? '—' : fmt(fin)}</b></div>
    </div>
    ${bad ? `<p class="form-msg" style="text-align:center;margin-top:8px;">No alcanza: hay ${fmt(item.stock_activo)} ${esc(item.unidad_medida)}.</p>` : ''}`;
}

/* Arma una pantalla de captura: selector, resumen, Enter-avanza y envío.
   - delta(item): cambio de stock que se previsualiza (o null).
   - leer(item, msgEl): cuerpo validado para la API (o null).
   - confirmar(item, body): opcional; promesa true/false antes de enviar. */
function setupCaptura({ items, preselect, path, delta, leer, onItem, confirmar: preguntar, okTitulo }) {
  const root = $('#capRoot');
  const msg = $('#frmMsg');
  let item = null;
  const resumen = () => ($('#resumen').innerHTML = flowHTML(item, item ? delta(item) : null));
  const pk = mountPicker($('#picker'), items, (it) => {
    item = it;
    msg.textContent = '';
    if (onItem) onItem(it);
    resumen();
    if (it) setTimeout(() => { const c = $('#fCant'); if (c) c.focus(); }, 0);
  });
  root.addEventListener('input', resumen);
  root.addEventListener('click', (e) => { if (e.target.closest('[data-signo]')) resumen(); });
  enterAvanza(root, () => $('#btnGuardar').click());
  renderSesion();
  resumen();

  if (preselect) {
    const it = items.find((x) => x.id === Number(preselect));
    if (it) pk.set(it);
    else toast('warn', 'Item no disponible', 'Ese item no existe o está obsoleto.');
  }
  if (!pk.get()) pk.focus();

  $('#btnGuardar').onclick = async () => {
    msg.textContent = '';
    if (!item) {
      msg.textContent = 'Selecciona un item.';
      return pk.focus();
    }
    const body = leer(item, msg);
    if (!body) return;
    if (preguntar && !(await preguntar(item, body))) return;
    const btn = $('#btnGuardar');
    btn.disabled = true;
    try {
      const mov = await http('POST', path, body);
      const antes = item.stock_activo;
      item.stock_activo = mov.stock_resultante;
      recalcBajoMinimo(item);
      state.sesion.unshift({
        tipo: mov.tipo, np: item.numero_parte, item_id: item.id,
        cantidad: mov.cantidad, stock: mov.stock_resultante, fecha: mov.fecha,
      });
      toast('ok', okTitulo, `${item.numero_parte}: ${fmt(antes)} → ${fmt(mov.stock_resultante)}`);
      $$('[data-f]', root).forEach((x) => { if (x.id !== 'pkInput') x.value = ''; });
      renderSesion();
      pk.set(null);
      pk.focus();
    } catch (e) {
      msg.textContent = e.message;
      toast('err', 'No se registró', e.message);
      // La contraseña del supervisor nunca se queda escrita tras un rechazo.
      $$('input[type="password"]', root).forEach((x) => (x.value = ''));
      if (e.status === 409) {
        // El stock cambió (otra captura) o el item se volvió obsoleto: refresca.
        const fresco = await call('GET', `/items/${item.id}`);
        if (fresco) {
          Object.assign(item, fresco.item);
          pk.refresh();
          resumen();
        }
      }
    } finally {
      btn.disabled = false;
    }
  };
  return { pk, resumen };
}

/* ============================================================
   ENTRADA
   ============================================================ */
async function mountEntrada(r) {
  const items = await cargarCatalogo(false);
  if (!items || state.route.name !== 'entrada') return;
  $('#view').innerHTML = capturaHTML({
    titulo: 'Datos de la entrada', boton: 'Registrar entrada', icono: 'arrowIn',
    campos:
      campo({ id: 'fCant', label: 'Cantidad', req: true, cls: 'qty', type: 'number', attrs: 'min="1" step="1" inputmode="numeric"' }) +
      campo({ id: 'fOC', label: 'Orden de compra', req: true, mono: true }) +
      campo({ id: 'fProv', label: 'Proveedor', req: true, span: true }) +
      campo({ id: 'fCom', label: 'Comentario', span: true, ph: 'Opcional' }),
  });
  setupCaptura({
    items, preselect: r.query.item, path: '/movimientos/entrada', okTitulo: 'Entrada registrada',
    delta: () => parseEntero(val('fCant')),
    leer: (it, m) => {
      const cantidad = parseEntero(val('fCant'));
      if (!cantidad) return fallo(m, 'fCant', 'La cantidad debe ser un entero mayor a 0.');
      if (!val('fOC').trim()) return fallo(m, 'fOC', 'La orden de compra es obligatoria.');
      if (!val('fProv').trim()) return fallo(m, 'fProv', 'El proveedor es obligatorio.');
      return {
        item_id: it.id, cantidad,
        orden_compra: val('fOC'), proveedor: val('fProv'), comentario: val('fCom') || null,
      };
    },
  });
}

/* ============================================================
   SALIDA (con cambio de pieza)
   ============================================================ */
async function mountSalida(r) {
  const items = await cargarCatalogo(false);
  if (!items || state.route.name !== 'salida') return;
  $('#view').innerHTML = capturaHTML({
    titulo: 'Datos de la salida', boton: 'Registrar salida', icono: 'arrowOut',
    campos:
      campo({ id: 'fCant', label: 'Cantidad', req: true, cls: 'qty', type: 'number', attrs: 'min="1" step="1" inputmode="numeric"' }) +
      campo({ id: 'fDepto', label: 'Departamento destino', req: true }) +
      campo({ id: 'fSolNom', label: 'Nómina del solicitante', req: true, mono: true, ph: 'ATI…' }) +
      campo({ id: 'fSolNombre', label: 'Nombre del solicitante', req: true }) +
      campo({ id: 'fCom', label: 'Comentario', span: true, ph: 'Opcional' }),
    extra: `<div id="cambioCard"></div>`,
  });

  let cambio = null; // 'pieza' | 'excepcion'
  const renderCambio = (it) => {
    cambio = null;
    const card = $('#cambioCard');
    if (!it || !it.requiere_cambio) return (card.innerHTML = '');
    card.innerHTML = `
      <div class="card">
        <div class="card-h"><h3><span class="step">3</span> Cambio de pieza</h3><span class="sub">Este item se entrega a cambio de la pieza usada</span></div>
        <div class="card-b" style="display:flex;flex-direction:column;gap:12px;">
          <div class="choices">
            <button class="choice" data-cambio="pieza"><span class="dot"></span>
              <span><b>Entregó la pieza usada</b><small>Queda como constancia; no suma stock.</small></span></button>
            <button class="choice" data-cambio="excepcion"><span class="dot"></span>
              <span><b>No entregó pieza</b><small>Excepción: la autoriza un supervisor.</small></span></button>
          </div>
          <div id="excBox"></div>
        </div>
      </div>`;
    $$('[data-cambio]', card).forEach((b) => (b.onclick = () => {
      cambio = b.dataset.cambio;
      $$('[data-cambio]', card).forEach((x) => x.classList.toggle('on', x === b));
      $('#excBox').innerHTML = cambio !== 'excepcion' ? '' : esSup()
        ? `<div class="banner info" style="margin-bottom:12px;">${I('shield')}<span>Eres supervisor: la excepción queda autorizada a tu nombre (<b>${esc(state.user.nomina)}</b>).</span></div>
           <div class="field"><label for="fExcMotivo">Motivo de la excepción<span class="req">*</span></label>
             <input class="input big" id="fExcMotivo" data-f autocomplete="off" /></div>`
        : `<div class="auth-box">
             <div class="row" style="gap:8px;font-weight:700;font-size:13.5px;">${I('key')} Autorización de supervisor
               <span class="muted" style="font-weight:500;font-size:12.5px;">— el supervisor teclea sus datos aquí mismo</span></div>
             <div class="form-grid">
               ${campo({ id: 'fSupNom', label: 'Nómina del supervisor', req: true, mono: true })}
               ${campo({ id: 'fSupPass', label: 'Contraseña del supervisor', req: true, type: 'password' })}
               ${campo({ id: 'fExcMotivo', label: 'Motivo de la excepción', req: true, span: true })}
             </div>
           </div>`;
      const f = $('#excBox [data-f]');
      if (f) f.focus();
    }));
  };

  setupCaptura({
    items, preselect: r.query.item, path: '/movimientos/salida', okTitulo: 'Salida registrada',
    onItem: renderCambio,
    delta: () => { const c = parseEntero(val('fCant')); return c === null ? null : -c; },
    leer: (it, m) => {
      const cantidad = parseEntero(val('fCant'));
      if (!cantidad) return fallo(m, 'fCant', 'La cantidad debe ser un entero mayor a 0.');
      if (cantidad > it.stock_activo) return fallo(m, 'fCant', `No hay suficiente stock: hay ${fmt(it.stock_activo)}.`);
      if (!val('fDepto').trim()) return fallo(m, 'fDepto', 'El departamento destino es obligatorio.');
      if (!val('fSolNom').trim()) return fallo(m, 'fSolNom', 'La nómina del solicitante es obligatoria.');
      if (!val('fSolNombre').trim()) return fallo(m, 'fSolNombre', 'El nombre del solicitante es obligatorio.');
      const body = {
        item_id: it.id, cantidad,
        departamento_destino: val('fDepto'), solicitante_nomina: val('fSolNom'),
        solicitante_nombre: val('fSolNombre'), comentario: val('fCom') || null,
        pieza_devuelta: false, excepcion: null,
      };
      if (it.requiere_cambio) {
        if (!cambio) return fallo(m, null, 'Indica si el solicitante entregó la pieza usada.');
        if (cambio === 'pieza') body.pieza_devuelta = true;
        else {
          if (!esSup()) {
            if (!val('fSupNom').trim()) return fallo(m, 'fSupNom', 'Falta la nómina del supervisor.');
            if (!val('fSupPass')) return fallo(m, 'fSupPass', 'Falta la contraseña del supervisor.');
          }
          if (!val('fExcMotivo').trim()) return fallo(m, 'fExcMotivo', 'El motivo de la excepción es obligatorio.');
          body.excepcion = {
            motivo: val('fExcMotivo'),
            supervisor_nomina: esSup() ? null : val('fSupNom'),
            supervisor_password: esSup() ? null : val('fSupPass'),
          };
        }
      }
      return body;
    },
  });
}

/* ============================================================
   AJUSTE (solo supervisor)
   ============================================================ */
async function mountAjuste(r) {
  const items = await cargarCatalogo(true); // sí se permite en obsoletos
  if (!items || state.route.name !== 'ajuste') return;
  let signo = -1;
  $('#view').innerHTML = capturaHTML({
    titulo: 'Datos del ajuste', boton: 'Revisar ajuste', icono: 'sliders',
    campos: `
      <div class="field span-2"><label>Tipo de ajuste<span class="req">*</span></label>
        <div class="seg" style="align-self:flex-start;">
          <button data-signo="1">${I('plus')} Sumar (+)</button>
          <button data-signo="-1" class="active">${I('minus')} Restar (−)</button>
        </div>
      </div>` +
      campo({ id: 'fCant', label: 'Cantidad', req: true, cls: 'qty', type: 'number', attrs: 'min="1" step="1" inputmode="numeric"' }) +
      `<div></div>` +
      campo({ id: 'fMotivo', label: 'Motivo', req: true, span: true, ph: 'Ej. conteo físico del 21/10: había 3 de menos' }),
  });
  $$('[data-signo]').forEach((b) => (b.onclick = () => {
    signo = Number(b.dataset.signo);
    $$('[data-signo]').forEach((x) => x.classList.toggle('active', x === b));
    $('#fCant').focus();
  }));
  setupCaptura({
    items, preselect: r.query.item, path: '/movimientos/ajuste', okTitulo: 'Ajuste registrado',
    delta: () => { const c = parseEntero(val('fCant')); return c === null ? null : signo * c; },
    leer: (it, m) => {
      const c = parseEntero(val('fCant'));
      if (!c) return fallo(m, 'fCant', 'La cantidad debe ser un entero mayor a 0.');
      if (it.stock_activo + signo * c < 0) return fallo(m, 'fCant', `El ajuste dejaría el stock en negativo: hay ${fmt(it.stock_activo)}.`);
      if (!val('fMotivo').trim()) return fallo(m, 'fMotivo', 'El motivo del ajuste es obligatorio.');
      return { item_id: it.id, cantidad: signo * c, motivo: val('fMotivo') };
    },
    confirmar: (it, body) => confirmar({
      title: `${I('sliders')} Confirmar ajuste`,
      sub: `<span class="mono">${esc(it.numero_parte)}</span> · ${esc(it.descripcion)}`,
      body: `${flowHTML(it, body.cantidad)}
        <dl class="kv" style="margin-top:14px;">
          <dt>Ajuste</dt><dd class="${body.cantidad > 0 ? 'pos' : 'neg'}">${signed(body.cantidad)} ${esc(it.unidad_medida)}</dd>
          <dt>Motivo</dt><dd>${esc(body.motivo)}</dd>
          <dt>Registra</dt><dd class="mono">${esc(state.user.nomina)}</dd>
        </dl>
        <p class="muted" style="font-size:12px;margin:12px 0 0;">El ajuste queda en el kardex y no se puede editar ni borrar.</p>`,
      ok: 'Confirmar ajuste',
    }),
  });
}

/* ============================================================
   CHEQUEO DE STOCK (solo supervisor)
   ============================================================ */
async function mountChequeo() {
  $('#view').innerHTML = `<div class="loading">Revisando…</div>`;
  const res = await call('GET', '/chequeo-stock');
  if (!res || state.route.name !== 'chequeo') return;
  $('#view').innerHTML = `
    <div class="card">
      <div class="card-h"><h3>${I('shield')} Stock activo vs. suma de movimientos</h3>
        <span class="sub">Lee vw_chequeo_stock; siempre debe salir vacía</span>
        <div class="right"><button class="btn sm" id="btnRevisar">${I('refresh')} Volver a revisar</button></div></div>
      <div class="card-b">
        ${res.ok
          ? `<div class="banner info">${I('checkCircle')}<span><b>Todo cuadra.</b> En todos los items el stock activo es igual a la suma de sus movimientos.</span></div>`
          : `<div class="banner danger" style="margin-bottom:12px;">${I('alert')}<span><b>${res.filas.length} item(s) no cuadran.</b> El cache de stock no coincide con el kardex. Avisa a sistemas antes de capturar más movimientos de estos items.</span></div>
             <table class="tbl">
               <thead><tr><th>Número de parte</th><th class="r">Stock activo</th><th class="r">Suma movimientos</th><th class="r">Diferencia</th><th></th></tr></thead>
               <tbody>${res.filas.map((f) => `
                 <tr class="click" data-go="#/item/${f.item_id}">
                   <td class="np">${esc(f.numero_parte)}</td><td class="num r">${fmt(f.stock_activo)}</td>
                   <td class="num r">${fmt(f.suma_movimientos)}</td><td class="num r neg">${signed(f.diferencia)}</td>
                   <td class="r">${I('chevR')}</td>
                 </tr>`).join('')}</tbody>
             </table>`}
      </div>
    </div>`;
  $('#btnRevisar').onclick = () => mountChequeo();
}

/* ============================================================
   TRÁNSITO POR ASIGNAR (solo supervisor)
   Líneas de OC abiertas en Infor cuya primera palabra de la referencia no
   es ningún número de parte de Kardex. El supervisor las relaciona a mano.
   ============================================================ */
async function mountTransito() {
  $('#view').innerHTML = `<div class="loading">Consultando Infor…</div>`;
  const [res, cat] = await Promise.all([
    call('GET', '/transito/por-asignar'),
    call('GET', '/items?obsoletos=false'), // nunca se asigna a un obsoleto
  ]);
  if (!res || !cat || state.route.name !== 'transito') return;
  state.tr = res;
  state.trItems = cat.items;
  renderTransito();
}

function renderTransito() {
  const { disponible, lineas } = state.tr;
  let body;
  if (!disponible) {
    body = `<div class="banner lock">${I('wifiOff')}<span><b>Tránsito de Infor no disponible.</b> No se pudo consultar el API de órdenes de compra. Intenta de nuevo en unos minutos.</span></div>`;
  } else if (!lineas.length) {
    body = `<div class="empty"><div class="ic">${I('checkCircle')}</div><b>Nada por asignar</b>Todas las líneas abiertas de Infor ya tienen item.</div>`;
  } else {
    body = `<div class="tr-grid">${lineas.map((l, i) => tarjetaTransito(l,
      `<span class="muted">Código <span class="mono">${l.codigo ? esc(l.codigo) : '—'}</span> no es un número de parte</span>
       <button class="btn sm primary tr-act" data-asignar="${i}">${I('box')} Asignar a un item</button>`)).join('')}</div>`;
  }
  $('#view').innerHTML = `
    <div class="banner info" style="margin-bottom:14px;">${I('info')}<span>
      Una línea de OC se relaciona <b>sola</b> cuando la primera palabra de su referencia es el número de parte en Kardex.
      Aquí salen las que no empataron. Al asignarla, suma al <b>En tránsito</b> del item hasta que se recibe en Infor.</span></div>
    <div class="card">
      <div class="card-h"><h3>${I('truck')} Líneas sin item</h3>
        <span class="sub">${disponible ? `${lineas.length} línea(s)` : 'N/D'}</span>
        <div class="right"><button class="btn sm" id="btnTrRevisar">${I('refresh')} Volver a consultar</button></div></div>
      <div class="card-b">${body}</div>
    </div>`;
  $('#btnTrRevisar').onclick = () => mountTransito();
  $$('[data-asignar]').forEach((b) => (b.onclick = () => abrirAsignar(lineas[Number(b.dataset.asignar)])));
}

function abrirAsignar(l) {
  openModal(`
    <div class="modal wide">
      <div class="mh"><h3>${I('truck')} Asignar línea en tránsito</h3>
        <p>Elige el item al que pertenece. Queda en su bitácora a tu nombre.</p></div>
      <div class="mb" style="display:flex;flex-direction:column;gap:14px;">
        ${tarjetaTransito(l)}
        <div id="asgPicker"></div>
        <p class="form-msg" id="asgMsg"></p>
      </div>
      <div class="mf">
        <button class="btn ghost" data-close>Cancelar</button>
        <button class="btn primary" id="asgOk">${I('checkCircle')} Asignar</button>
      </div>
    </div>`);
  const msg = $('#asgMsg');
  const pk = mountPicker($('#asgPicker'), state.trItems, () => {
    msg.textContent = '';
    if (pk && pk.get()) $('#asgOk').focus();
  });
  $('#asgOk').onclick = async () => {
    msg.textContent = '';
    const it = pk.get();
    if (!it) {
      msg.textContent = 'Selecciona el item.';
      return pk.focus();
    }
    $('#asgOk').disabled = true;
    try {
      await http('POST', '/transito/asignar', { doc_num: l.doc_num, line_num: l.line_num, item_id: it.id });
      closeModal();
      toast('ok', 'Línea asignada', `${claveLinea(l)} → ${it.numero_parte}: suma ${fmt(l.cantidad)} a su tránsito.`);
      state.tr.lineas = state.tr.lineas.filter((x) => x !== l);
      renderTransito();
    } catch (e) {
      msg.textContent = e.message;
      $('#asgOk').disabled = false;
      // Ya no está abierta o alguien más la asignó: refresca la lista al cerrar.
      if (e.status === 404 || e.status === 409) _modalOnClose = () => mountTransito();
    }
  };
}

/* ============================================================
   EVENTOS GLOBALES Y ARRANQUE
   ============================================================ */
document.addEventListener('click', (e) => {
  const g = e.target.closest('[data-go]');
  if (g && !g.disabled) {
    e.preventDefault();
    return go(g.dataset.go);
  }
  if (e.target.closest('[data-close]')) closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#scrim').classList.contains('show')) closeModal();
});
$('#scrim').addEventListener('mousedown', (e) => {
  if (e.target.id === 'scrim') closeModal();
});
window.addEventListener('hashchange', route);

async function arranque() {
  initLogin();
  try {
    const h = await (await fetch('/api/health')).json();
    state.version = h.version || '';
  } catch (_) {}
  $('#loginFoot').textContent = `Kardex${state.version ? ' · v' + state.version : ''} · American Tubing`;
  if (getToken()) {
    try {
      state.user = await http('GET', '/yo', undefined, { silent401: true });
      return entrarApp();
    } catch (_) {
      setToken(null);
    }
  }
  mostrarLogin();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', arranque);
else arranque();
