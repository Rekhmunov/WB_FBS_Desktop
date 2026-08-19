# SQLite -> PostgreSQL Migration Roadmap

Документ описывает безопасный переход FEEDPILOT с SQLite на PostgreSQL и полный cutover.

## 1. Цели миграции

- повысить надежность и устойчивость при одновременной работе пользователей;
- снять ограничения SQLite на конкурентные записи;
- подготовить платформу к росту нагрузки и фоновым задачам;
- сохранить полную историю данных (users, settings, accounts, reviews, actions, conversations).

## 2. Область данных (scope)

Переносим таблицы:

- `users`
- `sessions`
- `ai_settings`
- `marketplace_accounts`
- `response_templates`
- `response_template_variants`
- `processing_rules`
- `product_recommendations`
- `review_items`
- `review_actions`
- `conversation_items`

## 3. Стратегия перехода

### Phase A — Preparation

1. Развернуть PostgreSQL (отдельный instance/managed service).
2. Накатить SQL-схему `deploy/postgres/schema_v1.sql`.
3. Подготовить dry-run экспорт SQLite в CSV (`scripts/export_sqlite_for_postgres.py`).
4. Проверить импорт в staging PostgreSQL.

### Phase B — Dry run validation

1. Экспорт текущей SQLite в CSV + `manifest.json`.
2. Импорт в staging PostgreSQL.
3. Сверка:
   - row counts по каждой таблице;
   - checksum/выборочная сверка критичных полей;
   - проверка бизнес-сценариев (логин, sync, фильтры, правила).

### Phase C — Production cutover (окно обслуживания)

1. Freeze операций записи:
   - временно остановить sync;
   - ограничить админские изменения.
2. Финальный экспорт SQLite.
3. Импорт в production PostgreSQL.
4. Smoke-test на prod-копии (до переключения трафика).
5. Переключение runtime на PostgreSQL (отдельной задачей реализации).
   - задать `APP_DB_URL=postgresql://...`;
   - проверить, что сервис стартует на новом backend.

### Phase D — Post-cutover

1. Усиленный мониторинг 24-48 часов:
   - ошибки API,
   - latency `/api/reviews`,
   - failed login rate.
2. Сохранить SQLite snapshot как rollback backup.

## 4. Rollback план

Если после переключения выявлены критичные ошибки:

1. Остановить приложение.
2. Вернуть runtime обратно на SQLite (последний стабильный snapshot).
3. Перезапустить сервис.
4. Зафиксировать окно инцидента и причины.
5. Повторить dry-run после исправления.

## 5. Контрольные критерии готовности (Go/No-Go)

- [ ] Все таблицы импортированы без ошибок.
- [ ] Row counts совпадают (или документировано объяснимое отклонение).
- [ ] Критичные сценарии проходят smoke-test.
- [ ] Есть backup PostgreSQL и SQLite snapshot перед cutover.
- [ ] Команда знает rollback команды.

## 6. Runtime switch checklist (production)

1. Проверить доступность PostgreSQL и права пользователя приложения.
2. Убедиться, что в env задано:
   - `APP_ENV=production`
   - `APP_DB_URL=postgresql://...`
   - `APP_ENCRYPTION_KEY=...`
3. Перезапустить сервис.
4. Проверить smoke-тест:
   - логин;
   - список аккаунтов;
   - `/api/reviews`;
   - ручной/авто-ответ;
   - админка/роли.
5. Запустить валидацию после импорта:
   - `deploy/postgres/validation_after_import.sql`.

## 6. Риски и меры

1. **Несовместимость типов (SQLite TEXT -> PostgreSQL JSONB/BOOLEAN/TIMESTAMPTZ)**  
   Мера: staging dry-run и явные casts в import SQL.

2. **Нарушение ссылочной целостности**  
   Мера: импорт по порядку зависимостей (users -> accounts -> reviews/actions/...).

3. **Потеря данных в окне переключения**  
   Мера: write-freeze + финальный экспорт непосредственно перед cutover.

4. **Регрессия производительности запросов**  
   Мера: создать индексы из `schema_v1.sql`, проверить explain plans.

## 7. Что не делаем в этой задаче

- не добавляем ORM;
- не внедряем Redis/очереди.

## 8. Быстрые команды запуска (runtime на PostgreSQL)

```bash
export APP_ENV=production
export APP_DB_URL="postgresql://feedpilot:***@127.0.0.1:5432/feedpilot"
export APP_ENCRYPTION_KEY="..."
python3 -m uvicorn review_processor.web:app --host 0.0.0.0 --port 8000
```

