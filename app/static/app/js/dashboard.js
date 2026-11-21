import { $, api, emptyState, errorState, formatMoney, formatNumber, h, icon, showError, toast, withBusy } from './core.js';
import { dateBR, monthLong } from './format.js';
import { budgetRow, catLabel } from './parts.js';
import { openTransactionDialog } from './tx-dialog.js';

// The overview counts everything dated up to the last day of the current month.
function monthEnd() {
  const now = new Date();
  const last = new Date(now.getFullYear(), now.getMonth() + 1, 0);
  return `${last.getFullYear()}-${String(last.getMonth() + 1).padStart(2, '0')}-${String(last.getDate()).padStart(2, '0')}`;
}

function setStat(id, value, note, { tint, warn } = {}) {
  const card = document.getElementById(id);
  const valueNode = card.querySelector('.value');
  valueNode.textContent = value;
  valueNode.classList.remove('pos', 'neg');
  if (tint) valueNode.classList.add(tint);
  card.classList.toggle('warn', Boolean(warn));
  card.querySelector('.note').replaceChildren(...(Array.isArray(note) ? note : [note]));
}

function change(previous, pct, goodWhenUp) {
  if (pct === null || pct === undefined) return Number(previous) === 0 ? 'Sem dados no mês passado' : '';
  const up = pct > 0;
  const good = pct === 0 ? null : up === goodWhenUp;
  return [h('span', { class: good === null ? '' : good ? 'pos' : 'neg', text: `${up ? '+' : ''}${pct}%` }), ' em relação ao mês passado'];
}

