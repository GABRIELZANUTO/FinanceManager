// Debts and shared bills: people, what happened with each one, installments, settling up.
import {
  $, $$, api, clearErrors, confirmDialog, emptyState, errorState, formData, formatMoney, h, icon, openDialog, parseMoney,
  setMoney, showError, showErrors, toast, withBusy, wireDialog, wireMoneyInputs,
} from './core.js';
import { dateBR } from './format.js';

const host = document.getElementById('people-list');
const totals = document.getElementById('people-totals');
const todayIso = host.dataset.today;
const defaultAccount = host.dataset.defaultAccount;
const personDialog = document.getElementById('person-dialog');
const personForm = document.getElementById('person-form');
const entryDialog = document.getElementById('entry-dialog');
const entryForm = document.getElementById('entry-form');
const settleDialog = document.getElementById('settle-dialog');
const settleForm = document.getElementById('settle-form');
const splitDialog = document.getElementById('split-dialog');
const splitForm = document.getElementById('split-form');
const summaryDialog = document.getElementById('summary-dialog');

const SPLITS = ['split_me', 'split_them'];
const PAYMENTS = ['paid_me', 'paid_them'];
const SHOWN = 5;
let people = [];
let current = null;     // the person the entry form is for
let editingEntry = null;
let settling = null;

// Account and category lists come from <template> elements, filled once into every select.
function fillSelect(select, templateId) {
  select.replaceChildren(document.getElementById(templateId).content.cloneNode(true));
}
for (const select of [entryForm.elements.account, settleForm.elements.account, splitForm.elements.account]) fillSelect(select, 'account-options');
for (const select of [entryForm.elements.category, splitForm.elements.category]) fillSelect(select, 'category-options');

function sentence(p) {
  const money = formatMoney(Math.abs(Number(p.net)));
  if (p.direction === 'owes_you') return `${p.name} te deve ${money}`;
  if (p.direction === 'you_owe') return `Você deve ${money} a ${p.name}`;
  return `Você e ${p.name} estão quites`;
}

