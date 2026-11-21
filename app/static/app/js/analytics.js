import { $, $$, api, emptyState, errorState, formatMoney, h, icon } from './core.js';
import {
  balanceArea, cumulativeLine, dataTable, donut, goalBars, groupedBars, heatmap, monthlyBars, netFlowLine, rankedBars,
  sparklines, stackedBars,
} from './charts.js';
import { dateBR, dayLabel, monthLong, monthShort } from './format.js';

const form = document.getElementById('range-form');
const note = document.getElementById('range-note');
const errorBox = document.getElementById('analytics-error');
let data = null;

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const now = () => new Date();
const PRESETS = {
  month: () => [new Date(now().getFullYear(), now().getMonth(), 1), now()],
  'last-month': () => [new Date(now().getFullYear(), now().getMonth() - 1, 1), new Date(now().getFullYear(), now().getMonth(), 0)],
  30: () => [new Date(now().getFullYear(), now().getMonth(), now().getDate() - 29), now()],
  90: () => [new Date(now().getFullYear(), now().getMonth(), now().getDate() - 89), now()],
  year: () => [new Date(now().getFullYear(), 0, 1), now()],
  365: () => [new Date(now().getFullYear(), now().getMonth(), now().getDate() - 364), now()],
};

const empty = (title, text = 'Tente um período maior ou remova um filtro.') => emptyState(title, text);

