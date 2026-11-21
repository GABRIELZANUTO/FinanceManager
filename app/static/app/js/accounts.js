import {
  $, api, clearErrors, confirmDialog, emptyState, errorState, formData, formatMoney, h, icon, openDialog, setMoney, showError,
  showErrors, toast, withBusy, wireDialog, wireMoneyInputs,
} from './core.js';
import { dateBR } from './format.js';

const host = document.getElementById('account-list');
const tHost = document.getElementById('transfer-list');
const dialog = document.getElementById('account-dialog');
const form = document.getElementById('account-form');
const tDialog = document.getElementById('transfer-dialog');
const tForm = document.getElementById('transfer-form');
let accounts = [];
let editing = null;

const TYPE = { bank: 'Banco', cash: 'Dinheiro', wallet: 'Carteira digital', credit: 'Crédito' };

function limitInfo(a) {
  if (a.type !== 'credit') return null;
  const parts = [];
  if (a.credit_limit) {
    const pct = Math.min(100, a.used_percent || 0);
    const bar = h('div', { class: `progress ${pct >= 90 ? 'over' : pct >= 70 ? 'near' : ''}`, role: 'progressbar', 'aria-valuemin': '0',
      'aria-valuemax': '100', 'aria-valuenow': String(Math.round(pct)), 'aria-label': `Limite usado de ${a.name}` }, h('span'));
    bar.firstChild.style.width = `${pct}%`;
    parts.push(h('p', { class: 'small', text: `Disponível ${formatMoney(a.available)} de ${formatMoney(a.credit_limit)} (${a.used_percent}% usado)` }), bar);
  }
  if (Number(a.installments_left) > 0) {
    parts.push(h('p', { class: 'muted small', text: `Usado: compras ${formatMoney(a.used_purchases)} + parcelas a vencer ${formatMoney(a.installments_left)} (${a.installments_count} parcela${a.installments_count === 1 ? '' : 's'})` }));
    parts.push(h('details', { class: 'more small' }, h('summary', { text: 'Ver compras parceladas' }),
      h('ul', { class: 'mini-list' }, ...a.installment_plans.map((p) => h('li', {},
        h('span', {}, p.name, h('span', { class: 'sub', text: `faltam ${p.left} de ${formatMoney(p.each)} · última em ${dateBR(p.last)}` })),
        h('span', { class: 'amt', text: formatMoney(p.amount) }))))));
  }
  if (a.next_due) parts.push(h('p', { class: 'muted small', text: `Próximo vencimento: ${dateBR(a.next_due)}${a.closing_day ? ` · fecha todo dia ${a.closing_day}` : ''}` }));
  return parts.length ? h('div', { class: 'account-limit' }, ...parts) : null;
}

function card(a) {
  return h('article', { class: `card account${a.archived ? ' archived' : ''}` },
    h('div', { class: 'account-head' }, h('span', { class: 'cat-label' }, icon(a.icon, 'cat-icon'), h('strong', { text: a.name })),
      h('span', { class: 'tag', text: a.archived ? 'Arquivada' : TYPE[a.type] })),
    h('div', { class: `account-balance ${Number(a.balance) < 0 ? 'neg' : ''}`, text: formatMoney(a.balance) }),
    limitInfo(a),
    Number(a.scheduled) ? h('p', { class: 'muted small', text: `Lançamentos com data futura: ${Number(a.scheduled) > 0 ? '+' : ''}${formatMoney(a.scheduled)}` }) : null,
    h('p', { class: 'muted small', text: `Saldo inicial ${formatMoney(a.opening_balance)}` }),
    h('div', { class: 'actions' },
      h('a', { class: 'btn btn-sm', href: `/transactions/?account=${a.id}`, text: 'Ver movimentações' }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Editar ${a.name}`, onclick: () => openEdit(a) }, icon('pencil')),
      h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Excluir ${a.name}`, onclick: () => remove(a) }, icon('trash-2'))));
}

async function load() {
  try {
    const data = await api('/api/accounts/');
    accounts = data.accounts;
    host.className = 'account-grid';
    // Money and card debt apart: the card balance is what you owe, not money you have.
    const money = accounts.filter((a) => a.type !== 'credit').reduce((sum, a) => sum + Number(a.balance), 0);
    const debt = accounts.filter((a) => a.type === 'credit').reduce((sum, a) => sum + Math.max(0, -Number(a.balance)), 0);
    host.replaceChildren(...accounts.map(card),
      h('div', { class: 'card account total' }, h('div', { class: 'account-head' }, h('strong', { text: 'Dinheiro nas contas' })),
        h('div', { class: `account-balance ${money < 0 ? 'neg' : ''}`, text: formatMoney(money) }),
        debt ? h('p', { class: 'small', text: `Faturas a pagar: ${formatMoney(debt)}` }) : null,
        debt ? h('p', { class: 'muted small', text: `Livre depois das faturas: ${formatMoney(money - debt)}` }) : null));
  } catch (error) {
    host.className = '';
    host.replaceChildren(errorState(error.message, load));
  }
  loadTransfers();
}

