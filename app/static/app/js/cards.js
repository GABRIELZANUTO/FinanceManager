// Credit card statement preview: a modal to edit one line before importing.
// Nothing is saved here; the edits go into the row's hidden fields and are sent with the import.
import { openDialog, parseMoney, setMoney, wireDialog, wireMoneyInputs } from './core.js';
import { dateBR, formatMoney } from './format.js';

const dialog = document.getElementById('row-dialog');

if (dialog) {
  const form = dialog.querySelector('form');
  const error = dialog.querySelector('[data-row-error]');
  const field = (i, key) => document.querySelector(`[name="${key}_${i}"]`);
  let current = null;

  wireDialog(dialog);
  wireMoneyInputs(dialog);

  function open(link) {
    current = link.dataset.editRow;
    error.textContent = '';
    form.elements.name.value = field(current, 'name').value;
    form.elements.note.value = field(current, 'note').value;
    form.elements.date.value = field(current, 'date').value;
    setMoney(form.elements.amount, field(current, 'amount').value);
    const rowSelect = field(current, 'cat');
    const select = form.elements.category;
    select.replaceChildren(...Array.from(rowSelect.options, (o) => new Option(o.text, o.value)));
    select.value = rowSelect.value;
    dialog.querySelector('[data-original]').textContent = `Na fatura: ${link.dataset.description}`;
    openDialog(dialog, '#row-name');
  }

  document.querySelectorAll('[data-edit-row]').forEach((link) => {
    link.addEventListener('click', (event) => { event.preventDefault(); open(link); });
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const amount = Number(parseMoney(form.elements.amount.value));
    if (!(amount > 0) || Math.round(amount * 100) !== Math.round(amount * 100 * 1000) / 1000) {
      error.textContent = 'Informe um valor maior que zero, com no máximo 2 casas decimais.';
      return;
    }
    if (!form.elements.date.value) {
      error.textContent = 'Informe a data.';
      return;
    }
    const row = document.querySelector(`tr[data-row="${current}"]`);
    const link = row.querySelector('[data-edit-row]');
    const name = form.elements.name.value.trim() || field(current, 'name').defaultValue;
    const note = form.elements.note.value.trim();
    field(current, 'name').value = name;
    field(current, 'note').value = note;
    field(current, 'date').value = form.elements.date.value;
    field(current, 'amount').value = amount.toFixed(2);
    field(current, 'cat').value = form.elements.category.value;
    link.textContent = name;
    row.querySelector('[data-show-date]').textContent = dateBR(form.elements.date.value);
    row.querySelector('[data-show-amount]').textContent = formatMoney(amount);
    const noteLine = row.querySelector('[data-show-note]');
    noteLine.textContent = note ? `Observação: ${note}` : '';
    noteLine.hidden = !note;
    dialog.close();
  });
}
