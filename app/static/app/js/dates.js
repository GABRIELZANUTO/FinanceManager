// Brazilian date fields (dd/mm/aaaa) everywhere, whatever the language of the phone or browser.
//
// The browser draws <input type="date"> in the language of the device (mm/dd/yyyy on a phone in
// English). Each one is kept (hidden, it still holds the ISO value the server reads and every
// script sets) and gets a visible text field with a dd/mm/aaaa mask plus a calendar button.
import { icon } from './core.js';

const VALUE = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');

export function isoToBR(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  return m ? `${m[3]}/${m[2]}/${m[1]}` : '';
}

export function brToISO(text) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(text || '');
  if (!m) return null;
  const [day, month, year] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(year, month - 1, day);
  if (year < 1900 || date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  return `${m[3]}-${m[2]}-${m[1]}`;
}

// "1102202" -> "11/02/202": digits only, slashes added as you type.
export function maskBR(text) {
  const digits = (text || '').replace(/\D/g, '').slice(0, 8);
  if (digits.length <= 2) return digits;
  if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
  return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4)}`;
}

function enhance(input) {
  if (input.dataset.brDate) return;
  input.dataset.brDate = '1';
  const text = document.createElement('input');
  text.type = 'text';
  text.inputMode = 'numeric';
  text.placeholder = 'dd/mm/aaaa';
  text.maxLength = 10;
  text.autocomplete = 'off';
  text.className = 'date-text';
  if (input.id) {           // labels (for=...) and focus helpers now reach the visible field
    text.id = input.id;
    input.id = `${input.id}-iso`;
  }
  if (input.required) text.required = true;
  text.disabled = input.disabled;
  for (const attr of ['aria-label', 'aria-describedby']) if (input.hasAttribute(attr)) text.setAttribute(attr, input.getAttribute(attr));
  input.tabIndex = -1;
  input.setAttribute('aria-hidden', 'true');
  input.classList.add('date-native');

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'icon-btn date-pick';
  button.setAttribute('aria-label', 'Abrir calendário');
  button.append(icon('calendar'));

  const wrap = document.createElement('span');
  wrap.className = 'date-field';
  input.replaceWith(wrap);
  wrap.append(text, button, input);

  const show = () => { text.value = isoToBR(VALUE.get.call(input)); text.removeAttribute('aria-invalid'); };
  // Scripts set input.value = '2026-11-20': keep the visible field in step.
  Object.defineProperty(input, 'value', {
    configurable: true,
    get() { return VALUE.get.call(this); },
    set(v) { VALUE.set.call(this, v); show(); },
  });
  show();

  const emit = () => {
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  text.addEventListener('input', () => {
    const masked = maskBR(text.value);
    if (masked !== text.value) text.value = masked;
    const iso = brToISO(masked);
    if (iso || masked === '') {
      VALUE.set.call(input, iso || '');
      text.removeAttribute('aria-invalid');
      emit();
    }
  });
  text.addEventListener('blur', () => {
    if (text.value && !brToISO(text.value)) {
      text.setAttribute('aria-invalid', 'true');   // keep what was typed so it can be fixed
      if (VALUE.get.call(input)) { VALUE.set.call(input, ''); emit(); }
    }
  });
  input.addEventListener('change', () => { if (isoToBR(VALUE.get.call(input)) !== text.value) show(); });
  button.addEventListener('click', () => {
    try {
      input.showPicker();
    } catch (e) {
      text.focus();          // old browsers: typing is always possible
    }
  });
  if (input.form) input.form.addEventListener('reset', () => setTimeout(show));
  // Disabling the hidden field (some forms do) disables the visible one too.
  new MutationObserver(() => { text.disabled = input.disabled; button.disabled = input.disabled; })
    .observe(input, { attributes: true, attributeFilter: ['disabled'] });
}

export function enhanceDates(root = document) {
  for (const input of root.querySelectorAll('input[type="date"]')) enhance(input);
}

enhanceDates();
new MutationObserver((changes) => {
  for (const change of changes) {
    for (const node of change.addedNodes) {
      if (node.nodeType !== 1) continue;
      if (node.matches('input[type="date"]')) enhance(node);
      else enhanceDates(node);
    }
  }
}).observe(document.body, { childList: true, subtree: true });
