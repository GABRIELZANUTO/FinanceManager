// Shared helpers: DOM builder, API client, toasts, dialogs, form helpers.
import { config, formatNumber } from './format.js';
export { formatMoney, formatNumber } from './format.js';

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Create an element. Strings become text nodes (never HTML). */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.setAttribute('class', value);
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
}

function append(node, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function spriteUrl() {
  const meta = document.querySelector('meta[name="sprite"]');
  return document.body.dataset.sprite || (meta ? meta.content : '');
}

/** An icon from the self-hosted sprite (Lucide). */
export function icon(name, cssClass = '') {
  const node = svg('svg', { class: `icon ${cssClass}`.trim(), 'aria-hidden': 'true', focusable: 'false' });
  const use = svg('use');
  use.setAttribute('href', `${spriteUrl()}#${name}`);
  node.append(use);
  return node;
}

export function debounce(fn, wait = 250) {
  let timer;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function readJson(id, fallback = null) {
  const node = document.getElementById(id);
  if (!node) return fallback;
  try { return JSON.parse(node.textContent); } catch (e) { return fallback; }
}

/* ---------- API ---------- */

export class ApiError extends Error {
  constructor(status, message, errors = {}) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

/** The record changed elsewhere since the screen loaded it (HTTP 409, code "conflict"). */
export class ConflictError extends ApiError {
  constructor(data) {
    super(409, data.message || 'Este registro foi alterado em outro lugar.', {});
    this.current = data.current || {};
    this.version = data.version;
    this.updatedBy = data.updated_by || '';
    this.updatedAt = data.updated_at || '';
  }
}

/** An error the person already answered (in the conflict dialog): nothing more to show. */
function handled(error) {
  error.handled = true;
  return error;
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : '';
}

/* ---------- system status: saving, saved, offline ---------- */

let statusTimer;
export function setStatus(text, offline = false) {
  const node = document.getElementById('sys-status');
  if (!node) return;
  node.textContent = text;
  node.classList.toggle('offline', offline);
  clearTimeout(statusTimer);
  if (text && !offline) statusTimer = setTimeout(() => { if (navigator.onLine) node.textContent = ''; }, 2200);
}
window.addEventListener('offline', () => setStatus('Você está offline. As alterações não serão salvas.', true));
window.addEventListener('online', () => setStatus('Conexão restabelecida'));

/** Call the JSON API. ``version``: the version of the record the screen opened; a write based on
 *  an older one is refused by the server and the conflict dialog asks what to do. ``conflict``:
 *  { copyPath } to offer "Salvar as minhas como novo", { onCurrent(current) } to refill the screen
 *  with what is saved now (default: reload the page), or false to let the caller handle it. */
export async function api(path, options = {}) {
  const { method = 'GET', version, conflict } = options;
  let { body, query } = options;
  if (version !== undefined && version !== null && method !== 'GET') {
    if (body !== undefined) body = { ...body, version };
    else query = { ...(query || {}), version };
  }
  if (method !== 'GET') setStatus('Salvando...');
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== '' && value !== null && value !== undefined) params.set(key, value);
    }
    const text = params.toString();
    if (text) url += (url.includes('?') ? '&' : '?') + text;
  }
  const headers = { Accept: 'application/json', 'X-Requested-With': 'fetch' };
  const init = { method, headers, credentials: 'same-origin' };
  if (method !== 'GET') headers['X-CSRFToken'] = csrfToken();
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (e) {
    setStatus(navigator.onLine ? 'Não foi possível acessar o servidor' : 'Você está offline. As alterações não serão salvas.', true);
    throw new ApiError(0, 'Não foi possível acessar o servidor. Verifique sua conexão e tente novamente.');
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* non JSON */ }
  if (response.status === 401) {
    window.location.assign('/login/?next=' + encodeURIComponent(window.location.pathname));
    throw new ApiError(401, 'Entre novamente.');
  }
  if (method !== 'GET' && response.ok) setStatus('Salvo');
  if (method !== 'GET' && !response.ok) setStatus('');
  if (response.status === 409 && data && data.code === 'conflict') {
    const error = new ConflictError(data);
    if (conflict === false || version === undefined || version === null) throw error;
    return resolveConflict(path, { ...options, body, query }, error);
  }
  if (!response.ok || !data || data.ok === false) {
    const fallback = response.status >= 500 ? 'O servidor teve um problema. Tente novamente.' : 'A solicitação falhou.';
    throw new ApiError(response.status, (data && data.message) || fallback, (data && data.errors) || {});
  }
  return data;
}