async function loadTransfers() {
  try {
    const { transfers } = await api('/api/transfers/');
    tHost.className = '';
    if (!transfers.length) { tHost.replaceChildren(emptyState('Nenhuma transferência ainda', 'Mova dinheiro entre contas sem afetar receitas ou despesas.')); return; }
    tHost.replaceChildren(h('ul', { class: 'mini-list' }, ...transfers.map((t) => h('li', {},
      h('span', {}, `${t.from.name} → ${t.to.name}`, h('span', { class: 'sub', text: `${dateBR(t.date)}${t.note ? ` · ${t.note}` : ''}` })),
      h('span', { class: 'amt' }, formatMoney(t.amount),
        h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': 'Excluir esta transferência', onclick: () => removeTransfer(t) }, icon('trash-2')))))));
  } catch (error) {
    tHost.className = '';
    tHost.replaceChildren(errorState(error.message, loadTransfers));
  }
}

function openEdit(account) {
  editing = account;
  clearErrors(form);
  form.reset();
  $('#account-title').textContent = account ? 'Editar conta' : 'Nova conta';
  if (account) {
    form.elements.name.value = account.name;
    form.elements.type.value = account.type;
    setMoney(form.elements.opening_balance, account.opening_balance);
    form.elements.archived.checked = account.archived;
    if (account.credit_limit) setMoney(form.elements.credit_limit, account.credit_limit);
    form.elements.due_day.value = account.due_day || '';
    form.elements.closing_day.value = account.closing_day || '';
  }
  syncCreditFields();
  openDialog(dialog, '#account-name');
}

function syncCreditFields() {
  $('[data-credit-only]', form).hidden = form.elements.type.value !== 'credit';
}
form.elements.type.addEventListener('change', syncCreditFields);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(form);
  try {
    await withBusy($('[data-submit]', form), () => api(editing ? `/api/accounts/${editing.id}/` : '/api/accounts/', { method: 'POST', body: formData(form), version: editing && editing.version }));
    dialog.close();
    toast('Conta salva.');
    await load();
  } catch (error) { showErrors(form, error); }
});

async function remove(account) {
  const ok = await confirmDialog({ title: 'Excluir conta', message: `Excluir “${account.name}”? Contas com movimentação só podem ser arquivadas.` });
  if (!ok) return;
  try {
    await api(`/api/accounts/${account.id}/`, { method: 'DELETE', version: account.version });
    toast('Conta excluída.');
    await load();
  } catch (error) { showError(error); }
}

function openTransfer() {
  clearErrors(tForm);
  const active = accounts.filter((a) => !a.archived);
  const options = () => active.map((a) => h('option', { value: a.id, text: `${a.name} (${formatMoney(a.balance)})` }));
  tForm.elements.from_account.replaceChildren(...options());
  tForm.elements.to_account.replaceChildren(...options());
  if (active.length > 1) tForm.elements.to_account.value = String(active[1].id);
  tForm.elements.amount.value = '';
  tForm.elements.note.value = '';
  openDialog(tDialog, '#tr-amount');
}

tForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(tForm);
  try {
    await withBusy($('[data-submit]', tForm), () => api('/api/transfers/', { method: 'POST', body: formData(tForm) }));
    tDialog.close();
    toast('Transferência registrada.');
    await load();
  } catch (error) { showErrors(tForm, error); }
});

async function removeTransfer(t) {
  const ok = await confirmDialog({ title: 'Excluir transferência', message: `Excluir a transferência de ${t.from.name} para ${t.to.name}?` });
  if (!ok) return;
  try {
    await api(`/api/transfers/${t.id}/`, { method: 'DELETE', version: t.version });
    toast('Transferência excluída.');
    await load();
  } catch (error) { showError(error); }
}

wireDialog(dialog);
wireDialog(tDialog);
wireMoneyInputs(dialog);
wireMoneyInputs(tDialog);
document.getElementById('new-account').addEventListener('click', () => openEdit(null));
document.getElementById('new-transfer').addEventListener('click', openTransfer);
load();
