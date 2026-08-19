# FEEDPILOT Technical Backlog (Pre-Launch / Post-Launch)

Документ для постановки задач в Jira/Notion перед боевым запуском.

## Priority scale

- **P0** — блокер запуска
- **P1** — критично в первый релиз
- **P2** — важно после первых клиентов
- **P3** — масштабирование

## Pre-Launch backlog

### P0-01. App configuration via environment variables
- **Цель:** убрать прод-зависимости от hardcoded значений.
- **Scope:** `APP_ENV`, `APP_DB_PATH`, `APP_SELF_REGISTRATION_ENABLED`.
- **Acceptance criteria:**
  - приложение читает конфиг из env;
  - дефолтные значения безопасны для dev;
  - поведение документировано.
- **Dependencies:** none.

### P0-02. Production encryption key guard
- **Цель:** запретить запуск production без `APP_ENCRYPTION_KEY`.
- **Scope:** сервис шифрования секретов.
- **Acceptance criteria:**
  - при `APP_ENV=production` и пустом `APP_ENCRYPTION_KEY` сервис падает при старте;
  - при `APP_ENV=development` fallback остаётся.
- **Dependencies:** P0-01.

### P0-03. Deployment templates (systemd + nginx)
- **Цель:** стандартизировать запуск на VPS.
- **Scope:** шаблоны unit/service + reverse proxy.
- **Acceptance criteria:**
  - есть готовый `feedpilot.service` template;
  - есть готовый `nginx` site template;
  - в шаблонах используются env-файлы и loopback binding.
- **Dependencies:** P0-01.

### P0-04. Backup and restore operational scripts
- **Цель:** обеспечить восстановление данных.
- **Scope:** backup script, restore script, retention.
- **Acceptance criteria:**
  - backup создаёт timestamp-файл;
  - restore поднимает копию БД;
  - есть инструкции запуска и cron-пример.
- **Dependencies:** P0-03.

### P0-05. Production runbook
- **Цель:** быстрый ввод в эксплуатацию и troubleshooting.
- **Scope:** install, start, logs, update, incident steps.
- **Acceptance criteria:**
  - README содержит VPS deployment guide;
  - есть команды диагностики и rollback/restore.
- **Dependencies:** P0-03, P0-04.

### P1-01. PostgreSQL migration planning
- **Цель:** перейти с SQLite до роста клиентской нагрузки.
- **Scope:** целевая схема, миграции, mapping sqlite->postgres.
- **Acceptance criteria:**
  - документ миграции согласован;
  - описан cutover и rollback;
  - определён SLA downtime.
- **Dependencies:** none.

### P1-02. Data migration dry run
- **Цель:** проверить перенос текущих данных в staging.
- **Scope:** users/sessions/accounts/templates/reviews/actions/conversations.
- **Acceptance criteria:**
  - dry-run проходит без потерь;
  - контрольные выборки совпадают;
  - зафиксирован отчёт о расхождениях.
- **Dependencies:** P1-01.

### P1-03. Index tuning for review queries
- **Цель:** ускорить `/api/reviews` фильтры и пагинацию.
- **Scope:** индексы по `user_id/status/source/category/updated_at`.
- **Acceptance criteria:**
  - p95 `GET /api/reviews` в целевом диапазоне;
  - query plans фиксированы в документации.
- **Dependencies:** P1-02 (если переход на PostgreSQL выполняется до релиза).

### P1-04. Backup restore test in CI/Staging
- **Цель:** убедиться, что бэкапы реально восстанавливаются.
- **Scope:** регулярный restore-check job.
- **Acceptance criteria:**
  - хотя бы 1 успешный restore test в сутки;
  - алерт на провал проверки.
- **Dependencies:** P0-04.

## Post-Launch backlog

### P2-01. Incremental sync cursor per marketplace account
- **Цель:** убрать повторную загрузку большого диапазона.
- **Scope:** `last_successful_sync_at` + safety window.
- **Acceptance criteria:**
  - повторные sync-запуски уменьшают API traffic;
  - нет потерь отзывов (контрольная сверка).

### P2-02. Async workers + queue (Redis)
- **Цель:** вынести долгие операции из HTTP request path.
- **Scope:** sync/reply retry/background processing.
- **Acceptance criteria:**
  - пользовательский API не блокируется на длительные sync;
  - есть retry policy и dead-letter handling.

### P2-03. Retention policy for review_actions/conversations
- **Цель:** контролировать рост БД.
- **Scope:** TTL/archive policy + periodic cleanup.
- **Acceptance criteria:**
  - задан срок хранения;
  - размер БД прогнозируем.

### P2-04. Monitoring and alerting baseline
- **Цель:** наблюдаемость production.
- **Scope:** sync success ratio, failed accounts, reply errors, DB size.
- **Acceptance criteria:**
  - dashboard + alert rules в рабочем виде;
  - дежурный получает уведомления по порогам.

### P3-01. Multi-tenant scale strategy
- **Цель:** подготовка к росту числа кабинетов/пользователей.
- **Scope:** partitioning/sharding strategy, optional Citus.
- **Acceptance criteria:**
  - задокументированы триггеры масштабирования;
  - есть validated PoC.

### P3-02. Advanced AI storage and retrieval
- **Цель:** расширить AI сценарии (по необходимости).
- **Scope:** pgvector / retrieval-augmented context.
- **Acceptance criteria:**
  - бизнес-кейс подтверждён;
  - измерен прирост качества классификации/ответов.

