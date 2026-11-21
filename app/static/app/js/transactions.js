import {
  $, $$, api, clearErrors, confirmDialog, debounce, errorState, formatMoney, h, openDialog, parseMoney, showError, showErrors, toast,
  withBusy, wireDialog, wireMoneyInputs,
} from './core.js';
import { duplicateTransaction, editTransaction } from './tx-dialog.js';

const filters = document.getElementById('filters');
const results = document.getElementById('results');
const status = document.getElementById('filter-status');
const exportLink = document.getElementById('export-link');
const DEFAULTS = { sort: '-date', page_size: '25' };
let controller = null;
let page = new URLSearchParams(window.location.search).get('page');

function currentQuery() {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(filters).entries()) {
    if (value !== '' && DEFAULTS[key] !== value) params.set(key, value);
  }
  return params;
}

function sync(params) {
  const text = params.toString();
  history.replaceState(null, '', window.location.pathname + (text ? `?${text}` : ''));
  exportLink.href = exportLink.href.split('?')[0] + (text ? `?${text}` : '');
}

async function load(params, { push = true } = {}) {
  page = params.get('page');
  if (controller) controller.abort();
  controller = new AbortController();
  results.setAttribute('aria-busy', 'true');
  status.textContent = '';
  const query = new URLSearchParams(params);
  query.set('fragment', '1');
  try {
    const response = await fetch(`${window.location.pathname}?${query}`, {
      credentials: 'same-origin', signal: controller.signal, headers: { 'X-Requested-With': 'fetch' },
    });
    if (response.redirected && response.url.includes('/login/')) { window.location.assign(response.url); return; }
    if (!response.ok) throw new Error('bad status');
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    const inner = doc.getElementById('results-inner');
    if (!inner) throw new Error('bad fragment');
    results.replaceChildren(inner);
    if (push) sync(params);
    resetBulk();
  } catch (error) {
    if (error.name === 'AbortError') return;
    results.replaceChildren(errorState('Não foi possível carregar as transações. Verifique sua conexão.', () => load(params, { push })));
  } finally {
    results.removeAttribute('aria-busy');
  }
}

function reload() {
  const params = currentQuery();
  if (page) params.set('page', page);
  return load(params, { push: false });
}

const reloadSoon = debounce(() => load(currentQuery()), 300);
filters.addEventListener('submit', (event) => { event.preventDefault(); reloadSoon.cancel(); load(currentQuery()); });
filters.addEventListener('input', (event) => {
  if (event.target.matches('input[type="search"], input[type="number"]')) reloadSoon();
});
filters.addEventListener('change', (event) => {
  if (!event.target.matches('input[type="search"], input[type="number"]')) { reloadSoon.cancel(); load(currentQuery()); }
});
$('#clear-filters').addEventListener('click', (event) => {
  event.preventDefault();
  filters.reset();
  for (const el of $$('input, select', filters)) {
    if (el.type === 'search' || el.type === 'number' || el.type === 'date') el.value = '';
    else if (el.name === 'sort') el.value = '-date';
    else if (el.name === 'page_size') el.value = '25';
    else el.value = '';
  }
  load(currentQuery());
});

// Sorting, paging and "clear" links inside the results are fetched in place.
results.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-page-link], a[data-clear-filters]');
  if (link) {
    event.preventDefault();
    if (link.hasAttribute('data-clear-filters')) { $('#clear-filters').click(); return; }
    const params = new URLSearchParams(link.getAttribute('href').replace(/^\?/, ''));
    // reflect sort/page state back into the form controls
    for (const select of ['sort', 'page_size']) {
      if (filters.elements[select]) filters.elements[select].value = params.get(select) || DEFAULTS[select];
    }
    load(params);
    results.scrollIntoView({ block: 'start', behavior: 'smooth' });
    return;
  }
  const edit = event.target.closest('[data-edit]');
  if (edit) { editTransaction(edit.dataset.edit); return; }
  const dup = event.target.closest('[data-duplicate]');
  if (dup) { duplicateTransaction(dup.dataset.duplicate); return; }
  const rec = event.target.closest('[data-bulk-reconcile]');
  if (rec) { bulkReconcile(rec.dataset.bulkReconcile === '1'); return; }
  const del = event.target.closest('[data-delete]');
  if (del) { deleteOne(del.dataset.delete, del.dataset.name, del.dataset.version); return; }
  if (event.target.closest('[data-bulk-date]')) { openBulkDate(); return; }
  if (event.target.closest('[data-bulk-delete]')) bulkDelete();
  else if (event.target.closest('[data-bulk-apply]')) bulkCategory();
});


