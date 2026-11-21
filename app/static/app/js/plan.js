// Month plan on the Analytics page: what already happened and what is still to come.
import {
  $, api, clearErrors, confirmDialog, errorState, formData, formatMoney, h, icon, openDialog, setMoney, showError, showErrors,
  toast, withBusy, wireDialog, wireMoneyInputs,
} from './core.js';
import { openTransactionDialog } from './tx-dialog.js';
import { dateBR } from './format.js';
import { catLabel } from './parts.js';

const section = document.getElementById('plano');
const body = document.getElementById('plan-body');
const payDialog = document.getElementById('pay-dialog');
const payForm = document.getElementById('pay-form');
const MONTHS = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];
const STATUS = {
  done: ['Realizado', 'income'], scheduled: ['Lançado, data futura', ''], planned: ['Previsto', ''], late: ['Atrasado', 'warn'],
};
const INVOICE = { paid: 'Paga', open: 'Em aberto', overdue: 'Vencida sem pagamento registrado' };

const todayIso = section.dataset.today;
const currentMonth = todayIso.slice(0, 7);
let month = currentMonth;
let filter = 'all';
let plan = null;

const label = (ym) => { const name = MONTHS[Number(ym.slice(5, 7)) - 1]; return `${name[0].toUpperCase()}${name.slice(1)} de ${ym.slice(0, 4)}`; };
const signed = (row) => (row.kind === 'income' ? '+' : '-') + formatMoney(row.amount);

function stat(title, value, note, cls = '', extra = null) {
  return h('div', { class: `card stat ${cls}` }, h('div', { class: 'label', text: title }),
    h('div', { class: 'value', text: formatMoney(value) }), h('div', { class: 'note', text: note }), extra);
}

// Below the month's result: what the accounts add up to at the start and, with it, at the end of the month.
function balanceLine(b) {
  if (!b) return null;
  const end = Number(b.end);
  return h('div', { class: 'stat-balance' },
    h('div', { class: 'note', text: `Contas no início do mês: ${formatMoney(b.start)}` }),
    Number(b.debts) ? h('div', { class: 'note', text: `Dívidas e divisões no mês: ${Number(b.debts) > 0 ? '+' : ''}${formatMoney(b.debts)}` }) : null,
    h('div', { class: 'stat-balance-end' }, 'Livre no fim do mês: ',
      h('strong', { class: end < 0 ? 'neg' : 'pos', text: formatMoney(b.end) })),
    h('div', { class: 'note small', text: 'Bancos, dinheiro e carteiras menos o que se deve nos cartões, mais a sobra do mês e o que entra ou sai com dívidas.' }));
}

function totals(t, kind, balance) {
  const future = kind !== 'past';
  return h('div', { class: 'grid cols-4' },
    stat('Entradas', t.income, future ? `Já entrou ${formatMoney(t.income_done)}, falta ${formatMoney(t.income_planned)}` : 'Tudo realizado'),
    stat('Saídas', t.expense, future ? `Já saiu ${formatMoney(t.expense_done)}, falta ${formatMoney(t.expense_planned)}` : 'Tudo realizado'),
    stat('Gastos estimados', t.estimate, future ? 'Variáveis: o que resta do orçamento ou a média' : 'Mês encerrado'),
    stat(future ? 'Sobra prevista' : 'Resultado do mês', t.result, 'Entradas menos saídas e estimados',
      `stat-wide${Number(t.result) < 0 ? ' warn' : ''}`, balanceLine(balance)));
}

