'use strict';

/**
 * Список устройств: рендер таблицы, поиск, фильтры, сортировка и
 * автообновление. Сеть целиком в main-процессе, сюда приходят уже плоские
 * строки — см. обработчик `devices:list`.
 */

const el = {
  host: document.getElementById('host'),
  user: document.getElementById('user'),
  logout: document.getElementById('logout'),
  search: document.getElementById('search'),
  group: document.getElementById('group'),
  onlineOnly: document.getElementById('online-only'),
  refresh: document.getElementById('refresh'),
  rows: document.getElementById('rows'),
  empty: document.getElementById('empty'),
  status: document.getElementById('status'),
};

const state = {
  peers: [],
  selectedId: null,
  sortKey: 'deviceName',
  sortAsc: true,
  refreshing: false,
  timer: null,
};

// ------------------------------------------------------------- отрисовка ----

function visiblePeers() {
  const needle = el.search.value.trim().toLowerCase();
  const group = el.group.value;
  const onlineOnly = el.onlineOnly.checked;

  const rows = state.peers.filter((p) => {
    if (onlineOnly && !p.online) return false;
    if (group && p.group !== group) return false;
    if (!needle) return true;

    return [p.id, p.deviceName, p.os, p.localUser, p.owner, p.group, p.note]
      .some((value) => String(value).toLowerCase().includes(needle));
  });

  const dir = state.sortAsc ? 1 : -1;
  rows.sort((a, b) =>
    String(a[state.sortKey]).localeCompare(String(b[state.sortKey]), 'ru') * dir);

  return rows;
}

function render() {
  const rows = visiblePeers();

  el.rows.replaceChildren();
  el.empty.hidden = rows.length > 0;

  for (const peer of rows) {
    const tr = document.createElement('tr');
    tr.dataset.id = peer.id;
    if (peer.id === state.selectedId) tr.classList.add('selected');

    tr.append(
      cell(dot(peer.online)),
      cell(peer.deviceName),
      cell(peer.id, 'mono'),
      cell(peer.os),
      cell(peer.localUser),
      cell(peer.owner),
      cell(peer.group),
      cell(peer.note),
      actionCell(peer),
    );

    tr.addEventListener('click', () => select(peer.id));
    tr.addEventListener('dblclick', () => connect(peer));
    tr.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      select(peer.id);
      openMenu(event.clientX, event.clientY, peer);
    });

    el.rows.append(tr);
  }
}

function cell(content, className) {
  const td = document.createElement('td');
  if (className) td.className = className;

  // Текст только через textContent: имена и заметки приходят с сервера и
  // вставлять их как HTML нельзя.
  if (content instanceof Node) td.append(content);
  else td.textContent = content || '';

  return td;
}

function dot(online) {
  const span = document.createElement('span');
  span.className = online ? 'dot on' : 'dot';
  span.title = online ? 'В сети' : 'Не в сети';
  return span;
}

function actionCell(peer) {
  const td = document.createElement('td');
  td.className = 'actions';

  const button = document.createElement('button');
  button.textContent = 'Подключиться';
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    connect(peer);
  });

  td.append(button);
  return td;
}

function select(id) {
  state.selectedId = id;
  for (const tr of el.rows.children) tr.classList.toggle('selected', tr.dataset.id === id);
}

// --------------------------------------------------------- контекстное меню -

let menuEl = null;

function closeMenu() {
  if (!menuEl) return;
  menuEl.remove();
  menuEl = null;
}

function openMenu(x, y, peer) {
  closeMenu();

  menuEl = document.createElement('div');
  menuEl.className = 'menu';

  const add = (label, action) => {
    const button = document.createElement('button');
    button.textContent = label;
    button.addEventListener('click', () => {
      closeMenu();
      action();
    });
    menuEl.append(button);
  };

  add('Подключиться', () => connect(peer));
  add('Копировать ID', async () => {
    await window.portal.copyId(peer.id);
    el.status.textContent = `ID ${peer.id} скопирован в буфер обмена.`;
  });
  menuEl.append(document.createElement('hr'));
  add('Открыть в браузере', () => window.portal.openInBrowser(peer.id));

  document.body.append(menuEl);

  // Меню у нижнего или правого края должно раскрываться внутрь окна.
  const box = menuEl.getBoundingClientRect();
  menuEl.style.left = `${Math.min(x, window.innerWidth - box.width - 4)}px`;
  menuEl.style.top = `${Math.min(y, window.innerHeight - box.height - 4)}px`;
}

window.addEventListener('click', closeMenu);
window.addEventListener('resize', closeMenu);
window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMenu();
});

// ----------------------------------------------------------------- данные ---

async function connect(peer) {
  if (!peer.online) {
    el.status.textContent =
      `Устройство «${peer.deviceName}» не в сети — пробуем подключиться всё равно.`;
  }

  await window.portal.connect(peer);
}

async function loadGroups() {
  const result = await window.portal.listGroups();
  if (!result.ok) return;

  const current = el.group.value;
  el.group.replaceChildren();

  const all = document.createElement('option');
  all.value = '';
  all.textContent = 'Все папки';
  el.group.append(all);

  for (const name of result.data) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    el.group.append(option);
  }

  el.group.value = current;
}

async function refresh({ silent }) {
  if (state.refreshing) return;
  state.refreshing = true;

  if (!silent) el.status.textContent = 'Загружаем список устройств…';

  const result = await window.portal.listPeers();

  if (result.ok) {
    state.peers = result.data;
    render();

    const online = state.peers.filter((p) => p.online).length;
    const time = new Date().toLocaleTimeString('ru-RU');
    el.status.textContent =
      `Устройств: ${state.peers.length} · в сети: ${online} · обновлено в ${time}`;
  } else if (!result.expired) {
    // Обрыв связи не должен глушить автообновление: следующая попытка придёт
    // по таймеру. Протухший токен обрабатывается отдельно, в onAuthExpired.
    el.status.textContent = `Не удалось обновить список: ${result.error}`;
  }

  state.refreshing = false;
}

// ------------------------------------------------------------- интерфейс ----

el.search.addEventListener('input', render);
el.group.addEventListener('change', render);

el.onlineOnly.addEventListener('change', () => {
  window.portal.setOnlineOnly(el.onlineOnly.checked);
  render();
});

el.refresh.addEventListener('click', () => refresh({ silent: false }));
el.logout.addEventListener('click', () => window.portal.logout());

for (const th of document.querySelectorAll('th[data-sort]')) {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    state.sortAsc = state.sortKey === key ? !state.sortAsc : true;
    state.sortKey = key;
    render();
  });
}

window.portal.onAuthExpired(() => {
  if (state.timer) clearInterval(state.timer);
  el.status.textContent = 'Сессия на сервере истекла — войдите заново.';
});

async function init() {
  const session = await window.portal.getSession();

  if (session.ok) {
    el.host.textContent = session.data.host;
    el.onlineOnly.checked = session.data.onlineOnly;

    if (session.data.user) {
      el.user.textContent = session.data.user.isAdmin
        ? `${session.data.user.title} · администратор`
        : session.data.user.title;
    }
  }

  await loadGroups();
  await refresh({ silent: false });

  // Присутствие на сервере обновляется примерно раз в 15 секунд (heartbeat
  // клиентов), поэтому и опрос идёт с тем же шагом — чаще смысла нет.
  const seconds = (session.ok && session.data.autoRefreshSeconds) || 15;
  state.timer = setInterval(() => refresh({ silent: true }), seconds * 1000);
}

init();