function entryLine(e) {
  const details = [dateBR(e.date)];
  if (e.description) details.push(e.description);
  if (e.share) details.push(`sua parte ${formatMoney(e.share.amount)}`);
  if (e.account) details.push(e.account.name);
  if (e.due_date) details.push(`vence ${dateBR(e.due_date)}`);
  return h('li', {},
    h('span', {}, e.kind_label, h('span', { class: 'sub', text: details.join(' · ') })),
    h('span', { class: 'amt' }, h('span', { class: Number(e.amount) < 0 ? 'neg' : 'pos', text: formatMoney(e.amount) }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Editar este lançamento', onclick: () => openEntry(currentOf(e), e) }, icon('pencil')),
      h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': 'Remover este lançamento', onclick: () => removeEntry(e) }, icon('trash-2'))));
}

function plannedLine(e) {
  const receive = Number(e.amount) < 0;
  const late = e.date < todayIso;
  return h('li', {},
    h('span', {}, receive ? 'Vai te pagar' : 'Você vai pagar',
      h('span', { class: `sub ${late ? 'neg' : ''}`, text: `${dateBR(e.date)}${late ? ' (atrasada)' : ''}${e.description ? ` · ${e.description}` : ''}${e.account ? ` · ${e.account.name}` : ''}` })),
    h('span', { class: 'amt' }, h('span', { class: receive ? 'pos' : 'neg', text: formatMoney(Math.abs(Number(e.amount))) }),
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => confirmPlanned(e), text: receive ? 'Recebido' : 'Pago' }),
      h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': 'Remover esta parcela', onclick: () => removeEntry(e) }, icon('trash-2'))));
}

function currentOf(entry) {
  return people.find((p) => p.entries.some((e) => e.id === entry.id) || p.planned.some((e) => e.id === entry.id));
}

function card(p) {
  const list = h('ul', { class: 'mini-list small' }, ...p.entries.slice(0, SHOWN).map(entryLine));
  const more = p.entries.length > SHOWN ? h('button', {
    class: 'btn btn-quiet btn-sm', type: 'button', text: `Ver todos (${p.entries.length})`,
    onclick: (event) => { list.replaceChildren(...p.entries.map(entryLine)); event.currentTarget.remove(); },
  }) : null;
  return h('article', { class: 'card person' },
    h('div', { class: 'person-head' }, h('h2', { text: p.name }),
      h('div', { class: 'person-tools' },
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Copiar resumo para ${p.name}`, title: 'Copiar resumo', onclick: () => openSummary(p) }, icon('copy')),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Renomear ${p.name}`, title: 'Renomear', onclick: () => openPerson(p) }, icon('pencil')),
        h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Excluir ${p.name}`, title: 'Excluir', onclick: () => removePerson(p) }, icon('trash-2')))),
    h('p', { class: `person-net ${p.direction === 'owes_you' ? 'pos' : p.direction === 'you_owe' ? 'neg' : 'muted'}`, text: sentence(p) }),
    p.next_due ? h('p', { class: 'small muted', text: `Próximo vencimento: ${dateBR(p.next_due)}` }) : null,
    p.planned.length ? h('div', { class: 'person-planned' }, h('h3', { class: 'small', text: 'Parcelas previstas' }),
      h('ul', { class: 'mini-list small' }, ...p.planned.map(plannedLine))) : null,
    p.entries.length ? list : h('p', { class: 'muted small', text: 'Nada registrado ainda.' }),
    more,
    h('div', { class: 'actions' },
      h('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => openEntry(p) }, icon('plus'), 'Novo lançamento'),
      p.direction !== 'settled' ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => openSettle(p) }, icon('check'), 'Quitar') : null));
}

function stat(label, value, cls) {
  return h('div', { class: 'card stat' }, h('div', { class: 'label', text: label }), h('div', { class: `value ${cls}`, text: formatMoney(value) }));
}

async function load() {
  try {
    const data = await api('/api/people/');
    people = data.people;
    const planned = people.reduce((sum, p) => sum + Number(p.planned_total), 0);
    totals.replaceChildren(stat('Te devem', data.owed_to_you, 'pos'), stat('Você deve', data.you_owe, 'neg'),
      h('div', { class: 'card stat' }, h('div', { class: 'label', text: 'Parcelas previstas' }),
        h('div', { class: `value ${planned < 0 ? 'pos' : planned > 0 ? 'neg' : ''}`, text: formatMoney(Math.abs(planned)) }),
        h('div', { class: 'note', text: planned < 0 ? 'a receber, somando todas' : planned > 0 ? 'a pagar, somando todas' : 'nenhuma' })));
    host.className = 'goal-grid';
    if (!people.length) {
      host.className = '';
      host.replaceChildren(emptyState('Ninguém ainda', 'Adicione um amigo para controlar empréstimos e contas divididas.',
        h('button', { class: 'btn btn-primary', type: 'button', text: 'Adicionar pessoa', onclick: () => openPerson(null) })));
      return;
    }
    host.replaceChildren(...people.map(card));
  } catch (error) {
    host.className = '';
    host.replaceChildren(errorState(error.message, load));
  }
}

/* ---------- people ---------- */

let editingPerson = null;
function openPerson(person) {
  editingPerson = person;
  clearErrors(personForm);
  personForm.reset();
  $('#person-title').textContent = person ? 'Renomear' : 'Adicionar pessoa';
  if (person) personForm.elements.name.value = person.name;
  openDialog(personDialog, '#person-name');
}

personForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(personForm);
  try {
    await withBusy($('[data-submit]', personForm), () => api(editingPerson ? `/api/people/${editingPerson.id}/` : '/api/people/', { method: 'POST', body: formData(personForm), version: editingPerson && editingPerson.version }));
    personDialog.close();
    toast('Salvo.');
    await load();
  } catch (error) { showErrors(personForm, error); }
});

async function removePerson(p) {
  const ok = await confirmDialog({ title: 'Excluir pessoa', message: `Excluir ${p.name} e tudo o que foi registrado com essa pessoa?` });
  if (!ok) return;
  try {
    await api(`/api/people/${p.id}/`, { method: 'DELETE', version: p.version });
    toast('Excluído.');
    await load();
  } catch (error) { showError(error); }
}

/* ---------- entries ---------- */

const ACCOUNT_HELP = {
  lent: 'De onde saiu o dinheiro que você emprestou.',
  borrowed: 'Onde entrou o dinheiro que você pegou emprestado.',
  split_me: 'Com qual conta ou cartão você pagou a conta inteira.',
  split_them: 'A conta de onde vai sair o dinheiro quando você pagar a sua parte. Nada sai agora.',
  paid_me: 'Onde entrou o dinheiro que a pessoa te devolveu.',
  paid_them: 'De onde saiu o dinheiro que você devolveu.',
};

function syncKind() {
  const kind = entryForm.elements.kind.value;
  const split = SPLITS.includes(kind);
  const many = entryForm.elements.in_installments.checked;
  entryForm.querySelector('[data-for="amount"]').hidden = split;
  entryForm.querySelector('[data-for="total"]').hidden = !split || (Boolean(editingEntry) && kind === 'split_them');
  entryForm.querySelector('[data-for="share"]').hidden = !split;
  entryForm.querySelector('[data-for="due"]').hidden = !(kind === 'lent' || kind === 'borrowed') || many;
  entryForm.querySelector('[data-for="category"]').hidden = !(split && entryForm.elements.account.value);
  entryForm.querySelector('[data-for="installments"]').hidden = Boolean(editingEntry);
  entryForm.querySelector('[data-installments-fields]').hidden = !many;
  entryForm.querySelector('[data-installments-label]').textContent = PAYMENTS.includes(kind)
    ? 'São várias parcelas (o valor acima é o de cada uma)' : 'Acertar em parcelas (dividir o valor em várias)';
  entryForm.querySelector('[data-amount-label]').textContent = PAYMENTS.includes(kind) && many ? 'Valor de cada parcela' : 'Valor';
  entryForm.querySelector('[data-account-help]').textContent = ACCOUNT_HELP[kind] || '';
}
entryForm.elements.kind.addEventListener('change', syncKind);
entryForm.elements.account.addEventListener('change', syncKind);
entryForm.elements.in_installments.addEventListener('change', syncKind);

function openEntry(person, entry = null) {
  current = person;
  editingEntry = entry;
  clearErrors(entryForm);
  entryForm.reset();
  entryForm.elements.kind.disabled = Boolean(entry);
  entryForm.elements.date.value = todayIso;
  entryForm.elements.account.value = defaultAccount;
  if (entry) {
    entryForm.elements.kind.value = entry.kind;
    entryForm.elements.date.value = entry.date;
    entryForm.elements.due_date.value = entry.due_date || '';
    entryForm.elements.description.value = entry.description || '';
    entryForm.elements.account.value = entry.account ? String(entry.account.id) : '';
    const value = Math.abs(Number(entry.amount));
    if (entry.kind === 'split_me') {
      // the entry keeps the other person's part; your share is the linked expense
      const share = entry.share ? Number(entry.share.amount) : 0;
      setMoney(entryForm.elements.total, value + share);
      setMoney(entryForm.elements.my_share, share);
    } else if (entry.kind === 'split_them') {
      setMoney(entryForm.elements.my_share, value);   // only your share is kept: it is what you owe
    } else {
      setMoney(entryForm.elements.amount, value);
    }
    if (entry.share) entryForm.elements.category.value = String(entry.share.category);
    $('#entry-title').textContent = `Editar lançamento com ${person.name}`;
  } else {
    $('#entry-title').textContent = `Novo lançamento com ${person.name}`;
  }
  syncKind();
  openDialog(entryDialog, entry ? '#entry-date' : '#entry-kind');
}

entryForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(entryForm);
  const body = formData(entryForm);
  if (editingEntry) body.kind = editingEntry.kind;
  if (!entryForm.elements.in_installments.checked || editingEntry) { delete body.installments; delete body.first_due; }
  delete body.in_installments;
  for (const key of Object.keys(body)) if (body[key] === '') delete body[key];
  const url = editingEntry ? `/api/entries/${editingEntry.id}/` : `/api/people/${current.id}/entries/`;
  try {
    await withBusy($('[data-submit]', entryForm), () => api(url, { method: 'POST', body, version: editingEntry && editingEntry.version }));
    entryDialog.close();
    toast(editingEntry ? 'Lançamento alterado.' : 'Lançamento adicionado.');
    await load();
  } catch (error) { showErrors(entryForm, error); }
});

async function removeEntry(e) {
  const ok = await confirmDialog({
    title: e.planned ? 'Remover parcela' : 'Remover lançamento',
    message: e.account && !e.planned ? `O valor volta para ${e.account.name}${e.share ? ' e a despesa da sua parte também é removida' : ''}.` : 'Essa ação não pode ser desfeita.',
    confirmLabel: 'Remover',
  });
  if (!ok) return;
  try {
    await api(`/api/entries/${e.id}/`, { method: 'DELETE', version: e.version });
    toast('Removido.');
    await load();
  } catch (error) { showError(error); }
}

async function confirmPlanned(e) {
  try {
    await api(`/api/entries/${e.id}/confirm/`, { method: 'POST', body: {}, version: e.version });
    toast(Number(e.amount) < 0 ? 'Recebimento registrado.' : 'Pagamento registrado.');
    await load();
  } catch (error) { showError(error); }
}

/* ---------- settle up ---------- */

function openSettle(person) {
  settling = person;
  clearErrors(settleForm);
  settleForm.reset();
  settleForm.elements.account.value = defaultAccount;
  settleForm.elements.date.value = todayIso;
  const money = formatMoney(Math.abs(Number(person.net)));
  settleForm.querySelector('[data-settle-text]').textContent = person.direction === 'owes_you'
    ? `${person.name} te pagou ${money} e vocês ficam quites.` : `Você pagou ${money} a ${person.name} e vocês ficam quites.`;
  $('#settle-title').textContent = `Quitar com ${person.name}`;
  openDialog(settleDialog, '#settle-account');
}

settleForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(settleForm);
  const body = formData(settleForm);
  if (!body.account) delete body.account;
  try {
    await withBusy($('[data-submit]', settleForm), () => api(`/api/people/${settling.id}/settle/`, { method: 'POST', body }));
    settleDialog.close();
    toast('Quitado.');
    await load();
  } catch (error) { showErrors(settleForm, error); }
});

/* ---------- one bill, several people ---------- */

function openSplit() {
  if (!people.length) { toast('Adicione as pessoas primeiro.', { kind: 'warn' }); return; }
  clearErrors(splitForm);
  splitForm.reset();
  splitForm.elements.account.value = defaultAccount;
  splitForm.elements.date.value = todayIso;
  splitForm.querySelector('[data-split-people]').replaceChildren(...people.map((p) =>
    h('label', { class: 'check' }, h('input', { type: 'checkbox', value: String(p.id), 'data-person': '' }), ` ${p.name}`)));
  previewSplit();
  openDialog(splitDialog, '#split-total');
}

function previewSplit() {
  const chosen = $$('[data-person]:checked', splitForm).length;
  const total = Number(parseMoney(splitForm.elements.total.value)) || 0;
  const typed = parseMoney(splitForm.elements.my_share.value);
  const mine = typed === '' ? total / (chosen + 1) : Number(typed) || 0;
  splitForm.querySelector('[data-split-preview]').textContent = chosen && total > mine
    ? `Cada pessoa vai te dever ${formatMoney((total - mine) / chosen)}. A sua parte: ${formatMoney(mine)}.` : '';
}
splitForm.addEventListener('input', previewSplit);
splitForm.addEventListener('change', previewSplit);

splitForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(splitForm);
  const body = formData(splitForm);
  body.people = $$('[data-person]:checked', splitForm).map((box) => Number(box.value));
  for (const key of ['my_share', 'account', 'category']) if (body[key] === '') delete body[key];
  try {
    await withBusy($('[data-submit]', splitForm), () => api('/api/people/split/', { method: 'POST', body }));
    splitDialog.close();
    toast('Conta dividida.');
    await load();
  } catch (error) { showErrors(splitForm, error); }
});

/* ---------- summary to send ---------- */

async function openSummary(person) {
  try {
    const { text } = await api(`/api/people/${person.id}/summary/`);
    $('#summary-text').value = text;
    $('#summary-title').textContent = `Resumo para ${person.name}`;
    openDialog(summaryDialog, '[data-copy-target]');
  } catch (error) { showError(error); }
}

for (const dialog of [personDialog, entryDialog, settleDialog, splitDialog, summaryDialog]) wireDialog(dialog);
for (const dialog of [entryDialog, splitDialog]) wireMoneyInputs(dialog);
$('#new-person').addEventListener('click', () => openPerson(null));
$('#split-many').addEventListener('click', openSplit);
load();
