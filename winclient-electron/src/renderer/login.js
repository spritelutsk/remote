'use strict';

/**
 * Окно входа. Если с прошлого запуска остался токен, main тихо проверяет его
 * через /api/currentUser и сам открывает список устройств — форму пользователь
 * в этом случае даже не увидит.
 */

const el = {
  form: document.getElementById('form'),
  server: document.getElementById('server'),
  username: document.getElementById('username'),
  password: document.getElementById('password'),
  remember: document.getElementById('remember'),
  error: document.getElementById('error'),
  status: document.getElementById('status'),
  submit: document.getElementById('submit'),
};

function showError(message) {
  el.error.textContent = message;
  el.error.hidden = false;
}

function setBusy(busy, status) {
  el.submit.disabled = busy;
  el.server.disabled = busy;
  el.username.disabled = busy;
  el.password.disabled = busy;
  el.status.textContent = status;
}

function focusFirstEmpty() {
  if (!el.username.value) el.username.focus();
  else el.password.focus();
}

async function init() {
  const settings = await window.portal.getSettings();
  if (settings.ok) {
    el.server.value = settings.data.serverUrl;
    el.username.value = settings.data.username;
    el.remember.checked = settings.data.rememberMe;
  }

  setBusy(true, 'Проверяем сохранённый вход…');

  const stored = await window.portal.tryStoredLogin();
  // Успех — main уже открыл список устройств и закрывает это окно.
  if (stored.ok && stored.data) return;

  setBusy(false, '');
  focusFirstEmpty();
}

el.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  el.error.hidden = true;

  if (!el.username.value.trim() || !el.password.value) {
    showError('Введите логин и пароль.');
    return;
  }

  setBusy(true, 'Подключаемся к серверу…');

  const result = await window.portal.login({
    serverUrl: el.server.value.trim(),
    username: el.username.value.trim(),
    password: el.password.value,
    rememberMe: el.remember.checked,
  });

  if (result.ok) return; // main открывает список и закрывает это окно

  setBusy(false, '');
  showError(result.error);
});

init();
