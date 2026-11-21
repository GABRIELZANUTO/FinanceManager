// Sign-up form: per-field inline checks and a debounced username availability lookup.
import { api, debounce } from './core.js';
import { passwordProblem, setHint } from './password.js';

const form = document.querySelector('form[data-validate="register"]');
if (form) {
  const el = (id) => document.getElementById(id);
  const hintOf = (input) => input.closest('.field').querySelector('[data-hint]');
  const touched = new Set();
  let usernameState = { value: '', ok: null };

  const checks = {
    id_full_name(input) {
      if (!input.value.trim()) return 'Informe seu nome. Uma palavra já basta.';
      return '';
    },
    id_username(input) {
      const value = input.value.trim();
      if (!value) return 'Escolha um nome de usuário.';
      if (value.length > 150) return 'Use no máximo 150 caracteres.';
      if (!/^[\w.@+-]+$/.test(value)) return 'Use apenas letras, números e @ . + - _';
      if (usernameState.value === value.toLowerCase() && usernameState.ok === false) return 'Esse nome de usuário já está em uso.';
      return '';
    },
    id_email(input) {
      const value = input.value.trim();
      if (!value) return 'Informe seu e-mail.';
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return 'Isso não parece um endereço de e-mail.';
      return '';
    },
    id_password1: (input) => (input.value ? passwordProblem(input) : 'Escolha uma senha.'),
    id_password2: (input) => (input.value !== el('id_password1').value ? 'As duas senhas ainda não são iguais.' : (input.value ? '' : 'Repita a senha.')),
  };

  function show(input, quiet = false) {
    const problem = checks[input.id](input);
    if (input.id === 'id_password1' || input.id === 'id_password2') return problem; // password.js renders these
    if (!touched.has(input.id) && !quiet) return problem;
    const ok = !problem && input.value.trim() !== '';
    setHint(hintOf(input), problem || (ok ? (input.id === 'id_username' && usernameState.ok ? 'Nome de usuário disponível.' : 'Tudo certo.') : ''), ok);
    if (problem) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    return problem;
  }

  const lookup = debounce(async (input) => {
    const value = input.value.trim().toLowerCase();
    if (!value || !/^[\w.@+-]+$/.test(value)) return;
    try {
      const data = await api(form.dataset.availabilityUrl, { query: { u: value } });
      usernameState = { value, ok: data.available };
      if (input.value.trim().toLowerCase() === value) show(input, true);
    } catch (error) {
      // availability is a convenience; the server decides on submit
    }
  }, 450);

  for (const id of ['id_full_name', 'id_username', 'id_email']) {
    const input = el(id);
    input.addEventListener('input', () => {
      touched.add(id);
      show(input);
      if (id === 'id_username') lookup(input);
    });
    input.addEventListener('blur', () => { touched.add(id); show(input); });
  }

  form.addEventListener('submit', (event) => {
    const order = ['id_full_name', 'id_username', 'id_email', 'id_password1', 'id_password2'];
    for (const id of order) {
      const input = el(id);
      touched.add(id);
      if (show(input, true)) {
        event.preventDefault();
        if (id.startsWith('id_password')) input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
        return;
      }
    }
  });
}
