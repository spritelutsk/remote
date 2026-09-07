'use strict';

/**
 * Клиент REST API CortenDesk. Живёт в main-процессе намеренно: рендерер ходил
 * бы на чужой origin, а консоль не отдаёт CORS-заголовки произвольным
 * источникам — из окна такие запросы просто не выпустит браузерный движок.
 *
 * Две особенности контракта, из-за которых мало проверить HTTP-статус:
 *   - ошибки приходят как {"error": "..."} даже с кодом 200 (так устроен
 *     клиентский протокол RustDesk, стоковый клиент статус не смотрит);
 *   - списочные ответы страничные: {"total": N, "data": [...]}, pageSize <= 500.
 */

const PAGE_SIZE = 200;
const MAX_PAGES = 200; // предохранитель от бесконечного цикла
const TIMEOUT_MS = 30000;

/** Хосты, которым http разрешён: локальная отладка. */
const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

/** Ошибка, которую можно показать пользователю как есть. */
class ApiError extends Error {
  constructor(message, { expired = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.expired = expired;
  }
}

/** Токен протух или отозван — нужно переспросить логин. */
const authExpired = () =>
  new ApiError('Сессия на сервере истекла, войдите заново.', { expired: true });

/** Приводит введённый адрес к виду https://host[:port] (без хвостового слэша). */
function normalizeBase(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) throw new ApiError('Укажите адрес сервера.');

  const withScheme = trimmed.includes('://') ? trimmed : `https://${trimmed}`;

  let url;
  try {
    url = new URL(withScheme);
  } catch {
    throw new ApiError(`Некорректный адрес сервера: ${raw}`);
  }

  if (url.protocol !== 'http:' && url.protocol !== 'https:')
    throw new ApiError(`Некорректный адрес сервера: ${raw}`);

  // По http логин и пароль ушли бы открытым текстом. Схему по умолчанию мы и
  // так подставляем https, но явный http надо отклонить — кроме петли, где он
  // нужен для отладки против локального сервера.
  if (url.protocol === 'http:' && !LOOPBACK.has(url.hostname))
    throw new ApiError(
      'Подключение по http небезопасно: логин и пароль уйдут открытым текстом. '
        + 'Укажите адрес с https://',
    );

  return `${url.protocol}//${url.host}`;
}

class CortenDeskApi {
  constructor(baseUrl, token = null) {
    this.base = normalizeBase(baseUrl);
    this.token = token;
  }

  get host() {
    return new URL(this.base).host;
  }

  webClientUrl(peerId) {
    return `${this.base}/webclient?id=${encodeURIComponent(peerId)}`;
  }

  /** POST /api/login — выдаёт Bearer клиентского API. */
  async login(username, password, deviceId, deviceUuid) {
    const payload = await this.#request('POST', '/api/login', {
      auth: false,
      body: {
        username,
        password,
        id: deviceId,
        uuid: deviceUuid,
        autoLogin: false,
        type: 'account',
        verificationCode: '',
        deviceInfo: { os: 'Windows', type: 'client', name: 'CortenDesk Remote' },
      },
    });

    if (payload.error) throw new ApiError(translate(payload.error));

    if (payload.tfa_type)
      throw new ApiError(
        'Для этой учётной записи включена двухфакторная аутентификация, '
          + 'клиентский API её не поддерживает. Войдите через веб-консоль.',
      );

    if (!payload.access_token) throw new ApiError('Сервер не вернул токен доступа.');

    this.token = payload.access_token;
    return payload.user || { name: username };
  }

  /** POST /api/currentUser — тихая проверка живости сохранённого токена. */
  currentUser() {
    return this.#request('POST', '/api/currentUser');
  }

  /** GET /api/peers — устройства, видимые текущему пользователю. */
  peers() {
    return this.#allPages('/api/peers');
  }

  /** GET /api/device-group/accessible — доступные папки устройств. */
  async deviceGroups() {
    const rows = await this.#allPages('/api/device-group/accessible');

    return [...new Set(rows.map((r) => r.name).filter(Boolean))].sort((a, b) =>
      a.localeCompare(b, 'ru'),
    );
  }

  /** POST /api/logout — отзывает токен, ошибки намеренно проглатываются. */
  async logout() {
    if (!this.token) return;
    try {
      await this.#request('POST', '/api/logout');
    } catch {
      // Выход не должен падать из-за недоступного сервера.
    } finally {
      this.token = null;
    }
  }

  async #allPages(path) {
    const all = [];

    for (let page = 1; page <= MAX_PAGES; page += 1) {
      const payload = await this.#request(
        'GET',
        `${path}?current=${page}&pageSize=${PAGE_SIZE}`,
      );

      if (payload.error) throw new ApiError(translate(payload.error));

      const data = Array.isArray(payload.data) ? payload.data : [];
      all.push(...data);

      // Страница неполная или общее число уже набрано — дальше пусто.
      if (data.length < PAGE_SIZE || all.length >= (payload.total || 0)) break;
    }

    return all;
  }

  async #request(method, path, { auth = true, body = null } = {}) {
    if (auth && !this.token) throw authExpired();

    const headers = { Accept: 'application/json' };
    if (auth) headers.Authorization = `Bearer ${this.token}`;
    if (body) headers['Content-Type'] = 'application/json';

    let response;
    try {
      response = await fetch(this.base + path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch (err) {
      if (err && err.name === 'TimeoutError')
        throw new ApiError('Сервер не ответил вовремя.');

      throw new ApiError(`Не удалось связаться с сервером: ${err.message}`);
    }

    if (response.status === 401 || response.status === 403) throw authExpired();

    if (response.status === 429)
      throw new ApiError('Слишком много запросов к серверу, попробуйте через минуту.');

    const text = await response.text();

    if (!text.trim()) {
      if (!response.ok) throw new ApiError(`Сервер ответил ${response.status}.`);
      throw new ApiError('Сервер вернул пустой ответ.');
    }

    try {
      return JSON.parse(text);
    } catch {
      // Типичный случай: вместо JSON прилетела HTML-страница логина или
      // ошибка прокси — показывать её сырой бессмысленно.
      throw new ApiError(
        response.ok
          ? 'Сервер вернул не JSON. Проверьте адрес: он должен указывать на консоль CortenDesk.'
          : `Сервер ответил ${response.status} и вернул не JSON.`,
      );
    }
  }
}

/** Сообщения клиентского API приходят по-английски и коротко. */
function translate(error) {
  return error === 'Wrong credentials' ? 'Неверный логин или пароль.' : error;
}

module.exports = { CortenDeskApi, ApiError, normalizeBase };