function bankStrip(bank) {
  if (!bank) return null;
  const lines = [h('p', {}, 'Nos bancos hoje: ', h('strong', { text: formatMoney(bank.now) }),
    '. Nos bancos no fim do mês: ', h('strong', { class: Number(bank.end) < 0 ? 'neg' : '', text: formatMoney(bank.end) }), '.')];
  if (bank.lowest && Number(bank.lowest.balance) < Number(bank.now)) {
    lines.push(h('p', { class: 'small muted', text: `Menor saldo previsto: ${formatMoney(bank.lowest.balance)} em ${dateBR(bank.lowest.date)}.` }));
  }
  if (bank.first_negative) {
    lines.push(h('p', { class: 'small neg', text: `Atenção: o saldo dos bancos fica negativo em ${dateBR(bank.first_negative)}.` }));
  }
  lines.push(h('p', { class: 'small muted', text: 'Conta salário, contas fixas e faturas do cartão. Os gastos estimados não entram aqui, porque ainda não se sabe de qual conta vão sair.' }));
  if (bank.days.length) {
    lines.push(h('details', {}, h('summary', { text: 'Ver o saldo dos bancos dia a dia' }),
      h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, h('th', { text: 'Data' }), h('th', { class: 'num', text: 'Movimento previsto' }), h('th', { class: 'num', text: 'Saldo' }))),
        h('tbody', {}, ...bank.days.map((d) => h('tr', {}, h('td', { text: dateBR(d.date) }),
          h('td', { class: `num ${Number(d.change) < 0 ? 'neg' : 'pos'}`, text: formatMoney(d.change) }),
          h('td', { class: `num ${Number(d.balance) < 0 ? 'neg' : ''}`, text: formatMoney(d.balance) }))))))));
  }
  return h('div', { class: `notice ${bank.first_negative ? 'warn' : ''}` }, ...lines);
}

function countedOnce(list) {
  if (!list || !list.length) return null;
  return h('div', { class: 'notice warn' },
    h('p', {}, h('strong', { text: 'Parece já lançado: confirme' }),
      '. Enquanto você não responder, cada par conta uma vez só.'),
    h('ul', { class: 'match-list' }, ...list.map((m) => h('li', {},
      h('span', { class: 'small', text: `“${m.recurring}” de ${dateBR(m.due)} (${formatMoney(m.amount)}) e “${m.transaction}” lançado em ${dateBR(m.date)} (${formatMoney(m.transaction_amount)})` }),
      h('span', { class: 'match-actions' },
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => decideMatch(m, 'same'), text: 'É o mesmo' }),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => decideMatch(m, 'different'), text: 'São diferentes' }))))));
}

async function decideMatch(m, decision) {
  try {
    await api(`/api/recurring/${m.recurring_id}/match/`, { method: 'POST', body: { slot: m.due, transaction: m.transaction_id, decision } });
    toast(decision === 'same' ? 'Vinculado: conta uma vez só.' : 'Entendido: os dois entram.');
    load();
  } catch (error) { showError(error); }
}

function invoicesCard(invoices) {
  const items = invoices.length ? h('ul', { class: 'mini-list' }, ...invoices.map((i) => h('li', {},
    h('span', {}, `Fatura ${i.account}`, h('span', { class: `sub ${i.status === 'overdue' ? 'neg' : ''}`,
      text: `Vence em ${dateBR(i.due)} · ${INVOICE[i.status]}${Number(i.paid) > 0 && i.status !== 'paid' ? `, já pago ${formatMoney(i.paid)}` : ''}` })),
    h('span', { class: 'amt' }, formatMoney(i.status === 'paid' ? i.amount : i.open),
      i.status !== 'paid' ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => openPay(i), text: 'Pagar fatura' }) : null))))
    : h('p', { class: 'muted small', text: 'Nenhuma fatura neste mês. Informe o dia do vencimento no cadastro do cartão (Contas) para ela aparecer aqui.' });
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', { text: 'Faturas do cartão' })), items,
    h('p', { class: 'muted small', text: 'Estimativa pelas compras desde o vencimento anterior. Não soma nas saídas: as compras já estão lá. O pagamento é uma transferência do banco para o cartão.' }));
}

// Debts module: money lent, borrowed or paid back in the month, and installments still to come.
const DEBT_STATUS = { done: 'Feito', planned: 'Previsto', late: 'Atrasado' };

