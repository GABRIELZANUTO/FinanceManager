// Phone app: first run (only on this phone, or with an online account) and the Sincronização page.
import { $, $$, FIELD_LABELS, api, clearErrors, confirmDialog, emptyState, errorState, formData, h, openDialog, showError, showErrors, toast,
  withBusy } from './core.js';
import { openTransactionDialog } from './tx-dialog.js';

/** "Lançar manualmente": a new transaction form with this phone's values. */
function openTransactionPreset(local) {
  openTransactionDialog(null, { name: local.name, amount: local.amount, date: local.date, kind: local.kind });
}

const loginForm = document.getElementById('sync-login');
const page = document.getElementById('sync-page');
let creating = false;
let lastChoice = null;

function when(iso) {
  return iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '';
}

/* ---------- login and account creation ---------- */

function openLogin(mode) {
  creating = mode === 'create';
  loginForm.hidden = false;
  $('#sync-login-title').textContent = creating ? 'Criar conta online' : 'Entrar na minha conta online';
  $$('[data-create-only]', loginForm).forEach((node) => { node.hidden = !creating; });
  $('[data-submit]', loginForm).textContent = creating ? 'Criar conta e entrar' : 'Entrar';
  $('#sl-pass').setAttribute('autocomplete', creating ? 'new-password' : 'current-password');
  $('#sync-choice').hidden = true;
  clearErrors(loginForm);
  (creating ? $('#sl-name') : $('#sl-user')).focus();
}

async function submitLogin(choice) {
  clearErrors(loginForm);
  const data = formData(loginForm);
  const body = { ...data, create: creating };
  if (choice) body.choice = choice;
  try {
    const result = await withBusy($('[data-submit]', loginForm), () => api('/api/sync/login/', { method: 'POST', body }));
    if (result.needs_choice) {
      $('#sync-choice').hidden = false;
      lastChoice = body;
      return;
    }
    toast(result.choice === 'send' ? 'Conta conectada. Enviando os dados deste celular...' : 'Conta conectada. Baixando seus dados...');
    window.location.assign('/sync/');
  } catch (error) {
    if (error.errors && error.errors.password1) error.errors.password = error.errors.password1;
    showErrors(loginForm, error);
  }
}

if (loginForm) {
  loginForm.addEventListener('submit', (event) => { event.preventDefault(); submitLogin(null); });
  $('[data-sync-cancel]', loginForm).addEventListener('click', () => { loginForm.hidden = true; });
  $$('[data-sync-choice]', loginForm).forEach((button) => button.addEventListener('click', () => {
    const choice = button.dataset.syncChoice;
    $('#sync-choice').hidden = true;
    if (choice && lastChoice) submitLogin(choice);
  }));
}
$$('[data-sync-open]').forEach((button) => button.addEventListener('click', () => openLogin(button.dataset.syncOpen)));

const localButton = document.querySelector('[data-sync-local]');
if (localButton) {
  localButton.addEventListener('click', async () => {
    try {
      await api('/api/sync/local/', { method: 'POST', body: {} });
      window.location.assign('/');
    } catch (error) { showError(error); }
  });
}

/* ---------- Sincronização page ---------- */

const ACTIONS = {
  mine: 'Usar a deste celular', server: 'Usar a do servidor', manual: 'Lançar manualmente', retry: 'Tentar de novo',
};

function pendingList(items) {
  if (!items.length) return emptyState('Nada para enviar', 'Tudo o que você lançou neste celular já está na conta online.');
  return h('ul', { class: 'mini-list' }, ...items.map((p) => h('li', {},
    h('span', {}, `${p.model}${p.text ? `: ${p.text}` : ''}`,
      h('span', { class: 'sub', text: p.blocked ? 'esperando outro item ser resolvido' : p.action === 'delete' ? 'apagado' : p.created ? 'novo' : 'alterado' })))));
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return '(vazio)';
  if (Array.isArray(value)) return value.length ? `${value.length} item(ns)` : '(vazio)';
  if (value === true) return 'Sim';
  if (value === false) return 'Não';
  return String(value);
}

function conflictCard(c) {
  const rows = c.fields.filter((k) => !/^[0-9a-f-]{36}$/.test(String(c.local[k] || c.server[k] || '')))
    .map((k) => h('tr', {}, h('th', { scope: 'row', text: FIELD_LABELS[k] || k }), h('td', { text: valueText(c.local[k]) }),
      h('td', { text: c.server_deleted ? '(apagado)' : valueText(c.server[k]) })));
  const who = [c.updated_by && `por ${c.updated_by}`, c.updated_at && `em ${when(c.updated_at)}`].filter(Boolean).join(' ');
  const buttons = [];
  if (c.can_mine) buttons.push('mine');
  buttons.push('server');
  if (c.model === 'transaction' && c.local && c.local.amount) buttons.push('manual');
  if (c.can_retry) buttons.push('retry');
  return h('article', { class: 'card conflict' },
    h('h3', { text: `${c.model_label}${c.text ? `: ${c.text}` : ''}` }),
    h('p', { class: 'muted', text: `${c.reason_label}${who ? ` (${who})` : ''}.${c.message ? ` ${c.message}` : ''}` }),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', { text: 'Campo' }), h('th', { text: 'Neste celular' }), h('th', { text: 'No servidor' }))),
      h('tbody', {}, ...rows))) : null,
    h('p', { class: 'actions' }, ...buttons.map((b) => h('button', {
      class: b === 'mine' ? 'btn btn-primary' : 'btn', type: 'button', onclick: () => resolve(c, b),
    }, ACTIONS[b]))));
}

