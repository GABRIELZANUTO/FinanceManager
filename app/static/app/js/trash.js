import { $, api, confirmDialog, emptyState, errorState, formatMoney, h, icon, showError, toast } from './core.js';
import { catLabel } from './parts.js';
import { dateBR } from './format.js';

const ACTIONS = {
  created: 'criada', updated: 'alterada', deleted: 'excluída', restored: 'restaurada', recategorised: 'recategorizada', imported: 'importada',
};
const list = document.getElementById('trash-list');
const activity = document.getElementById('activity-list');

async function loadTrash() {
  try {
    const data = await api('/api/trash/');
    list.className = '';
    $('#empty-trash').hidden = data.transactions.length === 0;
    if (!data.transactions.length) {
      list.replaceChildren(emptyState('A lixeira está vazia', 'Transações excluídas aparecem aqui e podem ser restauradas.'));
      return;
    }
    list.replaceChildren(h('ul', { class: 'mini-list' }, ...data.transactions.map((t) => h('li', {},
      h('span', {}, t.name, h('span', { class: 'sub' }, catLabel(t.category, 'span'), ` · ${dateBR(t.date)} · ${t.days_left === 1 ? 'resta 1 dia' : `restam ${t.days_left} dias`}`)),
      h('span', { class: 'amt' }, h('span', { class: t.kind === 'income' ? 'pos' : 'neg', text: `${t.kind === 'income' ? '+' : '-'}${formatMoney(t.amount)} ` }),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: () => restore(t) }, icon('rotate-ccw'), 'Restaurar'),
        h('button', { class: 'icon-btn danger', type: 'button', 'aria-label': `Excluir ${t.name} para sempre`, onclick: () => remove(t) }, icon('trash-2')))))));
  } catch (error) {
    list.className = '';
    list.replaceChildren(errorState(error.message, loadTrash));
  }
}

async function loadActivity() {
  try {
    const { entries } = await api('/api/activity/');
    activity.className = '';
    activity.replaceChildren(entries.length ? h('ul', { class: 'mini-list' }, ...entries.map((e) => h('li', {},
      h('span', {}, e.summary || e.action, h('span', { class: 'sub', text: new Date(e.when).toLocaleString() })),
      h('span', { class: 'tag', text: ACTIONS[e.action] || e.action })))) : emptyState('Nenhuma alteração ainda', 'Inclusões, edições e exclusões de transações ficam registradas aqui.'));
  } catch (error) {
    activity.className = '';
    activity.replaceChildren(errorState(error.message, loadActivity));
  }
}

async function restore(t) {
  try {
    await api(`/api/transactions/${t.id}/restore/`, { method: 'POST', version: t.version });
    toast('Transação restaurada.');
    loadTrash();
    loadActivity();
  } catch (error) { showError(error); }
}

async function remove(t) {
  const ok = await confirmDialog({ title: 'Excluir para sempre', message: `Excluir “${t.name}” definitivamente? Isso não pode ser desfeito.` });
  if (!ok) return;
  try {
    await api(`/api/trash/${t.id}/`, { method: 'DELETE', version: t.version });
    toast('Excluída definitivamente.');
    loadTrash();
  } catch (error) { showError(error); }
}

$('#empty-trash').addEventListener('click', async () => {
  const ok = await confirmDialog({ title: 'Esvaziar lixeira', message: 'Excluir definitivamente tudo o que está na lixeira?', confirmLabel: 'Esvaziar lixeira' });
  if (!ok) return;
  try {
    const r = await api('/api/trash/empty/', { method: 'POST' });
    toast(`${r.removed} transação(ões) removida(s).`);
    loadTrash();
  } catch (error) { showError(error); }
});

loadTrash();
loadActivity();
