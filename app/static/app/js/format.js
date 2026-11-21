// Number and currency formatting. Mirrors app/formatting.py.
const SEPARATORS = {
  intl: [',', '.'],
  indian: [',', '.'],
  eu: ['.', ','],
  space: [' ', ','],
};

function readConfig() {
  const node = document.getElementById('money-config');
  if (node) {
    try { return JSON.parse(node.textContent); } catch (e) { /* fall through */ }
  }
  return { symbol: '', code: '', format: 'intl' };
}

export const config = readConfig();

function group(digits, indian) {
  if (digits.length <= 3) return [digits];
  const parts = [];
  let rest = digits;
  if (indian) {
    parts.unshift(rest.slice(-3));
    rest = rest.slice(0, -3);
    while (rest) { parts.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
  } else {
    while (rest) { parts.unshift(rest.slice(-3)); rest = rest.slice(0, -3); }
  }
  return parts;
}

export function formatNumber(value, format = config.format, places = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '';
  const cents = Math.round(Math.abs(number) * 10 ** places);
  const text = String(cents).padStart(places + 1, '0');
  const whole = text.slice(0, text.length - places);
  const frac = text.slice(text.length - places);
  const [groupSep, decimalSep] = SEPARATORS[format] || SEPARATORS.intl;
  const body = group(whole, format === 'indian').join(groupSep) + (places ? decimalSep + frac : '');
  return (number < 0 && cents > 0 ? '-' : '') + body;
}

export function formatMoney(value, withSymbol = true) {
  const text = formatNumber(value);
  if (!withSymbol || !config.symbol) return text;
  const negative = text.startsWith('-');
  const body = negative ? text.slice(1) : text;
  // "R$ 1.234,56", "Rs 1,500.00", "$1,500.00": a space when the symbol contains letters
  const joiner = /[A-Za-z]/.test(config.symbol) ? ' ' : '';
  return (negative ? '-' : '') + config.symbol + joiner + body;
}

// Short axis labels: 1,2 mil; 3,4 mi; 2 bi.
export function compact(value) {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  const decimalSep = (SEPARATORS[config.format] || SEPARATORS.intl)[1];
  const trim = (n) => String(Math.round(n * 10) / 10).replace('.', decimalSep);
  if (abs >= 1e9) return sign + trim(abs / 1e9) + ' bi';
  if (abs >= 1e6) return sign + trim(abs / 1e6) + ' mi';
  if (abs >= 1e3) return sign + trim(abs / 1e3) + ' mil';
  return sign + trim(abs);
}

const MONTHS = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez'];
const MONTHS_LONG = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];

export function monthShort(ym) { return MONTHS[Number(ym.slice(5, 7)) - 1]; }
export function monthLong(ym) {
  const name = MONTHS_LONG[Number(ym.slice(5, 7)) - 1];
  return `${name.charAt(0).toUpperCase()}${name.slice(1)} de ${ym.slice(0, 4)}`;
}
export function dayLabel(iso) { return `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`; }
/** '2026-11-04' -> '04/11/2026' (empty and non-ISO values are returned as they are). */
export function dateBR(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}/${m[2]}/${m[1]}` : (iso || '');
}
