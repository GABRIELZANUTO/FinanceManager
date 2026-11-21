// Phone app: sync state in the top bar, a notice on the dashboard when there are conflicts, and a
// blocking "Sincronizando" screen while a synchronisation runs (nothing can be seen or changed
// meanwhile; the page reloads afterwards when data arrived).
import { api, h, toast } from './core.js';

const link = document.getElementById('sync-indicator');
const busy = document.getElementById('sync-busy');
const IDLE_EVERY = 2000;
const BUSY_EVERY = 700;
let dataVersion = null;
let timer = null;
let lastStatus = null;
let asked = null;    // { at, before }: a sync asked from the top bar, until it has run

function renderIndicator(st) {
  if (!link) return;
  link.hidden = !st.connected;
  if (!st.connected) return;
  const conflicts = st.open_conflicts || st.conflicts;
  let label = 'Sincronizado';
  let kind = 'ok';
  if (conflicts) { label = `${conflicts} conflito(s) para revisar`; kind = 'bad'; }
  else if (st.running) { label = 'Sincronizando'; kind = 'busy'; }
  else if (st.last_error) { label = `Sem conexão${st.pending ? `, ${st.pending} pendente(s)` : ''}`; kind = 'warn'; }
  else if (st.pending) { label = `${st.pending} pendente(s)`; kind = 'warn'; }
  link.dataset.state = kind;
  link.setAttribute('aria-label', `Sincronização: ${label}`);
  link.title = `${label}. Toque para sincronizar.`;
  const badge = link.querySelector('.badge');
  const count = conflicts || st.pending;
  badge.hidden = !count;
  badge.textContent = count || '';
  const notice = document.getElementById('sync-notice');
  if (notice) {
    notice.hidden = !conflicts;
    if (conflicts) {
      notice.replaceChildren(h('span', { text: `Há ${conflicts} conflito(s) de sincronização para revisar. ` }),
        h('a', { href: '/sync/#conflitos', text: 'Revisar' }));
    }
  }
}

function showBusy(on) {
  if (!busy) return;
  if (on && !busy.open) {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    busy.showModal();           // on top of any open form; the rest of the page is inert
  } else if (!on && busy.open) {
    busy.close();
  }
  document.documentElement.toggleAttribute('aria-busy', on);
}

function formOpen() {
  return [...document.querySelectorAll('dialog[open]')].some((d) => d !== busy && d.querySelector('form'));
}

async function tick() {
  let st = null;
  try { st = await api('/api/sync/status/'); } catch (e) { /* not ready or offline: try again */ }
  if (st) {
    lastStatus = st;
    renderIndicator(st);
    // Asked from the top bar and not run yet (the worker starts a moment later): keep the screen.
    const waiting = asked && !st.running && st.last_sync_at === asked.before && st.last_error === asked.error
      && Date.now() - asked.at < 8000;
    if (asked && !waiting && !st.running) {
      asked = null;
      const conflicts = st.open_conflicts || st.conflicts;
      if (st.last_error) toast(st.last_error, { kind: 'error', timeout: 7000 });
      else if (conflicts) toast(`Sincronizado, com ${conflicts} conflito(s) para revisar em Sincronização.`, { kind: 'warn', timeout: 7000 });
      else toast('Tudo sincronizado.');
    }
    showBusy(Boolean(st.connected && (st.running || waiting)));
    if (dataVersion === null) dataVersion = st.data_version;
    else if (!st.running && st.data_version !== dataVersion) {
      dataVersion = st.data_version;
      // New data from the server: show it (a form being filled is kept; its save may then
      // show "alterado em outro lugar" if that record changed).
      if (!formOpen()) { window.location.reload(); return; }
    }
  }
  schedule(st && (st.running || asked) ? BUSY_EVERY : IDLE_EVERY);
}

function schedule(delay) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    if (document.visibilityState === 'visible') tick();
    else schedule(IDLE_EVERY);
  }, delay);
}

async function syncHere(event) {
  // The top bar shortcut synchronises right here; the full page stays in the menu (Sincronização).
  event.preventDefault();
  if (busy && busy.open) return;
  asked = { at: Date.now(), before: lastStatus && lastStatus.last_sync_at, error: lastStatus && lastStatus.last_error };
  showBusy(true);
  try {
    await api('/api/sync/run/', { method: 'POST', body: {} });
  } catch (error) {
    asked = null;
    showBusy(false);
    return;
  }
  schedule(BUSY_EVERY);
}

if (link) {
  link.addEventListener('click', syncHere);
  if (busy) busy.addEventListener('cancel', (event) => event.preventDefault());   // Esc does not close it
  tick();
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') tick(); });
}