async function resolveConflict(path, options, error) {
  const { body, query, conflict = {} } = options;
  const choice = await conflictDialog({ mine: body || {}, error, allowCopy: Boolean(conflict.copyPath) });
  if (choice === 'mine') {
    const again = { ...options, version: undefined, conflict: false };
    if (body !== undefined) {
      again.body = { ...body, force: true };
      delete again.body.version;
    } else {
      again.query = { ...(query || {}), force: 1 };
      delete again.query.version;
    }
    return api(path, again);
  }
  if (choice === 'copy') {
    const copy = { ...body };
    delete copy.version;
    delete copy.force;
    return api(conflict.copyPath, { method: 'POST', body: copy });
  }
  if (choice === 'current') {
    if (conflict.onCurrent) conflict.onCurrent(error.current);
    else window.location.reload();
  }
  throw handled(error);
}

/* ---------- conflict dialog ---------- */

export const FIELD_LABELS = {
  name: 'Nome', amount: 'Valor', date: 'Data', kind: 'Tipo', category: 'Categoria', account: 'Conta', payee: 'Favorecido',
  note: 'Observação', tags: 'Tags', type: 'Tipo de conta', opening_balance: 'Saldo inicial', archived: 'Arquivada',
  credit_limit: 'Limite', due_day: 'Dia do vencimento', closing_day: 'Dia do fechamento', color: 'Cor', icon: 'Ícone',
  parent: 'Categoria pai', target_amount: 'Valor da meta', target_date: 'Data da meta', frequency: 'Frequência',
  start_date: 'Início', end_date: 'Fim', next_due: 'Próximo vencimento', active: 'Ativa', description: 'Descrição',
  due_date: 'Vencimento', pattern: 'Texto da regra', priority: 'Prioridade', query: 'Filtro', planned: 'Prevista',
  installment_first: 'Primeira parcela', installment_total: 'Total de parcelas', reconciled: 'Conciliada',
};

function plain(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map(plain).join(', ');
  if (typeof value === 'object') return String(value.id ?? value.name ?? '');
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  const text = String(value).trim();
  const number = Number(text.replace(',', '.'));
  return text !== '' && Number.isFinite(number) && /^-?[0-9.,]+$/.test(text) ? String(number) : text;
}

function shown(key, value) {
  if (value === null || value === undefined || value === '') return '(vazio)';
  if (typeof value === 'boolean' || value === 'true' || value === 'false') return value === true || value === 'true' ? 'Sim' : 'Não';
  if (Array.isArray(value)) return value.map((v) => (typeof v === 'object' ? v.name : v)).join(', ') || '(vazio)';
  if (typeof value === 'object') return value.name || String(value.id ?? '');
  const option = document.querySelector(`dialog[open] select[name="${CSS.escape(key)}"] option[value="${CSS.escape(String(value))}"]`);
  return option ? option.textContent.trim() : String(value);
}

function when(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
}

/** Ask what to do with a write refused because the record changed: 'mine', 'current', 'copy' or ''. */
export function conflictDialog({ mine, error, allowCopy = false }) {
  const dialog = document.getElementById('conflict-dialog');
  if (!dialog) return Promise.resolve(window.confirm(`${error.message} Salvar as suas alterações por cima?`) ? 'mine' : 'current');
  const rows = Object.keys(FIELD_LABELS)
    .filter((key) => key in mine && key in error.current && plain(mine[key]) !== plain(error.current[key]))
    .map((key) => h('tr', {}, h('th', { scope: 'row', text: FIELD_LABELS[key] }),
      h('td', { text: shown(key, mine[key]) }), h('td', { text: shown(key, error.current[key]) })));
  const who = [error.updatedBy && `por ${error.updatedBy}`, when(error.updatedAt) && `em ${when(error.updatedAt)}`].filter(Boolean).join(' ');
  dialog.querySelector('#conflict-message').textContent =
    `Alguém salvou este registro depois que você o abriu${who ? ` (${who})` : ''}. Escolha o que fica.`;
  dialog.querySelector('#conflict-rows').replaceChildren(...(rows.length ? rows
    : [h('tr', {}, h('td', { colspan: '3', class: 'muted', text: 'Os campos deste formulário não mudaram; outra parte do registro mudou.' }))]));
  dialog.querySelector('[data-choice="copy"]').hidden = !allowCopy;
  return new Promise((resolve) => {
    dialog.returnValue = '';
    dialog.addEventListener('close', () => resolve(dialog.returnValue), { once: true });
    openDialog(dialog, '[data-choice="current"]');
  });
}

