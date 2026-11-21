import {
  $, $$, api, clearErrors, emptyState, errorState, formData, formatMoney, h, icon, openDialog, setMoney, showError, showErrors,
  toast, withBusy, wireDialog, wireMoneyInputs,
} from './core.js';
import { dataTable, groupedBars } from './charts.js';
import { monthLong, monthShort } from './format.js';
import { budgetRow } from './parts.js';

const switcher = document.querySelector('[data-month]');
const label = document.getElementById('month-label');
const list = document.getElementById('budget-list');
const stats = document.getElementById('budget-stats');
const dialog = document.getElementById('budget-dialog');
const form = document.getElementById('budget-form');
let month = switcher.dataset.month;
let data = null;
let history = null;
const views = { bva: 'chart', history: 'chart' };

function shift(ym, delta) {
  const date = new Date(Number(ym.slice(0, 4)), Number(ym.slice(5, 7)) - 1 + delta, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

function stat(title, value, note, tint) {
  return h('div', { class: 'card stat' }, h('div', { class: 'label', text: title }),
    h('div', { class: `value ${tint || ''}`, text: value }), h('div', { class: 'note', text: note || '' }));
}

function renderStats() {
  const o = data.overall;
  const t = data.totals;
  const cards = [];
  if (o) {
    cards.push(stat('Mês inteiro', `${formatMoney(o.spent)} de ${formatMoney(o.available)}`, `Restam ${formatMoney(o.remaining)}`, o.over ? 'neg' : ''));
    cards.push(stat('Pode gastar por dia', formatMoney(o.safe_per_day), 'Até o fim deste mês'));
    // Spent so far plus what is still to come this month (future-dated entries and recurring items).
    cards.push(stat('Previsto até o fim do mês', formatMoney(o.with_planned),
      o.planned_over ? `Com o que ainda vem (${formatMoney(o.planned)}) passa do limite` : `Inclui ${formatMoney(o.planned)} que ainda vem; fica dentro do limite`,
      o.planned_over ? 'neg' : 'pos'));
  } else {
    cards.push(stat('Todos os gastos', formatMoney(t.all_spent), 'Nenhum orçamento geral definido'));
  }
  cards.push(stat('Categorias', `${data.budgets.length}`, t.over ? `${t.over} acima do orçamento` : t.warn ? `${t.warn} perto do limite` : 'Todas dentro do limite', t.over ? 'neg' : ''));
  stats.replaceChildren(...cards.slice(0, 4));
}

function actions(b) {
  return h('span', { class: 'row-actions' },
    h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Editar orçamento ${b.category ? `de ${b.category.name}` : 'geral'}`, onclick: () => openEdit(b) }, icon('pencil')),
    h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Remover orçamento ${b.category ? `de ${b.category.name}` : 'geral'}`, onclick: () => remove(b) }, icon('trash-2')));
}

function renderList() {
  const items = (data.overall ? [data.overall] : []).concat(data.budgets);
  list.className = '';
  if (!items.length) {
    list.replaceChildren(emptyState('Nenhum orçamento para este mês', 'Defina um limite para o mês inteiro ou para uma categoria.',
      h('button', { class: 'btn btn-primary', type: 'button', text: 'Definir orçamento', onclick: () => openEdit(null) })));
    return;
  }
  list.replaceChildren(...items.map((b) => {
    const row = budgetRow(b, { detail: true });
    row.querySelector('.budget-top').append(actions(b));
    return row;
  }));
}

function renderWeekly() {
  const host = document.getElementById('weekly');
  host.className = '';
  const items = (data.overall ? [data.overall] : []).concat(data.budgets);
  if (!items.length) { host.replaceChildren(emptyState('Nada para detalhar', 'Os orçamentos aparecem aqui semana a semana.')); return; }
  const head = h('tr', {}, h('th', { text: 'Orçamento' }), ...data.weeks.map((w) => h('th', { class: 'num', text: `Dias ${w.start} a ${w.end}` })));
  const body = items.map((b) => h('tr', {}, h('td', { text: b.category ? b.category.name : 'Mês inteiro' }),
    ...b.weekly.map((spent, i) => {
      const over = Number(spent) > Number(b.weekly_limit[i]);
      return h('td', { class: `num ${over ? 'neg' : ''}` }, formatMoney(spent), h('span', { class: 'muted small block', text: `de ${formatMoney(b.weekly_limit[i])}` }));
    })));
  host.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'table' }, h('thead', {}, head), h('tbody', {}, ...body))));
}

function renderBva() {
  const box = document.getElementById('bva-chart');
  const items = (data.overall ? [data.overall] : []).concat(data.budgets);
  if (!items.length) { box.replaceChildren(emptyState('Nenhum orçamento', '')); return; }
  const rows = items.map((b) => ({ name: b.category ? b.category.name : 'Mês inteiro', limit: b.available, spent: b.spent }));
  if (views.bva === 'table') {
    box.replaceChildren(dataTable([{ label: 'Orçamento', get: (r) => r.name }, { label: 'Limite', num: true, get: (r) => formatMoney(r.limit) },
      { label: 'Gasto', num: true, get: (r) => formatMoney(r.spent) }], rows));
    return;
  }
  groupedBars(box, rows.map((r) => ({ label: r.name.slice(0, 8), long: r.name, a: r.limit, b: r.spent })),
    Math.max(240, box.clientWidth), { title: 'Orçado e realizado', aName: 'Orçado', bName: 'Gasto', aClass: 'm-budget', bClass: 'm-expense' });
}

