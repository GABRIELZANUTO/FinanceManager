import { $, api, clearErrors, confirmDialog, emptyState, errorState, formData, formatMoney, h, icon, openDialog, recall, remember, setMoney, showError, showErrors, toast, withBusy, wireDialog, wireMoneyInputs } from './core.js';
import { catLabel } from './parts.js';
import { dateBR } from './format.js';

const list = document.getElementById('recurring-list');
const dialog = document.getElementById('rec-dialog');
const form = document.getElementById('rec-form');
let items = [];
let editing = null;

/* ---------- sorting: click a column header (click again to reverse) ---------- */
const collator = new Intl.Collator('pt-BR', { sensitivity: 'base', numeric: true });
const SORTS = {
  name: (a, b) => collator.compare(a.name, b.name),
  category: (a, b) => collator.compare(a.category.name, b.category.name) || collator.compare(a.name, b.name),
  // paused items have no next date: they always go last
  next_due: (a, b) => (b.active - a.active) || a.next_due.localeCompare(b.next_due) || collator.compare(a.name, b.name),
  amount: (a, b) => Number(a.amount) - Number(b.amount) || collator.compare(a.name, b.name),
};
let sort = { key: 'next_due', dir: 1 };
try {
  const saved = JSON.parse(recall('recurring-sort') || 'null');
  if (saved && SORTS[saved.key] && (saved.dir === 1 || saved.dir === -1)) sort = saved;
} catch (e) { /* keep the default */ }

function sorted() {
  const compare = SORTS[sort.key];
  if (sort.key === 'next_due') {   // reversing still keeps paused items at the end
    return [...items].sort((a, b) => (b.active - a.active) || sort.dir * compare(a, b));
  }
  return [...items].sort((a, b) => sort.dir * compare(a, b));
}

