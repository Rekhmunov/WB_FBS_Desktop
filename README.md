# AI-reviews

Сервис обработки отзывов маркетплейсов (MVP в духе Spix): ленднинг, email/password авторизация, кабинет пользователя с левым меню, админ-панель, автоподтягивание отзывов по API, категоризация, шаблоны и ручная обработка оператором.

## Что реализовано сейчас

### 1) Лендинг + auth

- главная страница `/` как лендинг с преимуществами;
- кнопки входа и регистрации;
- регистрация/авторизация по email + password (без телефона/кодов);
- выход из аккаунта.

### 2) Админ-панель

URL: `/admin` (доступ только для роли `admin`)

- просмотр пользователей;
- смена ролей (`user` / `feedback_manager` / `admin`);
- настройка AI-классификатора:
  - `rules` (встроенные правила),
  - `yandex` (Foundation Models API; API key/folder/model URI).
- SLA-метрики и мониторинг ручной очереди.
- журнал действий (синхронизация, автоответы, ручные действия операторов).

Текущая модель доступа:

- первый зарегистрированный пользователь становится `admin`;
- дальше админ может назначать роль другим пользователям;
- роль `feedback_manager` видит только вкладки **Отзывы**, **Вопросы и чаты** и **Мой профиль** (без аналитики и настроек).

### 3) Кабинет сервиса (`/app`)

Интерфейс: слева навигация, справа рабочее поле.

Разделы:

- **Отзывы**: синхронизация, фильтры, автоответ, перевод в ручную очередь, ручной ответ;
- **Вопросы и чаты**: вопросы маркетплейсов и переписки с покупателями (статусы open/waiting/closed);
- **Аналитика**: ключевые метрики (обработано, доля позитива/негатива, вопросы/чаты);
- **Настройки**: источники API + правила обработки/шаблоны.

### 4) Логика процессов

После синхронизации отзыв:

1. анализируется (`sentiment/spam/toxicity/priority`);
2. получает категорию:
   - `negative_delivery`
   - `negative_product`
   - `negative_other`
   - `positive_quality`
   - `positive_product`
   - `neutral_other`
3. для категории применяется правило:
   - `auto` -> автоответ по шаблону;
   - `manual` -> очередь оператора;
   - `ignore` -> игнор.

### 5) Интеграции WB/OZON

- поддержаны отдельные клиенты для WB и OZON с пагинацией;
- для OZON используется `client_id + api_key`;
- для WB используется `api_key`;
- можно подключать несколько кабинетов каждого маркетплейса.
- добавлена нормализация ошибок API и retry для сетевых сбоев.

Дополнительные настройки интеграции можно передать через `integration` (JSON) при создании кабинета:

- для OZON:
  - `list_path` (по умолчанию `/v1/review/list`)
  - `page_size`, `max_pages`
  - `base_payload` (словарь, добавляется в каждую POST-загрузку)
- для WB:
  - `list_path` (если нужен явный path поверх base URL)
  - `page_size`, `max_pages`
  - `skip_param`, `take_param`, `unanswered_param`, `unanswered_value`

Пример payload для `POST /api/accounts` (OZON):

```json
{
  "marketplace": "ozon",
  "account_name": "Ozon Main",
  "api_url": "https://api-seller.ozon.ru",
  "api_key": "secret",
  "client_id": "12345",
  "integration": {
    "list_path": "/v1/review/list",
    "page_size": 50,
    "max_pages": 15,
    "base_payload": {
      "sort_dir": "DESC"
    }
  }
}
```

### 6) Безопасность API-ключей

- ключи API хранятся в БД только в зашифрованном виде;
- в интерфейсе отображается только маска ключа;
- для production обязательно задайте переменную окружения:

```bash
export APP_ENCRYPTION_KEY="<fernet-key>"
```

Если ключ не задан, используется dev fallback (подходит только для локальной разработки).
При `APP_ENV=production` запуск без `APP_ENCRYPTION_KEY` блокируется.

## Структура

- `review_processor/web.py` — FastAPI: лендинг, auth, app, admin, API.
- `web_templates/*.html` — отдельные HTML-страницы (landing/login/register/app/admin).
- `web_static/*.js`, `web_static/style.css` — клиентские скрипты и стили.
- `review_processor/repository.py` — PostgreSQL: users/sessions/accounts/templates/reviews/AI settings.
- `review_processor/service.py` — синхронизация, категоризация, процессы, ответы, WB/OZON клиенты.
- `review_processor/processor.py` — rule-based анализ отзывов.
- `review_processor/auth.py` — hash/verify password и токены сессий.
- `review_processor/security.py` — шифрование/дешифрование секретов.
- `tests/test_*.py` — unit-тесты.

## Быстрый старт

1. Установка зависимостей:

```bash
python3 -m pip install -r requirements.txt
```

2. Запуск:

```bash
python3 -m uvicorn review_processor.web:app --host 0.0.0.0 --port 8000
```

3. Открыть:

```text
http://localhost:8000
```

## OpenServer (локально)

Если открыть только папку проекта как статический сайт, будет показан `index.html` с подсказкой, потому что основной сайт рендерится FastAPI-приложением.

Чтобы сайт работал полноценно:

1. запустите backend:

```bash
python3 -m uvicorn review_processor.web:app --host 0.0.0.0 --port 8000
```

2. откройте в браузере:

```text
http://localhost:8000
```

## Основные API endpoints

- `GET /api/me`
- `POST /api/sync` (`all_accounts=true` или `account_id`)
  - в ответе для `all_accounts=true` возвращаются `success_accounts`, `failed_accounts`, `errors`
- `GET /api/reviews?priority=&status=&category=`
- `POST /api/reviews/{review_uid}/auto-reply`
- `POST /api/reviews/{review_uid}/queue-manual`
- `POST /api/reviews/{review_uid}/manual-reply`
- `GET /api/conversations?kind=&status=`
- `POST /api/conversations/{conversation_uid}/status`
- `GET /api/analytics`
- `GET/POST /api/accounts`, `POST /api/accounts/{id}/status`
- `GET/PUT /api/templates`
- `GET/PUT /api/admin/ai-settings` (admin)
- `GET /api/admin/users`, `POST /api/admin/users/{id}/role` (admin)
- `GET /api/admin/metrics` (admin)
- `GET /api/admin/actions` (admin)

## Подключение Яндекс ИИ

Поддержка уже заложена:

1. зайдите в `/admin`;
2. выберите provider = `yandex`;
3. укажите `yandex_api_key`, `yandex_folder_id` (и опционально `yandex_model_uri`);
4. сохраните настройки.

Если настройки не заполнены или API недоступен, система автоматически fallback'ится на встроенную rule-based категоризацию.

## CLI (базовая пакетная обработка JSON)

```bash
python3 -m review_processor.cli --input reviews.json --output processed_reviews.json
```

## Тесты

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## Развертывание на VPS (production)

Ниже базовый контур запуска сервиса на VPS без изменения бизнес-логики.

### 1) Обязательные переменные окружения

- `APP_ENV` — `production` для боевого окружения.
- `APP_DB_URL` — строка подключения PostgreSQL (обязательна).
- `APP_ENCRYPTION_KEY` — ключ шифрования секретов (обязательно в production).
- `APP_SELF_REGISTRATION_ENABLED` — `false` (рекомендуется для production).

Пример env-файла:

```env
APP_ENV=production
APP_DB_URL=postgresql://feedpilot:[REDACTED]@127.0.0.1:5432/feedpilot
APP_ENCRYPTION_KEY=<FERNET_KEY>
APP_SELF_REGISTRATION_ENABLED=false
PYTHONUNBUFFERED=1
```

> Важно: при `APP_ENV=production` приложение не будет работать без `APP_ENCRYPTION_KEY`.

### 2) systemd + nginx templates

В репозитории подготовлены шаблоны:

- `deploy/systemd/feedpilot.service`
- `deploy/nginx/feedpilot.conf`

Они используют env-файл (`EnvironmentFile`) и проксирование на `127.0.0.1:8000`.

Сервис запускается только с PostgreSQL runtime через `APP_DB_URL`.

### 3) Бэкапы PostgreSQL

Рекомендуемые команды:

```bash
# backup
pg_dump "$APP_DB_URL" -Fc -f /opt/feedpilot/backups/feedpilot_$(date +%F_%H-%M-%S).dump

# restore (на отдельную БД/стенд)
pg_restore --clean --if-exists --no-owner --dbname "$APP_DB_URL" /opt/feedpilot/backups/<backup>.dump
```

### 4) Обновление приложения в production

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart feedpilot
sudo systemctl status feedpilot
```

### 5) Runbook (минимум)

- Проверка логов приложения: `journalctl -u feedpilot -f`
- Проверка nginx: `nginx -t && systemctl reload nginx`
- Проверка порта: `ss -ltnp | rg 8000`
- Проверка backup/restore PostgreSQL не реже 1 раза в сутки на staging/backup-host.
- Проверка, что сайт отдается только по HTTPS и настроен редирект с 80 -> 443.
- В production убедиться, что в ответах есть security headers (`HSTS`, `CSP`, `X-Frame-Options`, `X-Content-Type-Options`).
- Для браузерных mutating API-запросов используется CSRF-заголовок `X-CSRF-Token` (frontend уже отправляет его автоматически).

## Миграция с SQLite (историческая) и валидация PostgreSQL

Для безопасного перехода без потери данных добавлены материалы:

- `docs/POSTGRESQL_MIGRATION_ROADMAP.md` — пошаговый план исторической migration/cutover/rollback.
- `deploy/postgres/schema_v1.sql` — целевая схема PostgreSQL (v1).
- `deploy/postgres/validation_after_import.sql` — сверка count/checksum после импорта.
- `scripts/export_sqlite_for_postgres.py` — экспорт текущей SQLite базы в CSV + `manifest.json`.

Пример экспорта:

```bash
python3 scripts/export_sqlite_for_postgres.py \
  --db /opt/feedpilot/data/reviews.db \
  --out /opt/feedpilot/migration_export
```

Дальше импорт CSV выполняется через `psql`/`\copy` по шагам из roadmap-документа.

После импорта запустите SQL-валидацию:

```bash
psql "$APP_DB_URL" -f deploy/postgres/validation_after_import.sql
```