// Each chart: draw(box, width, data) and the columns/rows for its table view.
const CHARTS = {
  monthly: {
    draw: (box, w, d) => monthlyBars(box, d.monthly, w),
    table: (d) => [[{ label: 'Mês', get: (r) => monthLong(r.month) }, { label: 'Receitas', num: true, get: (r) => formatMoney(r.income) },
      { label: 'Despesas', num: true, get: (r) => formatMoney(r.expense) }], d.monthly],
  },
  netflow: {
    draw: (box, w, d) => netFlowLine(box, d.monthly, w),
    table: (d) => [[{ label: 'Mês', get: (r) => monthLong(r.month) }, { label: 'Saldo', num: true, get: (r) => formatMoney(r.net) }], d.monthly],
  },
  stacked: {
    draw: (box, w, d) => (d.stacked.series.length ? stackedBars(box, d.stacked, w) : box.replaceChildren(empty('Nenhuma despesa ainda'))),
    table: (d) => [[{ label: 'Mês', get: (r) => monthLong(r.month) }, ...d.stacked.series.map((s, i) => ({ label: s.name, num: true, get: (r) => formatMoney(s.values[r.i]) }))],
      d.stacked.months.map((month, i) => ({ month, i }))],
  },
  donut: {
    draw: (box, w, d) => (d.categories.length ? donut(box, d.categories, w) : box.replaceChildren(empty('Nenhuma despesa neste período'))),
    table: (d) => [[{ label: 'Categoria', get: (r) => r.name }, { label: 'Participação', num: true, get: (r) => `${r.share}%` }, { label: 'Total', num: true, get: (r) => formatMoney(r.total) }], d.categories],
  },
  ranked: {
    draw: (box, w, d) => (d.categories.length ? rankedBars(box, d.categories, w, { title: 'Despesas por categoria, em ordem' }) : box.replaceChildren(empty('Nenhuma despesa neste período'))),
    table: (d) => [[{ label: 'Categoria', get: (r) => r.name }, { label: 'Transações', num: true, get: (r) => String(r.count) }, { label: 'Total', num: true, get: (r) => formatMoney(r.total) }], d.categories],
  },
  sparks: {
    draw: (box, w, d) => (d.trend.length ? sparklines(box, d.trend, d.stacked.months, w) : box.replaceChildren(empty('Ainda não há histórico de categorias'))),
    table: (d) => [[{ label: 'Categoria', get: (r) => r.name }, ...d.stacked.months.map((m, i) => ({ label: monthShort(m), num: true, get: (r) => formatMoney(r.values[i]) }))], d.trend],
  },
  heatmap: {
    draw: (box, w, d) => heatmap(box, d.heatmap, w),
    table: (d) => [[{ label: 'Data', get: (r) => dateBR(r.date) }, { label: 'Gasto', num: true, get: (r) => formatMoney(r.expense) }], d.heatmap.days.filter((x) => Number(x.expense) > 0)],
  },
  weekday: {
    draw: (box, w, d) => groupedBars(box, d.weekday.map((r) => ({ label: r.day, long: r.day, a: r.average })), w,
      { title: 'Gasto médio por dia da semana', aName: 'Média', aClass: 'm-budget' }),
    table: (d) => [[{ label: 'Dia da semana', get: (r) => r.day }, { label: 'Média por dia', num: true, get: (r) => formatMoney(r.average) }, { label: 'Total', num: true, get: (r) => formatMoney(r.total) }], d.weekday],
  },
  cumulative: {
    draw: (box, w, d) => cumulativeLine(box, d.cumulative, w),
    table: (d) => [[{ label: 'Data', get: (r) => dateBR(r.date) }, { label: 'Gasto acumulado', num: true, get: (r) => formatMoney(r.spent) }], d.cumulative.days],
  },
  budget: {
    draw: (box, w, d) => (d.budget_vs_actual.length
      ? groupedBars(box, d.budget_vs_actual.map((r) => ({ label: r.name.slice(0, 8), long: r.name, a: r.limit, b: r.spent })), w,
        { title: 'Orçado e realizado', aName: 'Orçado', bName: 'Gasto', aClass: 'm-budget', bClass: 'm-expense' })
      : box.replaceChildren(empty('Nenhum orçamento para este mês', 'Defina orçamentos na página Orçamentos.'))),
    table: (d) => [[{ label: 'Orçamento', get: (r) => r.name }, { label: 'Limite', num: true, get: (r) => formatMoney(r.limit) }, { label: 'Gasto', num: true, get: (r) => formatMoney(r.spent) }], d.budget_vs_actual],
  },
  goals: {
    draw: (box, w, d) => (d.goals.length ? goalBars(box, d.goals, w) : box.replaceChildren(empty('Nenhuma meta ainda', 'Crie uma na página Metas.'))),
    table: (d) => [[{ label: 'Meta', get: (r) => r.name }, { label: 'Guardado', num: true, get: (r) => formatMoney(r.saved) }, { label: 'Objetivo', num: true, get: (r) => formatMoney(r.target) }, { label: 'Progresso', num: true, get: (r) => `${r.percent}%` }], d.goals],
  },
  balance: {
    draw: (box, w, d) => balanceArea(box, d.daily, w),
    table: (d) => [[{ label: 'Data', get: (r) => dateBR(r.date) }, { label: 'Receitas', num: true, get: (r) => formatMoney(r.income) }, { label: 'Despesas', num: true, get: (r) => formatMoney(r.expense) }, { label: 'Saldo', num: true, get: (r) => formatMoney(r.balance) }], d.daily],
  },
  payees: {
    draw: (box, w, d) => (d.payees.length ? rankedBars(box, d.payees.map((p) => ({ ...p, total: p.total, color: 'blue' })), w, { title: 'Principais favorecidos' }) : box.replaceChildren(empty('Nenhum favorecido registrado', 'Informe o favorecido ao registrar uma despesa.'))),
    table: (d) => [[{ label: 'Favorecido', get: (r) => r.name }, { label: 'Transações', num: true, get: (r) => String(r.count) }, { label: 'Total', num: true, get: (r) => formatMoney(r.total) }], d.payees],
  },
};

const cards = {};
$$('[data-chart-card]').forEach((el) => { cards[el.dataset.chartCard] = { el, view: 'chart', box: $('[data-chart]', el) }; });

