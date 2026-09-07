'use strict';

const path = require('node:path');
const { app, BrowserWindow, ipcMain, session, shell, clipboard, dialog, Menu } = require('electron');

const { CortenDeskApi, ApiError } = require('./api');
const store = require('./store');

/**
 * Главный процесс: владеет состоянием (настройки, токен, экземпляр API),
 * окнами и всеми сетевыми запросами.
 *
 * Про две авторизации, которые тут сходятся:
 *   - список устройств берётся по Bearer клиентского API;
 *   - страница /webclient закрыта обычной сессией консоли (middleware `auth`),
 *     и Bearer в неё не подставить.
 * Поэтому окно сеанса живёт в отдельном persist-разделе: cookie консоли там
 * сохраняется между запусками, и вход спрашивается только в первый раз.
 */

/** Раздел сессии для окон веб-клиента — именно он хранит cookie консоли. */
const SESSION_PARTITION = 'persist:cortendesk';

/** Сколько раз возвращаться на /webclient, если сервер увёл на другую страницу. */
const MAX_RESUME_ATTEMPTS = 3;

const state = {
  settings: null,
  api: null,
  user: null,
  loginWindow: null,
  devicesWindow: null,
};

// ---------------------------------------------------------------- окна ------

function createLoginWindow() {
  const win = new BrowserWindow({
    width: 420,
    height: 520,
    resizable: false,
    title: 'Вход — CortenDesk Remote',
    backgroundColor: '#f4f6fa',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, '..', 'renderer', 'login.html'));
  state.loginWindow = win;
  win.on('closed', () => {
    state.loginWindow = null;
  });

  return win;
}

function createDevicesWindow() {
  const win = new BrowserWindow({
    width: 1140,
    height: 680,
    minWidth: 820,
    minHeight: 480,
    title: 'CortenDesk Remote',
    backgroundColor: '#f4f6fa',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, '..', 'renderer', 'devices.html'));
  state.devicesWindow = win;
  win.on('closed', () => {
    state.devicesWindow = null;
  });

  return win;
}

/**
 * Окно сеанса: нативный веб-клиент CortenDesk.
 *
 * Разбор навигации повторяет логику WPF-версии. Порядок ветвлений важен:
 * сначала успех, потом чужой хост (портал-провайдер SSO — туда лезть нельзя),
 * потом формы входа консоли, и только оставшееся считается «вошли, но не
 * туда» — это редирект на главную после логина, откуда мы возвращаемся сами.
 */
function createSessionWindow(peer) {
  const target = state.api.webClientUrl(peer.id);
  const consoleHost = state.api.host;
  let attempts = 0;

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    title: `${peer.deviceName} (${peer.id}) — CortenDesk Remote`,
    backgroundColor: '#f4f6fa',
    autoHideMenuBar: true,
    webPreferences: {
      // Отдельный persist-раздел: тут копится cookie сессии консоли.
      partition: SESSION_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      // Веб-клиенту нужны WebCodecs и WebSocket, но не доступ к системе.
      sandbox: true,
    },
  });

  // Куда этому окну позволено ходить.
  //
  // Просто «только origin консоли» не годится: вход идёт через SSO на портал,
  // это другой хост, и такой список сломал бы логин. Правило поэтому такое:
  //   * схема только http/https — это отсекает window.open('file:///...'),
  //     ради которого находка и заведена;
  //   * origin консоли разрешён всегда;
  //   * чужой origin разрешается, только если мы попали на него СЕРВЕРНЫМ
  //     редиректом с уже разрешённого — это и есть нога SSO. Скрипт на
  //     странице так сделать не может: присваивание location приходит как
  //     will-navigate, а не will-redirect.
  const allowedOrigins = new Set([new URL(state.api.base).origin]);

  const originOf = (url) => {
    try {
      const parsed = new URL(url);
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.origin : null;
    } catch {
      return null;   // не URL вовсе — точно не наш
    }
  };

  const isAllowed = (url) => {
    const origin = originOf(url);
    return origin !== null && allowedOrigins.has(origin);
  };

  // Всплывающие окна веб-клиента остаются внутри этого же окна, иначе они
  // уходят в системный браузер, где нашей сессии нет. Раньше сюда попадал
  // любой url со страницы — включая file:// и сайт атакующего.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowed(url)) win.loadURL(url);
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (event, url) => {
    if (!isAllowed(url)) event.preventDefault();
  });

  win.webContents.on('will-redirect', (event, url) => {
    const target = originOf(url);
    if (target === null) {
      event.preventDefault();
      return;
    }

    // Редирект пришёл с разрешённой страницы — значит это шаг SSO, и его
    // цель тоже становится разрешённой (обратный редирект на консоль).
    if (isAllowed(win.webContents.getURL())) {
      allowedOrigins.add(target);
      return;
    }

    if (!allowedOrigins.has(target)) event.preventDefault();
  });

  win.webContents.on('did-finish-load', () => {
    const current = win.webContents.getURL();
    if (!current) return;

    let url;
    try {
      url = new URL(current);
    } catch {
      return;
    }

    if (url.pathname.toLowerCase() === '/webclient') {
      attempts = 0;
      return;
    }

    if (url.host !== consoleHost) return; // внешний провайдер SSO — не мешаем
    if (isAuthPage(url.pathname)) return; // форма входа — ждём пользователя

    if (attempts < MAX_RESUME_ATTEMPTS) {
      attempts += 1;
      win.loadURL(target);
      return;
    }

    dialog.showMessageBox(win, {
      type: 'warning',
      title: 'CortenDesk Remote',
      message: 'Не удалось открыть веб-клиент',
      detail:
        'Сервер увёл нас со страницы сеанса. Проверьте, что у вашей учётной '
        + `записи есть доступ к устройству ${peer.id}.`,
    });
  });

  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;

    if (input.key === 'F11') {
      win.setFullScreen(!win.isFullScreen());
      event.preventDefault();
    } else if (input.key === 'Escape' && win.isFullScreen()) {
      win.setFullScreen(false);
      event.preventDefault();
    }
  });

  win.loadURL(target);
  return win;
}