async function deleteOne(id, name, version) {
  const ok = await confirmDialog({ title: 'Excluir transação', message: `Excluir “${name}”? Você pode desfazer por alguns segundos.` });
  if (!ok) return;
  try {
    await api(`/api/transactions/${id}/`, { method: 'DELETE', version: version ? Number(version) : undefined });
    toast('Transação excluída.', { action: { label: 'Desfazer', onClick: () => restore([id]) } });
    await reload();
  } catch (error) { showError(error); }
}

async function restore(ids) {
  try {
    await api('/api/transactions/bulk/', { method: 'POST', body: { action: 'restore', ids } });
    toast(ids.length === 1 ? 'Transação restaurada.' : `${ids.length} transações restauradas.`);
    await reload();
  } catch (error) { showError(error); }
}

/* ---------- bulk ---------- */

function selected() { return $$('.row-check:checked', results).map((box) => Number(box.value)); }

function resetBulk() { updateBulk(); }

function updateBulk() {
  const bar = $('[data-bulk]', results);
  if (!bar) return;
  const ids = selected();
  bar.hidden = ids.length === 0;
  $('[data-bulk-count]', bar).textContent = String(ids.length);
  $$('.row-check', results).forEach((box) => box.closest('tr').classList.toggle('selected', box.checked));
  const all = $('[data-select-all]', results);
  const boxes = $$('.row-check', results);
  all.checked = boxes.length > 0 && ids.length === boxes.length;
  all.indeterminate = ids.length > 0 && ids.length < boxes.length;
}

results.addEventListener('change', (event) => {
  if (event.target.matches('[data-select-all]')) {
    $$('.row-check', results).forEach((box) => { box.checked = event.target.checked; });
  }
  if (event.target.matches('.row-check, [data-select-all]')) updateBulk();
});

async function bulkDelete() {
  const ids = selected();
  if (!ids.length) return;
  const ok = await confirmDialog({ title: 'Excluir transações', message: `Excluir ${ids.length} transação(ões) selecionada(s)? Você pode desfazer por alguns segundos.` });
  if (!ok) return;
  try {
    await api('/api/transactions/bulk/', { method: 'POST', body: { action: 'delete', ids } });
    toast(`${ids.length} transação(ões) excluída(s).`, { action: { label: 'Desfazer', onClick: () => restore(ids) } });
    await reload();
  } catch (error) { showError(error); }
}

async function bulkCategory() {
  const ids = selected();
  const category = $('#bulk-category', results).value;
  if (!ids.length) return;
  if (!category) { toast('Escolha uma categoria primeiro.', { kind: 'warn' }); return; }
  try {
    await api('/api/transactions/bulk/', { method: 'POST', body: { action: 'category', ids, category } });
    toast(`Categoria alterada em ${ids.length} transação(ões).`);
    await reload();
  } catch (error) { showError(error); }
}

/* ---------- bulk date change ---------- */

const dateDialog = document.getElementById('date-dialog');
const dateForm = document.getElementById('date-form');
let dateIds = [];

function openBulkDate() {
  dateIds = selected();
  if (!dateIds.length) return;
  clearErrors(dateForm);
  dateForm.reset();
  $('[data-date-count]', dateForm).textContent = String(dateIds.length);
  openDialog(dateDialog, '#bulk-date');
}

dateForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(dateForm);
  const byDate = dateForm.elements.mode.value === 'date';
  const body = { action: 'date', ids: dateIds };
  if (byDate) {
    if (!dateForm.elements.date.value) { showErrors(dateForm, { message: 'Escolha a nova data.', errors: { date: ['Escolha a nova data.'] } }); return; }
    body.date = dateForm.elements.date.value;
  } else {
    body.shift_months = Number(dateForm.elements.shift_months.value);
  }
  try {
    const data = await withBusy($('[data-submit]', dateForm), () => api('/api/transactions/bulk/', { method: 'POST', body }));
    dateDialog.close();
    toast(`Data alterada em ${data.moved} transação(ões).`);
    await reload();
  } catch (error) { showErrors(dateForm, error); }
});
wireDialog(dateDialog);

/* ---------- phones: the filters beyond "Buscar" open on demand ---------- */
const moreToggle = filters.querySelector('[data-filters-toggle]');
function showMoreFilters(open) {
  filters.classList.toggle('show-more', open);
  moreToggle.setAttribute('aria-expanded', String(open));
  moreToggle.querySelector('[data-filters-toggle-label]').textContent = open ? 'Menos filtros' : 'Mais filtros';
}
if (moreToggle) {
  moreToggle.addEventListener('click', () => showMoreFilters(!filters.classList.contains('show-more')));
  const active = [...new URLSearchParams(window.location.search).keys()]
    .some((key) => !['q', 'sort', 'page', 'page_size'].includes(key));
  showMoreFilters(active);
}

