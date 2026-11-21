// Small renderers shared by several pages.
import { formatMoney, h, icon, tone } from './core.js';

export function catLabel(category, tag = 'strong') {
  return h(tag, { class: 'cat-label' }, icon(category.icon || 'tag', `cat-icon tone-text-${category.color}`), category.name);
}

/** One budget (category or overall) with a progress bar and alert badge. */
export function budgetRow(b, { detail = false } = {}) {
  const percent = Math.min(b.percent, 100);
  const level = b.level;
  const bar = h('div', {
    class: `progress ${level === 'over' ? 'over' : level === 'warn' ? 'near' : ''}`, role: 'progressbar',
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(percent)),
    'aria-label': `Orçamento ${b.category ? `de ${b.category.name}` : 'geral'} utilizado`,
  }, h('span'));
  bar.firstChild.style.width = `${percent}%`;
  const remaining = Number(b.remaining);
  const badge = level === 'over' ? h('span', { class: 'tag expense', text: 'Acima do orçamento' })
    : level === 'warn' ? h('span', { class: 'tag warn', text: `${Math.round(b.percent)}% usado` }) : null;
  const name = b.category ? catLabel(b.category) : h('strong', { text: 'Mês inteiro' });
  const row = h('div', { class: 'budget-row' },
    h('div', { class: 'budget-top' }, h('span', { class: 'budget-name' }, name, badge, b.rollover ? h('span', { class: 'tag', text: 'Acumula saldo' }) : null)),
    h('div', { class: 'budget-amt' }, h('span', { class: 'amt' }, `Gasto ${formatMoney(b.spent)} de ${formatMoney(b.available)}`),
      remaining < 0 ? h('span', { class: 'over-note', text: `Estourou em ${formatMoney(-remaining)}` })
        : h('span', { class: 'muted', text: `Restam ${formatMoney(remaining)}` })),
    bar);
  // What is still to come this month (future-dated entries, recurring items), apart from what was spent.
  if (Number(b.planned) > 0) {
    const plannedBar = h('span', { class: 'progress-planned' });
    plannedBar.style.left = `${percent}%`;
    plannedBar.style.width = `${Math.max(0, Math.min(100, (Number(b.with_planned) / Number(b.available)) * 100) - percent)}%`;
    bar.append(plannedBar);
    row.append(h('p', { class: `small ${b.planned_over ? 'neg' : 'muted'}`,
      text: `Previsto até o fim do mês: mais ${formatMoney(b.planned)} (total ${formatMoney(b.with_planned)})${b.planned_over ? ': vai passar do limite' : ''}` }));
  }
  if (detail) {
    const parts = [`Pode gastar ${formatMoney(b.safe_per_day)} por dia`];
    if (Number(b.carry) > 0) parts.push(`inclui ${formatMoney(b.carry)} acumulado do mês anterior`);
    if (!Number(b.planned)) parts.push(`no ritmo atual vai gastar ${formatMoney(b.projected)}`);
    row.append(h('p', { class: `budget-note small ${b.projected_over ? 'neg' : 'muted'}`, text: parts.join(' · ') }));
  }
  return row;
}

export { tone };
