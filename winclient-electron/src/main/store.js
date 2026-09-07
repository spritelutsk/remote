'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { app, safeStorage } = require('electron');

/**
 * Настройки и сохранённый токен в папке userData.
 *
 * Токен лежит отдельным файлом и шифруется через safeStorage — на Windows это
 * DPAPI, то есть ключ текущего пользователя ОС. В settings.json его класть
 * нельзя: это обычный читаемый JSON, а токен клиентского API открывает весь
 * список устройств.
 */

const SETTINGS_FILE = 'settings.json';
const TOKEN_FILE = 'token.dat';

const DEFAULTS = {
  serverUrl: 'https://rd.spritelutsk.duckdns.org',
  username: '',
  rememberMe: true,
  deviceUuid: '',
  autoRefreshSeconds: 15,
  onlineOnly: false,
};

const filePath = (name) => path.join(app.getPath('userData'), name);

function randomUuid() {
  return require('node:crypto').randomUUID().replace(/-/g, '');
}

function loadSettings() {
  let stored = {};

  try {
    const raw = fs.readFileSync(filePath(SETTINGS_FILE), 'utf8');
    stored = JSON.parse(raw);
  } catch {
    // Файла нет или он битый — работаем на значениях по умолчанию.
  }

  const settings = { ...DEFAULTS, ...stored };

  if (!settings.deviceUuid) settings.deviceUuid = randomUuid();
  if (!(settings.autoRefreshSeconds >= 5)) settings.autoRefreshSeconds = 15;

  return settings;
}

function saveSettings(settings) {
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(filePath(SETTINGS_FILE), JSON.stringify(settings, null, 2), 'utf8');
  } catch {
    // Не сохранилось — приложение работает, просто забудет выбор.
  }
}

function saveToken(token) {
  try {
    // Без поддержки шифрования в системе токен на диск не пишем вовсе:
    // класть его открытым текстом хуже, чем спросить пароль ещё раз.
    if (!safeStorage.isEncryptionAvailable()) return;

    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(filePath(TOKEN_FILE), safeStorage.encryptString(token));
  } catch {
    // Игнорируем: следующий запуск просто спросит логин.
  }
}

function loadToken() {
  try {
    if (!safeStorage.isEncryptionAvailable()) return null;

    return safeStorage.decryptString(fs.readFileSync(filePath(TOKEN_FILE)));
  } catch {
    // Файл перенесли с другой машины или профиль сменился — расшифровать
    // нельзя, и это нормально: просто попросим войти заново.
    return null;
  }
}

function clearToken() {
  try {
    fs.rmSync(filePath(TOKEN_FILE), { force: true });
  } catch {
    // Нечего чистить.
  }
}

const deviceId = (settings) => `win-${settings.deviceUuid}`;

module.exports = { loadSettings, saveSettings, saveToken, loadToken, clearToken, deviceId };