document.addEventListener('tx:changed', reload);
updateBulk();

/* ---------- inline category change ---------- */

results.addEventListener('click', (event) => {
  const button = event.target.closest('[data-inline-category]');
  if (!button) return;
  const select = h('select', { class: 'inline-select', 'aria-label': 'Categoria' });
  const kind = button.dataset.kind;
  for (const option of $$('#tx-category option')) {
    if (!option.value) continue;
    if (option.dataset.kind !== 'both' && option.dataset.kind !== kind) continue;
    select.append(h('option', { value: option.value, text: option.textContent }));
  }
  select.value = button.dataset.current;
  button.replaceWith(select);
  select.focus();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (save && select.value !== button.dataset.current) {
      try {
        await api('/api/transactions/bulk/', { method: 'POST', body: { action: 'category', ids: [Number(button.dataset.inlineCategory)], category: select.value } });
        toast('Categoria atualizada.');
      } catch (error) { showError(error); }
    }
    reload();
  };
  select.addEventListener('change', () => finish(true));
  select.addEventListener('blur', () => finish(false));
  select.addEventListener('keydown', (e) => { if (e.key === 'Escape') finish(false); });
});

/* ---------- saved views ---------- */

const viewSelect = $('#views-select');
const viewDelete = $('#view-delete');
const viewDialog = $('#view-dialog');
const viewForm = $('#view-form');
let views = [];

async function loadViews() {
  try {
    views = (await api('/api/views/')).views;
    viewSelect.replaceChildren(h('option', { value: '', text: views.length ? 'Filtros salvos' : 'Nenhum filtro salvo ainda' }),
      ...views.map((v) => h('option', { value: v.id, text: v.name })));
    viewDelete.hidden = true;
  } catch (error) { /* views are optional */ }
}

function applyQuery(query) {
  const params = new URLSearchParams(query);
  for (const el of $$('input, select', filters)) {
    if (!el.name) continue;
    if (el.type === 'checkbox') el.checked = params.get(el.name) === el.value;
    else el.value = params.get(el.name) || DEFAULTS[el.name] || '';
  }
  load(currentQuery());
}

viewSelect.addEventListener('change', () => {
  const view = views.find((v) => String(v.id) === viewSelect.value);
  viewDelete.hidden = !view;
  if (view) applyQuery(view.query);
});

viewDelete.addEventListener('click', async () => {
  const view = views.find((v) => String(v.id) === viewSelect.value);
  if (!view) return;
  try {
    await api(`/api/views/${view.id}/`, { method: 'DELETE', version: view.version });
    toast('Filtro salvo excluído.');
    loadViews();
  } catch (error) { showError(error); }
});

$('#view-save').addEventListener('click', () => {
  clearErrors(viewForm);
  viewForm.reset();
  openDialog(viewDialog, '#view-name');
});
wireDialog(viewDialog);
viewForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(viewForm);
  try {
    await withBusy($('[data-submit]', viewForm), () => api('/api/views/', {
      method: 'POST', body: { name: viewForm.elements.name.value, query: currentQuery().toString() } }));
    viewDialog.close();
    toast('Filtro salvo.');
    loadViews();
  } catch (error) { showErrors(viewForm, error); }
});
loadViews();

/* ---------- reconcile ---------- */

async function bulkReconcile(value) {
  const ids = selected();
  if (!ids.length) return;
  try {
    await api('/api/transactions/bulk/', { method: 'POST', body: { action: value ? 'reconcile' : 'unreconcile', ids } });
    toast(value ? 'Marcado como conciliado.' : 'Conciliação removida.');
    await reload();
    checkReconcile();
  } catch (error) { showError(error); }
}

async function checkReconcile() {
  const account = $('#rec-acct');
  if (!account) return;
  const out = $('#rec-result');
  try {
    const data = await api('/api/reconcile/', { query: { account: account.value } });
    const statement = parseMoney($('#rec-balance').value);
    if (statement === '') { out.textContent = `Saldo conciliado até agora: ${formatMoney(data.cleared)}. ${data.unreconciled} item(ns) ainda não conciliado(s).`; return; }
    const diff = Math.round((Number(statement) - Number(data.cleared)) * 100) / 100;
    out.textContent = diff === 0 ? 'Confere com o seu extrato.' : `Diferença em relação ao extrato: ${formatMoney(diff)}. ${data.unreconciled} item(ns) ainda não conciliado(s).`;
    out.className = diff === 0 ? 'pos' : 'neg';
  } catch (error) { out.textContent = error.message; }
}
$('#rec-check').addEventListener('click', checkReconcile);
wireMoneyInputs($('#reconcile'));
