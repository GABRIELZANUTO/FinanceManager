// The add/edit transaction dialog (markup lives in _dialogs.html).
import {
  $, $$, api, clearErrors, debounce, formData, openDialog, recall, remember, setMoney, showErrors, toast, today, withBusy,
  wireDialog, wireMoneyInputs, h, parseMoney, formatMoney, icon,
} from './core.js';

let dialog;
let form;
let editingId = null;
let editingVersion = null;

function init() {
  if (dialog) return true;
  dialog = document.getElementById('tx-dialog');
  if (!dialog) return false;
  form = $('#tx-form', dialog);
  wireDialog(dialog);
  form.elements.account.addEventListener('change', syncCardDate);
  wireMoneyInputs(dialog);
  $$('input[name="kind"]', form).forEach((radio) => radio.addEventListener('change', () => { syncCategories(); hint(); }));
  form.addEventListener('submit', submit);
  $$('[data-date-preset]', form).forEach((button) => button.addEventListener('click', () => {
    const d = new Date();
    d.setDate(d.getDate() + Number(button.dataset.datePreset));
    form.elements.date.value = localIso(d);
  }));
  attachSuggest(form.elements.payee, 'payee', $('#payee-options', dialog));
  attachSuggest(form.elements.tags, 'tag', $('#tag-options', dialog), true);
  form.elements.name.addEventListener('input', debounce(hint, 300));
  form.elements.payee.addEventListener('input', debounce(hint, 300));
  form.elements.category.addEventListener('change', () => { inferKind(); hint(); });
  $('#split-add', dialog).addEventListener('click', () => addSplitRow());
  $('#tx-history-btn', dialog).addEventListener('click', showHistory);
  form.elements.amount.addEventListener('input', updateLeft);
  return true;
}

