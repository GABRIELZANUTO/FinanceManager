// Settings: API keys (create, show once, list, revoke).
import { $, api, confirmDialog, emptyState, errorState, h, showError, showErrors, toast } from './core.js';

const form = document.getElementById('key-form');
const list = document.getElementById('key-list');
const fresh = document.getElementById('key-new');
const SCOPES = { read: 'ler', write: 'alterar', sync: 'sincronizar' };

function when(iso) {
  return iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : 'nunca';
}

async function load() {
  try {
    const { tokens } = await api('/api/tokens/');
    list.className = '';
    list.replaceChildren(tokens.length ? h('ul', { class: 'mini-list' }, ...tokens.map((t) => h('li', {},
      h('span', {}, t.name, h('span', { class: 'sub', text:
        `${t.scopes.map((s) => SCOPES[s] || s).join(', ')} · criada ${when(t.created_at)} · último uso ${when(t.last_used_at)}`
        + (t.expires_at ? ` · vale até ${when(t.expires_at)}` : '') })),
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => revoke(t) }, 'Revogar'))))
      : emptyState('Nenhuma chave ativa', 'Crie uma chave para usar seus dados em outro programa.'));
  } catch (error) {
    list.className = '';
    list.replaceChildren(errorState(error.message, load));
  }
}

async function revoke(t) {
  const ok = await confirmDialog({ title: 'Revogar chave', message: `Revogar “${t.name}”? O que usa essa chave para de funcionar na hora.` });
  if (!ok) return;
  try {
    await api(`/api/tokens/${t.id}/`, { method: 'DELETE' });
    toast('Chave revogada.');
    load();
  } catch (error) { showError(error); }
}

function showKey(token) {
  const field = h('input', { type: 'text', readonly: true, value: token, 'aria-label': 'Chave criada', class: 'mono' });
  const copy = h('button', { class: 'btn btn-sm', type: 'button', onclick: async () => {
    try { await navigator.clipboard.writeText(token); toast('Chave copiada.'); } catch (e) { field.select(); }
  } }, 'Copiar');
  fresh.replaceChildren(h('p', { text: 'Copie a chave agora: ela não aparece de novo.' }), h('p', { class: 'actions' }, field, copy));
  fresh.hidden = false;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const name = String(data.get('name') || '').trim();
  if (!name) { $('#key-name').focus(); return; }
  try {
    const result = await api('/api/tokens/', { method: 'POST', body: {
      name, scopes: ['read', ...data.getAll('scopes')], expires_days: data.get('expires_days') || null,
    } });
    form.reset();
    showKey(result.token);
    load();
  } catch (error) { showErrors(form, error); }
});

load();