function draw(name) {
  const card = cards[name];
  const spec = CHARTS[name];
  const width = Math.max(240, Math.floor(card.box.clientWidth || card.el.clientWidth - 32));
  if (card.view === 'table') {
    const [columns, rows] = spec.table(data);
    card.box.replaceChildren(rows.length ? dataTable(columns, rows) : empty('Sem dados'));
  } else {
    spec.draw(card.box, width, data);
  }
}

function renderTotals() {
  const t = data.totals;
  const stat = (label, value, tint) => h('div', { class: 'card stat' }, h('div', { class: 'label', text: label }),
    h('div', { class: `value ${tint || ''}`, text: formatMoney(value) }));
  $('#totals').replaceChildren(stat('Receitas', t.income, 'pos'), stat('Despesas', t.expense, 'neg'),
    stat('Resultado', t.net, Number(t.net) < 0 ? 'neg' : 'pos'), stat('Saldo no início', data.opening_balance));
}

function renderTop() {
  const host = $('#top-list');
  if (!data.top.length) { host.replaceChildren(empty('Nenhuma despesa neste período')); return; }
  host.replaceChildren(h('ol', { class: 'top-list' }, ...data.top.map((t) => h('li', {},
    h('span', { class: 'grow' }, h('strong', { text: t.name }), h('span', { class: 'muted small', text: ` ${t.category.name} · ${dateBR(t.date)}` })),
    h('span', { class: 'neg', text: formatMoney(t.amount) })))));
}

function query() {
  const q = { start: form.elements.start.value, end: form.elements.end.value };
  if (form.elements.category.value) q.category = form.elements.category.value;
  if (form.elements.tag.value) q.tag = form.elements.tag.value;
  return q;
}

async function load() {
  const q = query();
  if (!q.start || !q.end) return;
  history.replaceState(null, '', `${window.location.pathname}?${new URLSearchParams(q)}`);
  try {
    data = await api('/api/analytics/', { query: q });
    errorBox.hidden = true;
    note.textContent = `${data.daily.length} dia(s). Os gráficos mensais mostram os 12 meses até a data final. O saldo ignora os filtros de categoria e tag.`;
    renderTotals();
    renderTop();
    Object.keys(cards).forEach(draw);
  } catch (error) {
    errorBox.replaceChildren(errorState(error.message, load));
    errorBox.hidden = false;
  }
}

$$('[data-toggle-table]').forEach((button) => button.addEventListener('click', () => {
  const name = Object.keys(cards).find((key) => cards[key].el.contains(button));
  const card = cards[name];
  card.view = card.view === 'chart' ? 'table' : 'chart';
  button.textContent = card.view === 'chart' ? 'Mostrar tabela' : 'Mostrar gráfico';
  button.setAttribute('aria-pressed', String(card.view === 'table'));
  if (data) draw(name);
}));

form.addEventListener('change', (event) => {
  if (event.target.id === 'r-preset') {
    const make = PRESETS[event.target.value];
    if (make) {
      const [a, b] = make();
      form.elements.start.value = iso(a);
      form.elements.end.value = iso(b);
    }
  } else if (event.target.name === 'start' || event.target.name === 'end') {
    $('#r-preset').value = '';
  }
  load();
});
form.addEventListener('submit', (event) => { event.preventDefault(); load(); });

const params = new URLSearchParams(window.location.search);
form.elements.start.value = params.get('start') || form.dataset.start;
form.elements.end.value = params.get('end') || form.dataset.end;
if (params.get('category')) form.elements.category.value = params.get('category');
if (params.get('tag')) form.elements.tag.value = params.get('tag');

let lastWidth = window.innerWidth;
window.addEventListener('resize', () => {
  if (Math.abs(window.innerWidth - lastWidth) < 8) return;
  lastWidth = window.innerWidth;
  clearTimeout(window.__fmResize);
  window.__fmResize = setTimeout(() => { if (data) Object.keys(cards).forEach(draw); }, 150);
});
document.addEventListener('tx:changed', load);
load();