function sortHeader(label, key, numeric = false) {
  const on = sort.key === key;
  return h('th', { class: numeric ? 'num' : '', 'aria-sort': on ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none' },
    h('button', { class: 'th-sort', type: 'button', onclick: () => setSort(key) }, label,
      h('span', { class: 'sort-ind', 'aria-hidden': 'true', text: on ? (sort.dir === 1 ? '\u25b2' : '\u25bc') : '' })));
}

function setSort(key) {
  sort = { key, dir: sort.key === key ? -sort.dir : 1 };
  remember('recurring-sort', JSON.stringify(sort));
  render();
}

function syncCategories() {
  const select = form.elements.category;
  const wanted = form.elements.kind.value;
  for (const option of select.options) {
    if (!option.value) continue;
    const ok = option.dataset.kind === 'both' || option.dataset.kind === wanted;
    option.hidden = !ok;
    option.disabled = !ok;
  }
  if (select.selectedOptions[0] && select.selectedOptions[0].disabled) select.value = '';
}
form.querySelectorAll('input[name="kind"]').forEach((radio) => radio.addEventListener('change', syncCategories));

function row(item) {
  const sign = item.kind === 'income' ? '+' : '-';
  return h('tr', { class: item.active ? '' : 'is-dup' },
    h('td', { class: 'name-cell', 'data-label': 'Descrição' }, item.name,
      item.installment_total ? h('span', { class: 'small muted', text: ` Parcelas ${item.installment_first} a ${item.installment_total}, até ${item.end_date ? dateBR(item.end_date) : '-'}` }) : null,
      item.last_error ? h('span', { class: 'row-error', text: `A última execução falhou: ${item.last_error}` }) : null),
    h('td', { 'data-label': 'Categoria' }, catLabel(item.category, 'span')),
    h('td', { 'data-label': 'Repete', text: { weekly: 'Toda semana', once: 'Uma vez' }[item.frequency] || 'Todo mês' }),
    h('td', { 'data-label': 'Próxima data', text: item.active ? dateBR(item.next_due) : 'Pausada' }),
    h('td', { class: `num ${item.kind === 'income' ? 'pos' : 'neg'}`, 'data-label': 'Valor', text: sign + formatMoney(item.amount) }),
    h('td', { class: 'row-actions' },
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': `${item.active ? 'Pausar' : 'Retomar'} ${item.name}`, title: item.active ? 'Pausar' : 'Retomar', onclick: () => toggle(item) }, icon(item.active ? 'pause' : 'play')),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Editar ${item.name}`, onclick: () => openEdit(item) }, icon('pencil')),
      h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Excluir ${item.name}`, onclick: () => remove(item) }, icon('trash-2'))));
}

function render() {
  if (!items.length) {
    list.className = '';
    list.replaceChildren(emptyState('Nada agendado', 'Cadastre aluguel, salário ou uma assinatura uma vez e o lançamento é feito por você.'));
    return;
  }
  list.className = 'table-wrap';
  list.replaceChildren(h('table', { class: 'table stack' },
    h('caption', { class: 'sr-only', text: 'Transações recorrentes' }),
    h('thead', {}, h('tr', {}, sortHeader('Descrição', 'name'), sortHeader('Categoria', 'category'), h('th', { text: 'Repete' }),
      sortHeader('Próxima data', 'next_due'), sortHeader('Valor', 'amount', true), h('th', {}, h('span', { class: 'sr-only', text: 'Ações' })))),
    h('tbody', {}, ...sorted().map(row))));
}

async function load() {
  try {
    items = (await api('/api/recurring/')).items;
    render();
  } catch (error) {
    list.className = '';
    list.replaceChildren(errorState(error.message, load));
  }
}

function openEdit(item) {
  editing = item;
  clearErrors(form);
  form.reset();
  $('#rec-title').textContent = item ? 'Editar recorrência' : 'Nova recorrência';
  if (item) {
    for (const key of ['name', 'frequency', 'note', 'payee']) form.elements[key].value = item[key] || '';
    setMoney(form.elements.amount, item.amount);
    form.elements.account.value = item.account ? String(item.account) : '';
    form.elements.kind.value = item.kind;
    form.elements.start_date.value = item.start_date;
    form.elements.end_date.value = item.end_date || '';
    form.elements.parcelada.checked = Boolean(item.installment_total);
    form.elements.installment_total.value = item.installment_total || '';
    form.elements.installment_first.value = item.installment_first || 1;
    if (item.installment_total) form.elements.name.value = item.name.replace(/\s*\(parcelado em \d+x\)\s*$/i, '');
    syncCategories();
    form.elements.category.value = String(item.category.id);
  } else {
    form.elements.start_date.value = dialog.dataset.today;
    if (document.body.dataset.defaultAccount) form.elements.account.value = document.body.dataset.defaultAccount;
    syncCategories();
  }
  syncInstallment();
  openDialog(dialog, '#rec-name');
}

// "Compra parcelada": number of installments instead of frequency and last date.
function syncInstallment() {
  const on = form.elements.parcelada.checked;
  form.querySelectorAll('[data-installment]').forEach((node) => { node.hidden = !on; });
  form.querySelectorAll('[data-not-installment]').forEach((node) => { node.hidden = on; });
  if (on) form.elements.kind.value = 'expense';
}
form.elements.parcelada.addEventListener('change', syncInstallment);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(form);
  const body = formData(form);
  delete body.parcelada;
  if (!form.elements.parcelada.checked) { body.installment_total = ''; body.installment_first = ''; }
  try {
    await withBusy($('[data-submit]', form), () => api(
      editing ? `/api/recurring/${editing.id}/` : '/api/recurring/', { method: 'POST', body, version: editing && editing.version }));
    dialog.close();
    toast('Recorrência salva.');
    await load();
  } catch (error) { showErrors(form, error); }
});

async function toggle(item) {
  try {
    await api(`/api/recurring/${item.id}/toggle/`, { method: 'POST', version: item.version });
    toast(item.active ? 'Pausada.' : 'Retomada. As datas perdidas durante a pausa foram puladas.');
    await load();
  } catch (error) { showError(error); }
}

async function remove(item) {
  const ok = await confirmDialog({ title: 'Excluir recorrência', message: `Parar e excluir “${item.name}”? As transações já criadas por ela são mantidas.` });
  if (!ok) return;
  try {
    await api(`/api/recurring/${item.id}/`, { method: 'DELETE', version: item.version });
    toast('Recorrência excluída.');
    await load();
  } catch (error) { showError(error); }
}

wireDialog(dialog);
wireMoneyInputs(dialog);
$('#new-recurring').addEventListener('click', () => openEdit(null));
load();