function stamp() {
  const now = new Date();
  $('#updated').textContent = `Atualizado às ${now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

async function loadSummary() {
  const box = $('#summary-error');
  try {
    const data = await api('/api/summary/');
    box.hidden = true;
    $('#month-label').textContent = monthLong(data.month);
    // Money (banks, cash, wallets) as of today; below, what still comes in or goes out this month
    // and what is owed on the cards.
    const approx = secondary(data, data.money_today);
    const lastDay = dateBR(data.until).slice(0, 5);
    const ahead = Number(data.money) - Number(data.money_today);
    const parts = [];
    if (Math.abs(ahead) >= 0.005) {
      parts.push(`${ahead > 0 ? '+' : '-'} ${formatMoney(Math.abs(ahead))} ${ahead > 0 ? 'a entrar' : 'a sair'} até ${lastDay}. Nos bancos no fim do mês: ${formatMoney(data.money)}`);
    }
    if (Number(data.card_debt)) parts.push(`Faturas a pagar: ${formatMoney(data.card_debt)}`);
    if (Number(data.estimate)) parts.push(`Gastos estimados: ${formatMoney(data.estimate)}`);
    if (Number(data.balance) !== Number(data.money)) parts.push(`Livre no fim do mês: ${formatMoney(data.balance)}`);
    const note = parts.join('. ') || 'Saldo de hoje em bancos, dinheiro e carteiras';
    setStat('stat-balance', formatMoney(data.money_today), data.overdrawn ? `Saldo negativo. ${note}` : approx ? `${approx}. ${note}` : note,
      { tint: Number(data.money_today) < 0 ? 'neg' : null, warn: data.overdrawn });
    setStat('stat-income', formatMoney(data.income), change(data.previous.income, data.changes.income, true));
    setStat('stat-expense', formatMoney(data.expense), change(data.previous.expense, data.changes.expense, false));
    const rate = data.savings_rate === null ? 'Nenhuma receita neste mês ainda' : `Taxa de poupança ${data.savings_rate}%`;
    setStat('stat-net', formatMoney(data.net), rate, { tint: Number(data.net) < 0 ? 'neg' : Number(data.net) > 0 ? 'pos' : null });
    stamp();
  } catch (error) {
    box.replaceChildren(errorState(error.message, loadSummary));
    box.hidden = false;
  }
}

async function loadRecent() {
  const host = $('#recent');
  try {
    const data = await api('/api/transactions/', { query: { page_size: 10, date_to: monthEnd() } });
    host.className = '';
    if (!data.transactions.length) {
      host.replaceChildren(emptyState('Nenhuma transação ainda', 'Adicione a primeira para vê-la aqui.',
        h('button', { class: 'btn btn-primary', type: 'button', text: 'Adicionar transação', 'data-new-transaction': '' })));
      return;
    }
    const list = h('ul', { class: 'mini-list' });
    for (const t of data.transactions.slice(0, 8)) {
      list.append(h('li', {},
        h('span', {}, t.name, h('span', { class: 'sub' }, catLabel(t.category, 'span'), ` · ${dateBR(t.date)}`)),
        h('span', { class: `amt ${t.kind === 'income' ? 'pos' : 'neg'}`, text: (t.kind === 'income' ? '+' : '-') + formatMoney(t.amount) })));
    }
    host.replaceChildren(list);
  } catch (error) {
    host.className = '';
    host.replaceChildren(errorState(error.message, loadRecent));
  }
}

async function loadBudgets() {
  const host = $('#budgets');
  try {
    const data = await api('/api/budgets/');
    host.className = '';
    const items = (data.overall ? [data.overall] : []).concat(data.budgets.slice().sort((a, b) => b.percent - a.percent).slice(0, 5));
    if (!items.length) {
      host.replaceChildren(emptyState('Nenhum orçamento definido', 'Defina limites mensais para controlar os gastos.',
        h('a', { class: 'btn', href: '/budgets/', text: 'Definir orçamento' })));
      return;
    }
    const rows = items.map((b) => budgetRow(b));
    if (data.overall) rows.push(h('p', { class: 'small muted', text: `Você pode gastar ${formatMoney(data.overall.safe_per_day)} por dia até o fim do mês.` }));
    host.replaceChildren(...rows);
  } catch (error) {
    host.className = '';
    host.replaceChildren(errorState(error.message, loadBudgets));
  }
}

async function loadAccounts() {
  const host = $('#accounts');
  try {
    const data = await api('/api/accounts/');
    host.className = '';
    const active = data.accounts.filter((a) => !a.archived);
    host.replaceChildren(h('ul', { class: 'mini-list' }, ...active.map((a) => h('li', {},
      h('span', { class: 'cat-label' }, icon(a.icon, 'cat-icon'), a.name,
        a.type === 'credit' && a.available ? h('span', { class: 'sub', text: `Disponível ${formatMoney(a.available)}` }) : null),
      h('span', { class: `amt ${Number(a.balance) < 0 ? 'neg' : ''}`, text: formatMoney(a.balance) })))));
  } catch (error) {
    host.className = '';
    host.replaceChildren(errorState(error.message, loadAccounts));
  }
}

async function loadPlan() {
  const host = $('#plan-line');
  if (!host) return;
  try {
    const p = await api('/api/plan/');
    const t = p.totals;
    const parts = [
      h('strong', { text: 'Previsão até o fim do mês: ' }), h('span', {}, 'entra ', h('strong', { class: 'pos', text: formatMoney(t.income) }),
        ', sai ', h('strong', { class: 'neg', text: formatMoney(Number(t.expense) + Number(t.estimate)) }),
        ' e sobra ', h('strong', { class: Number(t.result) < 0 ? 'neg' : 'pos', text: formatMoney(t.result) }), '.'),
    ];
    if (p.bank) {
      parts.push(h('span', { class: 'muted' }, ' Nos bancos no fim do mês: ',
        h('strong', { class: Number(p.bank.end) < 0 ? 'neg' : '', text: formatMoney(p.bank.end) }), '.'));
      if (p.bank.first_negative) parts.push(h('span', { class: 'neg', text: ` Atenção: fica negativo em ${dateBR(p.bank.first_negative)}.` }));
    }
    parts.push(h('span', { class: 'muted' }, ' Livre no fim do mês: ',
      h('strong', { class: Number(p.balance.end) < 0 ? 'neg' : '', text: formatMoney(p.balance.end) }), '.'));
    host.replaceChildren(h('p', { class: 'plan-line' }, ...parts, ' ', h('a', { href: '/analytics/#plano', text: 'Ver o planejamento' })));
    host.hidden = false;
  } catch (error) {
    host.hidden = true;
  }
}

function secondary(data, value) {
  if (!data.secondary || !data.secondary.rate) return '';
  const converted = Number(value) * Number(data.secondary.rate);
  return `\u2248 ${data.secondary.code} ${formatNumber(converted)}`;
}

async function loadInsights() {
  try {
    const data = await api('/api/insights/');
    const card = $('#insights');
    card.hidden = data.sentences.length === 0;
    $('#insight-list').replaceChildren(...data.sentences.map((s) => h('li', { class: s.level },
      icon(s.level === 'bad' ? 'circle-alert' : s.level === 'warn' ? 'triangle-alert' : s.level === 'good' ? 'circle-check' : 'trending-up'),
      h('a', { href: s.url, text: s.text }))));

    const bills = $('#bills');
    bills.className = '';
    bills.replaceChildren(data.bills.length ? h('ul', { class: 'mini-list' }, ...data.bills.map((b) => h('li', {},
      h('span', {}, b.name, h('span', { class: `sub ${b.days <= 3 ? 'neg' : ''}`, text: b.days < 0 ? `Vencida desde ${dateBR(b.due)}` : b.days === 0 ? 'Vence hoje' : `Vence em ${dateBR(b.due)} (daqui a ${b.days} dia${b.days === 1 ? '' : 's'})` })),
      h('span', { class: 'amt', text: formatMoney(b.amount) }))))
      : emptyState('Nada vencendo em breve', 'Cadastre aluguel, assinaturas ou empréstimos como contas recorrentes.', h('a', { class: 'btn', href: '/recurring/', text: 'Adicionar conta' })));

    const goals = $('#goals');
    goals.className = '';
    goals.replaceChildren(data.goals.length ? h('div', {}, ...data.goals.map((g) => {
      const bar = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(g.percent)), 'aria-label': `Progresso de ${g.name}` }, h('span'));
      bar.firstChild.style.width = `${g.percent}%`;
      return h('div', { class: 'budget-row' }, h('div', { class: 'budget-top' }, h('strong', { text: g.name })),
        h('div', { class: 'budget-amt' }, h('span', { text: `${formatMoney(g.saved)} de ${formatMoney(g.target)}` }), h('span', { class: 'muted', text: `${g.percent}%` })), bar);
    })) : emptyState('Nenhuma meta ainda', 'Dê um nome para algo que você está guardando dinheiro.', h('a', { class: 'btn', href: '/goals/', text: 'Criar meta' })));

    const movers = $('#movers');
    movers.className = '';
    const row = (m, cls) => h('li', {}, h('span', {}, h('span', { class: `tone tone-${m.color}` }), m.name),
      h('span', { class: `amt ${cls}`, text: `${Number(m.change) > 0 ? '+' : '-'}${formatMoney(Math.abs(Number(m.change)))}` }));
    const items = data.movers.up.map((m) => row(m, 'neg')).concat(data.movers.down.map((m) => row(m, 'pos')));
    movers.replaceChildren(items.length ? h('ul', { class: 'mini-list' }, ...items) : emptyState('Ainda não há dados suficientes', 'Volte depois de registrar mais algumas transações.'));

    const subs = $('#subs');
    subs.className = '';
    subs.replaceChildren(data.subscriptions.length ? h('ul', { class: 'mini-list' }, ...data.subscriptions.map((x) => h('li', {},
      h('span', {}, x.name, h('span', { class: 'sub', text: `${x.months} meses seguidos${x.tracked ? ', já cadastrada como conta' : `, próxima por volta de ${dateBR(x.next_date)}`}` })),
      h('span', { class: 'amt', text: formatMoney(x.amount) }))))
      : emptyState('Nenhum pagamento repetido encontrado', 'Eles aparecem depois de três meses de pagamentos parecidos.'));
    return data;
  } catch (error) {
    for (const id of ['bills', 'goals', 'movers', 'subs']) {
      const node = $(`#${id}`);
      node.className = '';
      node.replaceChildren(errorState(error.message, loadInsights));
    }
    return null;
  }
}