/* ---------- toasts ---------- */

export function toast(message, { kind = 'success', action = null, timeout = 5000 } = {}) {
  const host = document.getElementById('toasts');
  if (!host) return null;
  const node = h('div', { class: `toast ${kind}` }, h('span', { text: message }));
  const remove = () => node.remove();
  if (action) {
    node.append(h('button', {
      type: 'button', text: action.label,
      onclick: async () => { remove(); await action.onClick(); },
    }));
  }
  host.append(node);
  if (timeout) setTimeout(remove, action ? Math.max(timeout, 8000) : timeout);
  return node;
}

export function showError(error) {
  if (error && error.handled) return;
  toast(error instanceof ApiError ? error.message : 'Algo deu errado.', { kind: 'error', timeout: 7000 });
}

/* ---------- dialogs ---------- */

// Modals close only from their X or Cancel buttons (or Esc). Clicking outside does nothing, and
// leaving a form with changes not saved asks first. The values when the modal opened are kept so
// "changed" can be told apart from "opened and looked".
function formState(dialog) {
  return JSON.stringify([...dialog.querySelectorAll('input, select, textarea')]
    .filter((el) => el.type !== 'hidden' || el.name)
    .map((el) => (el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value)));
}

export function openDialog(dialog, focusSelector) {
  const opener = document.activeElement;
  if (!dialog.open) dialog.showModal();
  const target = focusSelector ? dialog.querySelector(focusSelector) : null;
  if (target) target.focus();
  // a frame later, so values a script fills right after opening count as the starting point
  requestAnimationFrame(() => { dialog.initialState = formState(dialog); });
  dialog.addEventListener('close', () => {
    dialog.initialState = undefined;
    if (opener && document.contains(opener)) opener.focus();
  }, { once: true });
}

export function isDirty(dialog) {
  return dialog.initialState !== undefined && !dialog.hasAttribute('data-no-confirm') && Boolean(dialog.querySelector('form'))
    && formState(dialog) !== dialog.initialState;
}

export async function requestClose(dialog) {
  if (!dialog.open || dialog.dataset.asking) return;
  if (isDirty(dialog)) {
    dialog.dataset.asking = '1';
    const leave = await confirmDialog({
      title: 'Descartar alterações?',
      message: 'Você mudou alguns campos e ainda não salvou. Se sair agora, essas alterações serão perdidas.',
      confirmLabel: 'Descartar', cancelLabel: 'Continuar editando',
    });
    delete dialog.dataset.asking;
    if (!leave) return;
  }
  dialog.close();
}

export function wireDialog(dialog) {
  if (dialog.dataset.wired) return;
  dialog.dataset.wired = '1';
  dialog.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) requestClose(dialog);
  });
  dialog.addEventListener('cancel', (event) => {   // Esc: same as Cancel
    event.preventDefault();
    requestClose(dialog);
  });
}

export function confirmDialog({
  title = 'Tem certeza?', message = '', confirmLabel = 'Excluir', cancelLabel = 'Cancelar', danger = true,
} = {}) {
  const dialog = document.getElementById('confirm-dialog');
  dialog.querySelector('#confirm-title').textContent = title;
  dialog.querySelector('#confirm-message').textContent = message;
  dialog.querySelector('[data-cancel]').textContent = cancelLabel;
  const ok = dialog.querySelector('[data-ok]');
  ok.textContent = confirmLabel;
  ok.classList.toggle('btn-danger', danger);
  ok.classList.toggle('btn-primary', !danger);
  return new Promise((resolve) => {
    dialog.returnValue = '';
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    openDialog(dialog, '[data-cancel]');
  });
}

