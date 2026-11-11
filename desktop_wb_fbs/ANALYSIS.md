# Анализ паритета: веб FeedPilot ↔ desktop_wb_fbs

Эталон: `AI-reviews-main.zip` (ветка/архив веб-сервиса).  
Цель: разделы **ВБ ФБС** и **ОЗОН ФБС** на ПК — тот же поток сборки/печати/КИЗ/грузомест, адаптированный под PyQt5.

Сверка: **11.11.2026, Europe/Moscow** · `main` @ `6c52fc2`.

## Главный вывод

**Полного паритета с вебом ещё нет**, но дневное ядро сборки уже перенесено.

| Раздел | Оценка | Комментарий |
|--------|--------|-------------|
| **ВБ ФБС** | ~75–85% ядра | Новые → поставка → КИЗ/ШК → стикеры → TRBX → «В доставке». Ops (водитель/TTN/автосинк/ТСД/возвраты) нет. |
| **ОЗОН ФБС** | ~45–55% продукта | Portal-вкладки, локальные поставки, ship-all/split, карточка поставки, labels v3, ГМ. Нет driver/TTN/TSD/returns/fines и полного sticker-print pipeline. |

Масштаб: web `ozon_fbs_supplies.py` ≈ 8500 LOC · desktop `ozon_supplies.py` ≈ 950 LOC.

---

## ВБ ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки Новые / На сборке / В доставке | ✓ | ✓ (`delivery_listed`) |
| Ручной sync | ✓ | ✓ |
| Create / add поставку + МГТ | ✓ | ✓ |
| КИЗ + товары без КИЗ (pick) | ✓ | ✓ |
| Стикеры / лист подбора / TRBX | ✓ | ✓ |
| Отменённые в поставке | ✓ | ✓ |
| Автосинк / ops-log | ✓ | ✗ |
| Водитель / TTN / ТСД / возвраты / КИЗ-оборот | ✓ | ✗ P2 |

---

## ОЗОН ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки portal (6 шт.) | ✓ | ✓ `awaiting_*` / cancelled / arbitration |
| Локальные поставки после ship | ✓ | ✓ `ozon_fbs_supplies` + `supply_id` |
| «Собрать все заказы» + выбор create/add | ✓ | ✓ `OzonShipAllDialog` |
| «Создать поставку» из выбранных | ✓ | ✓ (без полного preview-guard веба) |
| Split multi `/v1/posting/fbs/split` | ✓ | ✓ |
| Карточка поставки (КИЗ / ШК / этикетки / лист / ГМ / rename / move) | ✓ | ✓ `OzonSupplyDetailDialog` |
| Package-label v3 + poll | ✓ | ✓ |
| Sticker barcode bind | ✓ полный | ◐ колонки + bind из get |
| Отменённые в поставке | ✓ таблица | ◐ список в message box |
| Грузоместа containers | ✓ + reconcile/scan | ◐ dialog от поставки/carriage |
| Автосинк / водитель / TTN / ТСД / ПВЗ / штрафы | ✓ | ✗ P2 |

### Ключевые файлы десктопа

- UI: `app/ui/ozon_fbs_page.py`, `ozon_supply_detail.py`, `ozon_ship_all_dialog.py`, `ozon_containers_dialog.py`
- Сервисы: `app/services/ozon_supplies.py`, `ozon_split.py`, `ozon_labels.py`, `ozon_mark_pick.py`
- API/sync: `app/ozon/client.py`, `app/ozon/sync.py`, `app/ozon/__init__.py`
- Схема: `app/db/__init__.py` (`ozon_fbs_postings`, `ozon_fbs_supplies`, `ozon_fbs_carriages`)

### Эталон в архиве

- WB: `review_processor/wb_fbs.py`, `wb_fbs_detail.py`
- Ozon: `ozon_fbs.py`, `ozon_fbs_supplies.py`, `ozon_fbs_marking.py`, `ozon_fbs_containers.py`, `ozon_fbs_stickers.py`
- UI: `web_static/ozon_fbs.js`, `web_static/app.js`

---

## Приоритет дальше

### P0 — довести дневную сборку Ozon
1. Полноценный экран/таблица отменённых в поставке (как WB detail).  
2. Preview-guards для create-from-selection + «добавить к существующей» из выбора.  
3. Sticker print / scan resolve ближе к `ozon_fbs_stickers.py`.

### P1
- Ozon GM: reconcile / scan-bind / archive как в web.  
- WB: QR через `/barcode` после deliver (если ещё не везде).

### P2 — ops
Автосинк, водитель, TTN/эТрН, ТСД, возвраты/ПВЗ, штрафы.

---

## История недавних коммитов (main)

| Commit | Суть |
|--------|------|
| `e3a5f3a` | WB «В доставке» + `delivery_listed` |
| `6797cc1` | Ozon containers / грузоместа |
| `ac2c01a` | Portal tabs + local supplies + ship-all |
| `52babb7` | Split multi + package-label v3 |
| `a51f13d` | Карточка локальной поставки + ship-all choose modal |
| `6c52fc2` | Create supply from selection + cancelled в detail |