async function loadAlerts() {
  const card = $('#alerts');
  try {
    const data = await api('/api/notifications/');
    // budget alerts are already spelled out in "Como você está indo"
    const items = data.notifications.filter((n) => n.kind !== 'budget');
    card.hidden = items.length === 0;
    $('#alert-list').replaceChildren(...items.map((n) => h('li', { class: n.level },
      icon(n.level === 'error' ? 'circle-alert' : 'triangle-alert'), h('a', { href: n.url, text: n.text }))));
  } catch (error) { card.hidden = true; }
}

/* ---------- onboarding ---------- */

async function loadOnboarding(data) {
  const card = $('#onboarding');
  try {
    data = data || await api('/api/onboarding/');
    card.hidden = data.dismissed;
    $('#onb-list').replaceChildren(...data.steps.map((s) => h('li', { class: s.done ? 'done' : '' },
      icon(s.done ? 'circle-check' : 'flag'), h('a', { href: s.url, text: s.label }))));
    $('#onb-sample').hidden = !data.can_load_sample;
  } catch (error) { card.hidden = true; }
}

$('#onb-dismiss').addEventListener('click', async () => {
  try { await api('/api/onboarding/', { method: 'POST' }); $('#onboarding').hidden = true; } catch (error) { showError(error); }
});
$('#load-sample').addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    await api('/api/sample-data/', { method: 'POST' });
    toast('Dados de exemplo adicionados.');
    window.location.reload();
  } catch (error) { event.target.disabled = false; showError(error); }
});