function debtsCard(rows) {
  if (!rows || !rows.length) return null;
  const items = rows.map((r) => h('li', {},
    h('span', {}, r.person, h('span', { class: `sub ${r.status === 'late' ? 'neg' : ''}`,
      text: `${dateBR(r.date)} · ${r.label}${r.description ? ` · ${r.description}` : ''} · ${DEBT_STATUS[r.status]}${r.account ? ` · ${r.account}` : ''}` })),
    h('span', { class: 'amt' },
      h('span', { class: r.effect > 0 ? 'pos' : 'neg', text: `${Number(r.effect) > 0 ? '+' : '-'}${formatMoney(r.amount)}` }),
      r.planned ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => confirmDebt(r), text: r.receive ? 'Recebido' : 'Pago' }) : null)));
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', { text: 'Dívidas e divisões' }), h('a', { href: '/people/', text: 'Abrir' })),
    h('ul', { class: 'mini-list' }, ...items),
    h('p', { class: 'muted small', text: 'Não entram nas receitas nem nas despesas: o dinheiro só foi e voltou. Entram no saldo das contas.' }));
}

async function confirmDebt(row) {
  try {
    await api(`/api/entries/${row.id}/confirm/`, { method: 'POST', body: {} });
    toast(row.receive ? 'Recebimento registrado.' : 'Pagamento registrado.');
    load();
  } catch (error) { showError(error); }
}

function estimatesCard(estimates, kind) {
  let content;
  if (kind === 'past') content = h('p', { class: 'muted small', text: 'Mês encerrado: só valores realizados.' });
  else if (!estimates.length) content = h('p', { class: 'muted small', text: 'Nada a estimar: os orçamentos já foram usados ou não há histórico de gastos variáveis.' });
  else {
    content = h('ul', { class: 'mini-list' }, ...estimates.map((e) => h('li', {},
      h('span', {}, catLabel(e.category, 'span'), h('span', { class: 'sub', text: e.basis === 'budget' ? 'O que resta do orçamento' : 'Média dos últimos 3 meses' })),
      h('span', { class: 'amt neg', text: formatMoney(e.amount) }))));
  }
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', { text: 'Gastos estimados' }), h('a', { href: '/budgets/', text: 'Orçamentos' })), content);
}