async function resolve(c, resolution) {
  if (resolution === 'server') {
    const ok = await confirmDialog({ title: 'Usar a do servidor', message: 'A versão deste celular será descartada. Continuar?', confirmLabel: 'Usar a do servidor' });
    if (!ok) return;
  }
  try {
    const result = await api(`/api/sync/conflicts/${c.id}/resolve/`, { method: 'POST', body: { resolution } });
    toast('Pronto.');
    if (resolution === 'manual' && result.local) openTransactionPreset(result.local);
    load();
  } catch (error) { showError(error); }
}

function historyList(items) {
  if (!items.length) return emptyState('Nenhuma sincronização ainda', 'Os últimos ciclos aparecem aqui.');
  return h('ul', { class: 'mini-list' }, ...items.map((s) => h('li', {},
    h('span', {}, when(s.start), h('span', { class: 'sub', text: s.error || (s.skipped ? 'pulado' : `enviados ${s.pushed}, recebidos ${s.pulled}, conflitos ${s.conflicts}`) })))));
}

function showStatus(st) {
  $('#sync-off').hidden = st.connected;
  $('#sync-on').hidden = !st.connected;
  $('#sync-lists').hidden = !st.connected;
  if (!st.connected) return;
  $('[data-sync="username"]').textContent = st.username;
  $('[data-sync="server"]').textContent = st.server;
  $('[data-sync="when"]').textContent = st.running ? 'Sincronizando...' : st.last_sync_at
    ? `Última sincronização: ${when(st.last_sync_at)}` : 'Ainda não sincronizou.';
  const error = $('[data-sync="error"]');
  error.hidden = !st.last_error;
  error.textContent = st.last_error || '';
}

async function load() {
  try {
    const data = await api('/api/sync/details/');
    showStatus(data.status);
    $('#tab-pending').replaceChildren(pendingList(data.pending));
    $('#tab-conflicts').replaceChildren(data.conflicts.length ? h('div', {}, ...data.conflicts.map(conflictCard))
      : emptyState('Nenhum conflito', 'Quando algo não puder ser juntado sozinho, aparece aqui para você decidir.'));
    $('#tab-history').replaceChildren(historyList(data.history));
    $('[data-count="pending"]').textContent = data.pending.length || '';
    $('[data-count="conflicts"]').textContent = data.conflicts.length || '';
  } catch (error) {
    $('#tab-pending').replaceChildren(errorState(error.message, load));
  }
}

function tabs() {
  const buttons = $$('[role="tab"]', page);
  buttons.forEach((button) => button.addEventListener('click', () => {
    buttons.forEach((b) => {
      const on = b === button;
      b.setAttribute('aria-selected', String(on));
      document.getElementById(b.getAttribute('aria-controls')).hidden = !on;
    });
  }));
  if (window.location.hash === '#conflitos') document.getElementById('t-conflicts').click();
}

if (page) {
  tabs();
  load();
  setInterval(() => { if (document.visibilityState === 'visible') load(); }, 15000);
  $('[data-sync-now]').addEventListener('click', async (event) => {
    try {
      await withBusy(event.currentTarget, () => api('/api/sync/run/', { method: 'POST', body: {} }));
      toast('Sincronizando...');
      setTimeout(load, 2500);
    } catch (error) { showError(error); }
  });
  $('[data-sync-logout]').addEventListener('click', async () => {
    const st = (await api('/api/sync/status/'));
    const dialog = document.getElementById('logout-dialog');
    $('#logout-message').textContent = st.pending
      ? `Ainda há ${st.pending} alteração(ões) não enviada(s). Sincronize antes de sair, ou elas ficam só neste celular. O que fazer com os dados deste celular?`
      : 'O que fazer com os dados que estão neste celular?';
    dialog.returnValue = '';
    dialog.addEventListener('close', async () => {
      if (!dialog.returnValue) return;
      try {
        await api('/api/sync/logout/', { method: 'POST', body: { keep: dialog.returnValue === 'keep' } });
        toast('Você saiu da conta online.');
        load();
      } catch (error) { showError(error); }
    }, { once: true });
    openDialog(dialog);
  });
}