function isAuthPage(pathname) {
  const p = pathname.toLowerCase();
  return (
    p.startsWith('/login')
    || p.startsWith('/oidc')
    || p.startsWith('/invite')
    || p.startsWith('/forgot-password')
    || p.startsWith('/reset-password')
  );
}

// ---------------------------------------------------------------- IPC -------

/** Оборачивает обработчик: ApiError превращается в {ok:false, error}. */
function handle(channel, fn) {
  ipcMain.handle(channel, async (event, ...args) => {
    try {
      return { ok: true, data: await fn(...args) };
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.expired) onAuthExpired();
        return { ok: false, error: err.message, expired: err.expired };
      }

      return { ok: false, error: err.message || String(err) };
    }
  });
}

function onAuthExpired() {
  store.clearToken();
  if (state.devicesWindow) state.devicesWindow.webContents.send('auth:expired');
}

function afterLogin(api, user) {
  state.api = api;
  state.user = user;

  createDevicesWindow();
  if (state.loginWindow) state.loginWindow.close();
}

handle('settings:get', async () => ({
  serverUrl: state.settings.serverUrl,
  username: state.settings.username,
  rememberMe: state.settings.rememberMe,
  onlineOnly: state.settings.onlineOnly,
  autoRefreshSeconds: state.settings.autoRefreshSeconds,
}));

handle('auth:try-stored', async () => {
  const token = store.loadToken();
  if (!token) return false;

  const api = new CortenDeskApi(state.settings.serverUrl, token);

  try {
    const user = await api.currentUser();
    afterLogin(api, user);
    return true;
  } catch {
    // Любая осечка на сохранённом токене — просто показываем форму.
    store.clearToken();
    return false;
  }
});

handle('auth:login', async ({ serverUrl, username, password, rememberMe }) => {
  const api = new CortenDeskApi(serverUrl);

  const user = await api.login(
    username,
    password,
    store.deviceId(state.settings),
    state.settings.deviceUuid,
  );

  state.settings.serverUrl = api.base;
  state.settings.username = username;
  state.settings.rememberMe = Boolean(rememberMe);
  store.saveSettings(state.settings);

  if (state.settings.rememberMe) store.saveToken(api.token);
  else store.clearToken();

  afterLogin(api, user);
  return true;
});

handle('auth:logout', async () => {
  store.clearToken();
  if (state.api) await state.api.logout();

  state.api = null;
  state.user = null;

  createLoginWindow();
  if (state.devicesWindow) state.devicesWindow.close();

  return true;
});

handle('session:get', async () => ({
  host: state.api ? state.api.host : '',
  user: state.user
    ? {
        title: state.user.display_name || state.user.name || '',
        isAdmin: Boolean(state.user.is_admin),
      }
    : null,
  onlineOnly: state.settings.onlineOnly,
  autoRefreshSeconds: state.settings.autoRefreshSeconds,
}));

handle('devices:list', async () => {
  const peers = await state.api.peers();

  return peers.map((p) => ({
    id: p.id || '',
    deviceName: (p.info && p.info.device_name) || p.id || '',
    os: (p.info && p.info.os) || '',
    localUser: (p.info && p.info.username) || '',
    owner: p.user_name || p.user || '',
    group: p.device_group_name || '',
    note: p.note || '',
    online: p.status === 1,
  }));
});

handle('devices:groups', async () => {
  try {
    return await state.api.deviceGroups();
  } catch (err) {
    // Прав на список папок может не быть — это не повод ломать весь экран.
    if (err instanceof ApiError && err.expired) throw err;
    return [];
  }
});

handle('settings:online-only', async (value) => {
  state.settings.onlineOnly = Boolean(value);
  store.saveSettings(state.settings);
  return true;
});

handle('session:open', async (peer) => {
  createSessionWindow(peer);
  return true;
});

handle('session:browser', async (peerId) => {
  await shell.openExternal(state.api.webClientUrl(peerId));
  return true;
});

handle('clipboard:id', async (peerId) => {
  clipboard.writeText(String(peerId));
  return true;
});

// -------------------------------------------------------------- запуск ------

// Второй экземпляр только поднимает уже открытое окно: два процесса на один
// профиль подрались бы за файл настроек и за раздел сессии.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = state.devicesWindow || state.loginWindow;
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);

    state.settings = store.loadSettings();
    store.saveSettings(state.settings); // фиксируем сгенерированный deviceUuid

    // Веб-клиент запрашивает разрешения (буфер обмена, полный экран). Всё, что
    // не нужно для сеанса, отклоняем; список намеренно короткий.
    session
      .fromPartition(SESSION_PARTITION)
      .setPermissionRequestHandler((webContents, permission, callback) => {
        callback(['clipboard-read', 'clipboard-sanitized-write', 'fullscreen'].includes(permission));
      });

    createLoginWindow();
  });

  app.on('window-all-closed', () => {
    app.quit();
  });
}
