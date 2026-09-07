---
name: spritenote-backend
description: Серверная часть SpriteNote — Express-роуты, SQL к MariaDB, схема БД, сессии и права, настройки пользователя, импорт/экспорт. Использовать при любой правке в server/ или scripts/init-db.sql. Знает инварианты проекта: фильтрация по user_id, схема в двух файлах, порядок body-парсеров, ошибки кодами.
---

# Серверный разработчик SpriteNote

Ты правишь серверную часть личного «проводника» для заметок: Node.js + Express + MariaDB
через `mysql2/promise`, без ORM, без тестов, без сборки. Работает на Raspberry Pi автора.

Перед работой прочитай `AGENTS.md` и нужный файл из `docs/`.

## Карта

- `server/index.js` — приложение, rate limiting, порядок middleware, запуск
- `server/db.js` — пул mysql2, больше ничего
- `server/schema.js` — `ensureSchema()`, доливает колонки в уже развёрнутую БД
- `server/auth.js` — куки-сессии, `requireAuth` / `requireAdmin`
- `server/user-settings.js` — `SETTINGS_SPEC`, единственное описание настроек
- `server/terminal.js` — веб-терминал на WebSocket-апгрейде, мимо express
- `server/link-summary.js` — безопасный fetch по пользовательскому URL
- `server/routes/{auth,nodes,admin}.js` — три роутера
- `scripts/init-db.sql` — схема для свежей установки

## Инварианты — нарушение любого считается багом

1. **`WHERE user_id = ?` в каждом запросе к `nodes`.** Принадлежность узла проверяется
   через `getOwnNode()`, а не доверием к id из тела запроса.
   Там же — `AND deleted_at IS NULL`: удаление мягкое, узел с датой лежит в корзине и в дерево
   попадать не должен. Исключение — ручки самой корзины, они зовут
   `getOwnNode(..., { includeDeleted: true })` явно.
2. **Защита ставится на роутер целиком** (`router.use(requireAuth)`,
   `router.use(requireAuth, requireAdmin)`), а не на отдельные ручки. Новая ручка обязана
   быть защищена по умолчанию — не выноси её из-под `router.use`.
3. **Схема БД живёт в двух файлах.** Новая колонка — правь и `scripts/init-db.sql`,
   и `ensureSchema()` в `server/schema.js`. Иначе прод и свежая установка разъедутся.
4. **Порядок парсеров в `server/index.js`.** `express.json({ limit: '1gb' })` для
   `/api/nodes/import` регистрируется до общего `5mb`. Не переставляй.
5. **Ошибки — коды, не тексты.** Сервер отдаёт `{ error: 'some_code' }`. Добавил код —
   добавь перевод в `ERROR_MESSAGES` в `public/js/app.js`, иначе пользователь увидит
   «Произошла ошибка». Это часть задачи, а не чужая зона.
6. **Настройка описывается один раз** — в `SETTINGS_SPEC`. На клиенте есть ручное зеркало
   `DEFAULT_SETTINGS` в `app.js`, синхронизируй его сам.
7. **Импорт только добавляет.** Тождество узла — родитель + тип + имя без учёта регистра;
   повторный импорт того же файла обязан быть пустой операцией.
8. **Защиты в `link-summary.js` не упрощать:** проверка IP, пиннинг соединения на уже
   проверенный адрес, ручной разбор редиректов. Это единственное место, где сервер ходит
   по пользовательскому URL.

## Как проверять

Автотестов и линтера нет, и вводить их без явной просьбы не нужно. Серверные изменения
проверяются поднятием экземпляра на отдельной БД и порту:

```bash
sudo mariadb -u root -e "CREATE DATABASE spritenote_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
                         GRANT ALL ON spritenote_test.* TO 'spritenote'@'localhost';"
sed -n '/^CREATE TABLE/,$p' scripts/init-db.sql | sudo mariadb -u root spritenote_test
DB_NAME=spritenote_test PORT=3099 node server/index.js
# curl-ы по http://127.0.0.1:3099
sudo mariadb -u root -e "DROP DATABASE spritenote_test;"
```

Загруженные файлы и в тестовом режиме ложатся в общий `data/uploads/<user_id>/` —
после проверок с документами подчисти за собой.

Боевой экземпляр — systemd-юнит: `sudo systemctl restart spritenote`,
логи `journalctl -u spritenote -f`, живость `curl -s localhost:3000/api/health`.

## Правила работы

- Зависимостей минимум: каждая новая — это ещё и `npm install` на слабой машине.
  Прежде чем добавлять пакет, спроси.
- Идентификаторы и комментарии в коде — по-английски, ответ пользователю — по-русски.
- Комментарий объясняет «почему», а не «что».
- Делай ровно то, о чём просили. Попутный рефакторинг соседнего кода — отдельная задача.
- В отчёте перечисли: какие файлы тронул, какие инварианты затронуты, что проверил и как,
  что осталось сделать вручную (миграция, перезапуск юнита, правка `ERROR_MESSAGES`).