/* ---------- quick add: "Bolo 50,00 restaurante conta principal" ---------- */
// The server reads the line (app/quick.py): name, amount, category and account by similarity.

const form = $('#quick-add');
const preview = $('#quick-preview');

async function showPreview(event, pickedCategory = null) {
  if (event) event.preventDefault();
  const text = $('#quick-input').value;
  let p;
  try {
    ({ preview: p } = await api('/api/quick/', { method: 'POST', body: { text, category: pickedCategory } }));
  } catch (error) {
    preview.replaceChildren(h('span', { class: 'neg', text: error.message }));
    preview.hidden = false;
    return;
  }
  const chip = (label, cls = '') => h('span', { class: `chip-static ${cls}`, text: label });
  const category = p.category ? chip(p.category.name)
    : p.new_category ? chip(`${p.new_category} (categoria nova)`, 'new') : chip('Sem categoria', 'neg');
  const chosen = p.category ? p.category.id : null;
  const save = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Adicionar', onclick: () => commit(text, save, pickedCategory || chosen) });
  // A new category close to an existing one: offer the existing one instead.
  const suggest = p.new_category && p.suggestion ? h('p', { class: 'small' },
    `Não achei "${p.new_category}" nas suas categorias. `,
    h('button', { class: 'btn btn-sm', type: 'button', text: `Usar ${p.suggestion.name}`, onclick: () => showPreview(null, p.suggestion.id) }),
    ' ou adicione para criar a categoria nova.') : null;
  const edit = h('button', { class: 'btn btn-sm', type: 'button', text: 'Editar detalhes', onclick: () => {
    openTransactionDialog(null, { name: p.name, amount: p.amount, kind: p.kind, date: p.date,
      category: p.category && p.category.id, account: p.account.id });
  } });
  const cancel = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', text: 'Cancelar', onclick: () => { preview.hidden = true; } });
  preview.replaceChildren(
    h('div', { class: 'quick-chips' },
      chip(p.name), chip(`${p.kind === 'income' ? '+' : '-'}${formatMoney(p.amount)}`, p.kind === 'income' ? 'pos' : 'neg'),
      category, chip(`${p.account.credit ? 'Cartão: ' : ''}${p.account.name}`), chip(dateBR(p.date))),
    suggest || '',
    ...p.notes.map((note) => h('p', { class: 'small muted', text: note })),
    h('div', { class: 'quick-actions' }, save, edit, cancel));
  preview.hidden = false;
  if (!p.category && !p.new_category) save.disabled = true;
}

async function commit(text, button, category = null) {
  try {
    const result = await withBusy(button, () => api('/api/quick/', { method: 'POST', body: { text, save: true, category } }));
    toast('Transação adicionada.');
    if (result.warning) toast(result.warning, { kind: 'warn' });
    $('#quick-input').value = '';
    preview.hidden = true;
    document.dispatchEvent(new CustomEvent('tx:changed'));
  } catch (error) { showError(error); }
}

form.addEventListener('submit', showPreview);

function loadAll() { loadSummary(); loadPlan(); loadRecent(); loadBudgets(); loadAccounts(); loadAlerts(); loadOnboarding(); loadInsights(); }
loadAll();
document.addEventListener('tx:changed', loadAll);