function renderHistory() {
  const box = document.getElementById('history-chart');
  if (!history) return;
  const rows = history;
  if (views.history === 'table') {
    box.replaceChildren(dataTable([{ label: 'Mês', get: (r) => monthLong(r.month) },
      { label: 'Orçamento', num: true, get: (r) => (r.limit ? formatMoney(r.limit) : '-') },
      { label: 'Gasto', num: true, get: (r) => formatMoney(r.spent) },
      { label: 'Usado', num: true, get: (r) => (r.percent === null ? '-' : `${r.percent}%`) }], rows));
    return;
  }
  groupedBars(box, rows.map((r) => ({ label: monthShort(r.month), long: monthLong(r.month), a: r.limit || 0, b: r.spent })),
    Math.max(240, box.clientWidth), { title: 'Histórico do orçamento', aName: 'Orçado', bName: 'Gasto', aClass: 'm-budget', bClass: 'm-expense' });
}

async function load() {
  label.textContent = monthLong(month);
  history_(); // keep the address in sync
  const error = document.getElementById('budget-error');
  try {
    data = await api('/api/budgets/', { query: { month } });
    error.hidden = true;
    const none = !data.overall && !data.budgets.length;
    document.getElementById('copy-prompt').hidden = !(none && data.has_previous);
    renderStats();
    renderList();
    renderWeekly();
    renderBva();
  } catch (e) {
    list.className = '';
    error.replaceChildren(errorState(e.message, load));
    error.hidden = false;
  }
  try {
    history = (await api('/api/budgets/history/')).months;
    renderHistory();
  } catch (e) { /* the history chart is optional */ }
}

function history_() {
  window.history.replaceState(null, '', `${window.location.pathname}?month=${month}`);
}

async function openEdit(budget) {
  clearErrors(form);
  form.reset();
  const select = form.elements.category;
  let categories;
  try { categories = (await api('/api/categories/')).categories; } catch (e) { showError(e); return; }
  const used = new Set(data.budgets.map((b) => (b.category ? b.category.id : null)));
  const options = categories.filter((c) => c.kind !== 'income' && (budget ? budget.category && c.id === budget.category.id : !used.has(c.id)));
  const overallTaken = Boolean(data.overall);
  const choices = [];
  if (!overallTaken || (budget && !budget.category)) choices.push(h('option', { value: '', text: 'Mês inteiro (todos os gastos)' }));
  select.replaceChildren(...choices, ...options.map((c) => h('option', { value: c.id, text: c.full_name })));
  $('#budget-month-name').textContent = monthLong(month);
  $('#budget-title').textContent = budget ? 'Editar orçamento' : 'Definir orçamento';
  if (budget) {
    select.value = budget.category ? String(budget.category.id) : '';
    setMoney(form.elements.amount, budget.limit);
    form.elements.warn_percent.value = budget.warn_percent;
    form.elements.rollover.checked = budget.rollover;
    select.disabled = true;
  } else {
    select.disabled = false;
    form.elements.warn_percent.value = 80;
  }
  openDialog(dialog, budget ? '#budget-amount' : '#budget-category');
}

async function remove(budget) {
  try {
    await api(`/api/budgets/${budget.id}/`, { method: 'DELETE', version: budget.version });
    toast('Orçamento removido.');
    await load();
  } catch (e) { showError(e); }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(form);
  const body = formData(form);
  body.category = form.elements.category.value;
  body.month = month;
  try {
    await withBusy($('[data-submit]', form), () => api('/api/budgets/', { method: 'POST', body }));
    dialog.close();
    toast('Orçamento salvo.');
    await load();
  } catch (e) { showErrors(form, e); }
});

$('#copy-last').addEventListener('click', async () => {
  try {
    const result = await api('/api/budgets/copy/', { method: 'POST', body: { from: shift(month, -1), to: month } });
    toast(`${result.copied} orçamento(s) copiado(s) do mês passado.`);
    await load();
  } catch (e) { showError(e); }
});

$$('[data-toggle-table]').forEach((button) => button.addEventListener('click', () => {
  const key = button.closest('.chart-card').querySelector('#bva-chart') ? 'bva' : 'history';
  views[key] = views[key] === 'chart' ? 'table' : 'chart';
  button.textContent = views[key] === 'chart' ? 'Mostrar tabela' : 'Mostrar gráfico';
  button.setAttribute('aria-pressed', String(views[key] === 'table'));
  if (key === 'bva') renderBva(); else renderHistory();
}));

$('#prev-month').addEventListener('click', () => { month = shift(month, -1); load(); });
$('#next-month').addEventListener('click', () => { month = shift(month, 1); load(); });
$('#new-budget').addEventListener('click', () => openEdit(null));
document.addEventListener('tx:changed', load);
wireDialog(dialog);
wireMoneyInputs(dialog);
load();