function rowFor(r) {
  const [text, cls] = STATUS[r.status];
  const extra = [];
  if (r.source === 'once') extra.push(h('span', { class: 'tag-chip', text: 'Previsão' }));
  if (r.source === 'recurring') extra.push(h('span', { class: 'tag-chip', text: 'Recorrente' }));
  const actions = h('td', { class: 'row-actions' });
  if (r.source === 'once') {
    actions.append(h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Excluir a previsão ${r.name}`, onclick: () => removeForecast(r) }, icon('trash-2')));
  } else if (r.source === 'transaction') {
    actions.append(h('a', { class: 'icon-btn', href: `/transactions/?q=${encodeURIComponent(r.name)}`, 'aria-label': `Ver ${r.name} no extrato` }, icon('eye')));
  } else {
    actions.append(h('a', { class: 'icon-btn', href: '/recurring/', 'aria-label': `Ver a recorrência ${r.name}` }, icon('repeat')));
  }
  return h('tr', { class: r.status === 'done' ? '' : 'plan-future' },
    h('td', { 'data-label': 'Data', text: dateBR(r.date) }),
    h('td', { class: 'name-cell', 'data-label': 'Descrição' }, r.name, ...extra),
    h('td', { 'data-label': 'Categoria' }, catLabel(r.category, 'span')),
    h('td', { 'data-label': 'Conta', text: r.account + (r.credit ? ' (cartão)' : '') }),
    h('td', { 'data-label': 'Situação' }, h('span', { class: `tag ${cls}`, text: text })),
    h('td', { class: `num ${r.kind === 'income' ? 'pos' : 'neg'}`, 'data-label': 'Valor', text: signed(r) }),
    actions);
}

function rowsTable(rows) {
  const shown = rows.filter((r) => filter === 'all' || (filter === 'done' ? r.status === 'done' : r.status !== 'done'));
  const chips = h('div', { class: 'chips plan-filter', role: 'group', 'aria-label': 'Mostrar' },
    ...[['all', 'Tudo'], ['todo', 'O que vai vencer'], ['done', 'O que já aconteceu']].map(([value, text]) =>
      h('button', { class: `chip ${filter === value ? 'active' : ''}`, type: 'button', 'aria-pressed': String(filter === value),
        onclick: () => { filter = value; render(); }, text })));
  if (!shown.length) return h('div', {}, chips, h('p', { class: 'muted', text: 'Nada para mostrar neste filtro.' }));
  return h('div', {}, chips, h('div', { class: 'table-wrap' }, h('table', { class: 'table stack plan-table' },
    h('thead', {}, h('tr', {}, ...['Data', 'Descrição', 'Categoria', 'Conta', 'Situação'].map((t) => h('th', { text: t })),
      h('th', { class: 'num', text: 'Valor' }), h('th', { text: '' }))),
    h('tbody', {}, ...shown.map(rowFor)))));
}

function render() {
  if (!plan) return;
  $('#plan-month').textContent = label(plan.month.slice(0, 7));
  $('#plan-prev').disabled = !plan.previous;
  $('#plan-next').disabled = !plan.next;
  $('#plan-current').disabled = month === currentMonth;
  body.className = '';
  body.replaceChildren(totals(plan.totals, plan.kind, plan.balance), bankStrip(plan.bank) || '', countedOnce(plan.counted_once) || '',
    h('div', { class: 'grid cols-2' }, invoicesCard(plan.invoices), estimatesCard(plan.estimates, plan.kind)),
    debtsCard(plan.debts) || '',
    rowsTable(plan.rows));
}

async function load() {
  try {
    plan = await api('/api/plan/', { query: { month } });
    month = plan.month.slice(0, 7);
    render();
  } catch (error) {
    body.className = '';
    body.replaceChildren(errorState(error.message, load));
  }
}

function go(target) {
  if (!target) return;
  month = target.slice(0, 7);
  load();
}

async function removeForecast(row) {
  const ok = await confirmDialog({ title: 'Excluir previsão', message: `Excluir a previsão “${row.name}” de ${dateBR(row.date)}?` });
  if (!ok) return;
  try {
    await api(`/api/recurring/${row.id}/`, { method: 'DELETE' });
    toast('Previsão excluída.');
    load();
  } catch (error) { showError(error); }
}

function openPay(invoice) {
  clearErrors(payForm);
  payForm.reset();
  payForm.elements.to_account.value = String(invoice.account_id);
  setMoney(payForm.elements.amount, invoice.open);
  payForm.elements.date.value = invoice.due < todayIso ? todayIso : invoice.due;
  payForm.elements.note.value = `Pagamento da fatura ${invoice.account} (${dateBR(invoice.due)})`.slice(0, 200);
  $('#pay-title').textContent = `Pagar fatura ${invoice.account}`;
  openDialog(payDialog, '#pay-from');
}

payForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors(payForm);
  if (!payForm.elements.from_account.value) {
    showErrors(payForm, { message: 'Cadastre uma conta de banco em Contas para pagar a fatura.' });
    return;
  }
  try {
    await withBusy($('[data-submit]', payForm), () => api('/api/transfers/', { method: 'POST', body: formData(payForm) }));
    payDialog.close();
    toast('Pagamento registrado.');
    load();
  } catch (error) { showErrors(payForm, error); }
});

function openNewEntry() {
  const first = `${month}-01`;
  openTransactionDialog(null, { date: month === currentMonth || first < todayIso ? todayIso : first });
}

document.addEventListener('tx:changed', load);
$('#plan-prev').addEventListener('click', () => go(plan && plan.previous));
$('#plan-next').addEventListener('click', () => go(plan && plan.next));
$('#plan-current').addEventListener('click', () => go(currentMonth));
$('#plan-add').addEventListener('click', openNewEntry);
wireDialog(payDialog);
wireMoneyInputs(payDialog);
load();