function localIso(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function attachSuggest(input, type, list, lastToken = false) {
  const load = debounce(async () => {
    const text = lastToken ? input.value.split(',').pop().trim() : input.value.trim();
    try {
      const data = await api('/api/suggest/', { query: { type, q: text } });
      const prefix = lastToken ? input.value.split(',').slice(0, -1).map((t) => t.trim()).filter(Boolean).join(', ') : '';
      list.replaceChildren(...data.items.map((item) => h('option', { value: prefix ? `${prefix}, ${item}` : item })));
    } catch (e) { /* suggestions are optional */ }
  }, 200);
  input.addEventListener('input', load);
  input.addEventListener('focus', load);
}

function kind() { return form.elements.kind.value; }

/** Choosing an income-only or expense-only category sets the type for you. */
function inferKind() {
  const option = form.elements.category.selectedOptions[0];
  if (option && (option.dataset.kind === 'income' || option.dataset.kind === 'expense')) {
    form.elements.kind.value = option.dataset.kind;
    syncCategories();
    form.elements.category.value = option.value;
  }
}

/* ---------- split rows ---------- */

function categoryOptions() {
  return $$('#tx-category option', dialog).filter((o) => o.value && !o.disabled)
    .map((o) => h('option', { value: o.value, text: o.textContent }));
}

function addSplitRow(category = '', amount = '') {
  const select = h('select', { 'aria-label': 'Categoria desta parte' }, h('option', { value: '', text: 'Categoria' }), ...categoryOptions());
  select.value = category;
  const input = h('input', { type: 'text', inputmode: 'decimal', 'data-money': '', 'aria-label': 'Valor desta parte', value: amount, placeholder: 'Valor' });
  const row = h('div', { class: 'split-row' }, select, input,
    h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': 'Remover esta parte', onclick: () => { row.remove(); updateLeft(); } }, icon('x')));
  input.addEventListener('input', updateLeft);
  $('#split-rows', dialog).append(row);
  wireMoneyInputs(row);
  updateLeft();
}

function splitRows() {
  return $$('#split-rows .split-row', dialog).map((row) => ({
    category: $('select', row).value, amount: parseMoney($('input', row).value),
  }));
}

function updateLeft() {
  const total = Number(parseMoney(form.elements.amount.value)) || 0;
  const used = splitRows().reduce((sum, r) => sum + (Number(r.amount) || 0), 0);
  const note = $('#split-left', dialog);
  if (!$('#split-rows .split-row', dialog)) { note.textContent = ''; return; }
  const left = Math.round((total - used) * 100) / 100;
  note.textContent = left === 0 ? 'As partes fecham o total.' : left > 0 ? `Faltam ${formatMoney(left)} para distribuir.` : `${formatMoney(-left)} a mais que o total.`;
}

async function showHistory() {
  const box = $('#tx-history', dialog);
  if (!box.hidden) { box.hidden = true; return; }
  try {
    const { entries } = await api(`/api/transactions/${editingId}/history/`);
    box.replaceChildren(h('h3', { text: 'Histórico' }), entries.length
      ? h('ul', { class: 'mini-list small' }, ...entries.map((e) => h('li', {}, h('span', { text: e.summary || e.action }), h('span', { class: 'muted', text: new Date(e.when).toLocaleString() }))))
      : h('p', { class: 'muted small', text: 'Nenhuma alteração registrada.' }));
    box.hidden = false;
  } catch (error) { toast(error.message, { kind: 'error' }); }
}

// Card counted by invoice month (Settings): the date asked is the day of the purchase.
function syncCardDate() {
  const option = form.elements.account.selectedOptions[0];
  const card = Boolean(option && option.dataset.type === 'credit') && document.body.dataset.cardMode === 'invoice';
  $('[data-date-label]', form).textContent = card ? 'Data da compra' : 'Data';
  $('[data-card-date-help]', form).hidden = !card;
}

function syncCategories() {
  const select = form.elements.category;
  const wanted = kind();
  for (const option of select.options) {
    if (!option.value) continue;
    const ok = option.dataset.kind === 'both' || option.dataset.kind === wanted;
    option.hidden = !ok;
    option.disabled = !ok;
  }
  const current = select.selectedOptions[0];
  if (current && current.disabled) select.value = '';
}

/** Tell the user which category a rule would pick when they leave it on "automatic". */
async function hint() {
  const note = $('[data-rule-hint]', form);
  if (form.elements.category.value) { note.hidden = true; return; }
  const text = `${form.elements.name.value} ${form.elements.payee.value}`.trim();
  if (text.length < 2) { note.hidden = true; return; }
  try {
    const { category } = await api('/api/rules/suggest/', { query: { text, kind: kind() } });
    note.hidden = !category;
    if (category) note.textContent = `Uma regra vai classificar como ${category.full_name}.`;
  } catch (e) { note.hidden = true; }
}

async function submit(event) {
  event.preventDefault();
  clearErrors(form);
  const button = $('[data-submit]', form);
  const payload = formData(form);
  payload.tags = String(payload.tags || '').split(',').map((t) => t.trim()).filter(Boolean);
  const parts = splitRows().filter((r) => r.category || r.amount);
  let url = editingId ? `/api/transactions/${editingId}/` : '/api/transactions/';
  if (!editingId && parts.length) {
    url = '/api/transactions/split/';
    payload.splits = parts;
    delete payload.category;
  }
  try {
    const result = await withBusy(button, () => api(url, {
      method: 'POST', body: payload, version: editingId ? editingVersion : undefined,
      conflict: { copyPath: '/api/transactions/', onCurrent: (current) => openTransactionDialog(current) },
    }));
    remember('last-kind', payload.kind);
    if (payload.category) remember(`last-category-${payload.kind}`, payload.category);
    if (payload.account) remember('last-account', payload.account);
    dialog.close();
    toast(editingId ? 'Transação atualizada.' : parts.length ? 'Transação dividida adicionada.' : 'Transação adicionada.');
    if (result.warning) toast(result.warning, { kind: 'warn', timeout: 7000 });
    document.dispatchEvent(new CustomEvent('tx:changed', { detail: result }));
  } catch (error) {
    showErrors(form, error);
  }
}

/** Open the dialog for a new transaction, or for editing when `transaction` is given.
 *  `preset` pre-fills a new one (used by quick add). */
export function openTransactionDialog(transaction = null, preset = null) {
  if (!init()) return;
  clearErrors(form);
  form.reset();
  $('[data-rule-hint]', form).hidden = true;
  editingId = transaction && !(preset && preset.duplicate) ? transaction.id : null;
  editingVersion = editingId ? transaction.version : null;
  $('#split-rows', dialog).replaceChildren();
  updateLeft();
  $('#tx-history', dialog).hidden = true;
  $('#tx-history-btn', dialog).hidden = !editingId;
  $('#tx-split-box', dialog).hidden = Boolean(editingId);
  $('#tx-split-box', dialog).open = false;
  $('#tx-more', dialog).open = false;
  $('#tx-title', dialog).textContent = editingId ? 'Editar transação' : transaction ? 'Duplicar transação' : 'Adicionar transação';
  if (transaction) {
    form.elements.kind.value = transaction.kind;
    form.elements.name.value = transaction.name;
    setMoney(form.elements.amount, transaction.amount);
    // On a card counted by invoice month, the field holds the day of the purchase.
    form.elements.date.value = editingId ? (transaction.purchase_date || transaction.date) : today();
    form.elements.note.value = transaction.note || '';
    form.elements.payee.value = transaction.payee || '';
    form.elements.tags.value = (transaction.tags || []).join(', ');
    syncCategories();
    form.elements.category.value = String(transaction.category.id);
    if (transaction.account) form.elements.account.value = String(transaction.account.id);
    if (transaction.payee || (transaction.tags || []).length || transaction.note) $('#tx-more', dialog).open = true;
  } else {
    const lastKind = recall('last-kind');
    form.elements.kind.value = (preset && preset.kind) || (lastKind === 'income' ? 'income' : 'expense');
    form.elements.date.value = (preset && preset.date) || today();
    syncCategories();
    const last = recall(`last-category-${form.elements.kind.value}`);
    const option = last && $(`option[value="${CSS.escape(last)}"]`, form.elements.category);
    if (option && !option.disabled) form.elements.category.value = last;
    // The account from Settings ("Conta padrão"); without one, the last account used.
    const account = document.body.dataset.defaultAccount || recall('last-account');
    if (account && $(`#tx-account option[value="${CSS.escape(account)}"]`, form)) form.elements.account.value = account;
    if (preset) {
      if (preset.name) form.elements.name.value = preset.name;
      if (preset.amount) setMoney(form.elements.amount, preset.amount);
      if (preset.category) form.elements.category.value = String(preset.category);
      if (preset.account && $(`#tx-account option[value="${CSS.escape(String(preset.account))}"]`, form)) {
        form.elements.account.value = String(preset.account);
      }
    }
  }
  syncCardDate();
  const code = $('[data-currency-code]', dialog);
  if (code) code.textContent = document.body.dataset.currency ? `(${document.body.dataset.currency})` : '';
  openDialog(dialog, '#tx-name');
  hint();
}

export async function duplicateTransaction(id) {
  try {
    const { transaction } = await api(`/api/transactions/${id}/`);
    openTransactionDialog(transaction, { duplicate: true });
  } catch (error) {
    toast(error.message, { kind: 'error' });
  }
}

export async function editTransaction(id) {
  try {
    const { transaction } = await api(`/api/transactions/${id}/`);
    openTransactionDialog(transaction);
  } catch (error) {
    toast(error.message, { kind: 'error' });
  }
}