/* ---------- forms ---------- */

export function clearErrors(form) {
  $$('[data-error-for]', form).forEach((node) => { node.textContent = ''; });
  $$('[aria-invalid]', form).forEach((node) => node.removeAttribute('aria-invalid'));
  const box = $('[data-form-error]', form);
  if (box) { box.hidden = true; box.textContent = ''; }
}

export function showErrors(form, error) {
  clearErrors(form);
  if (error && error.handled) return;
  let general = [];
  const errors = error.errors || {};
  for (const [field, messages] of Object.entries(errors)) {
    const slot = form.querySelector(`[data-error-for="${CSS.escape(field)}"]`);
    if (slot && field !== '__all__') {
      slot.textContent = messages.join(' ');
      const input = form.querySelector(`[name="${CSS.escape(field)}"]`);
      if (input) input.setAttribute('aria-invalid', 'true');
    } else {
      general = general.concat(messages);
    }
  }
  if (!general.length && !Object.keys(errors).length) general = [error.message];
  const box = $('[data-form-error]', form);
  if (box && general.length) { box.textContent = general.join(' '); box.hidden = false; }
  const first = form.querySelector('[aria-invalid="true"]');
  if (first) first.focus();
}

export function formData(form) {
  const data = {};
  for (const [key, value] of new FormData(form).entries()) data[key] = value;
  for (const input of $$('input[data-money]', form)) data[input.name] = parseMoney(input.value);
  for (const input of $$('input[type="checkbox"]', form)) data[input.name] = input.checked;
  return data;
}

/* ---------- money inputs: thousands separators while keeping a plain decimal for the server ---------- */

const GROUP = { intl: ',', indian: ',', eu: '.', space: ' ' };
const DECIMAL = { intl: '.', indian: '.', eu: ',', space: ',' };

export function parseMoney(text) {
  let value = String(text || '').replace(/[\s\u00a0\u202f]/g, '');
  if (!value) return '';
  const format = config.format;
  const group = GROUP[format];
  const decimal = DECIMAL[format];
  if (decimal === ',' && !value.includes(',') && /^-?\d+\.\d{1,2}$/.test(value)) return value;
  if (group && group !== ' ') value = value.split(group).join('');
  if (decimal !== '.') value = value.replace(decimal, '.');
  return /^-?\d*\.?\d*$/.test(value) ? value : String(text);
}

export function setMoney(input, value) {
  input.value = value === '' || value === null || value === undefined ? '' : formatNumber(value);
}

export function wireMoneyInputs(root = document) {
  for (const input of $$('input[data-money]', root)) {
    if (input.dataset.moneyWired) continue;
    input.dataset.moneyWired = '1';
    input.addEventListener('blur', () => {
      const plain = parseMoney(input.value);
      if (plain !== '' && /^-?\d*\.?\d+$|^-?\d+\.$/.test(plain) && Number.isFinite(Number(plain))) {
        if (!(input.hasAttribute('data-signed')) && Number(plain) < 0) return;
        setMoney(input, plain);
      }
    });
  }
}

export async function withBusy(button, task) {
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try { return await task(); } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

/* ---------- states ---------- */

export function errorState(message, retry) {
  const box = h('div', { class: 'state-error', role: 'alert' }, h('span', { text: message }));
  if (retry) box.append(h('button', { class: 'btn btn-sm', type: 'button', text: 'Tentar novamente', onclick: retry }));
  return box;
}

export function emptyState(title, text, button) {
  return h('div', { class: 'empty' }, h('h3', { text: title }), h('p', { text }), button || null);
}

export function tone(color) { return h('span', { class: `tone tone-${color}` }); }

export function remember(key, value) {
  try { localStorage.setItem(`fm:${key}`, value); } catch (e) { /* storage unavailable */ }
}
export function recall(key) {
  try { return localStorage.getItem(`fm:${key}`); } catch (e) { return null; }
}

export function today(offsetDays = 0) {
  const now = new Date();
  now.setDate(now.getDate() + offsetDays);
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
