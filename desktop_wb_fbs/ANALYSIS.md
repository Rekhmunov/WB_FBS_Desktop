# Анализ паритета: веб FeedPilot ↔ desktop_wb_fbs

Эталон: `AI-reviews-main.zip` в корне репозитория (веб-сервис).  
Цель: вкладки **ВБ ФБС** и **ОЗОН ФБС** в десктопе — **идентичны** веб-разделу по потоку сборки, печати, КИЗ и грузоместам.

Сверка: 11.11.2026 (Europe/Moscow).

## Стек десктопа

| Компонент | Выбор |
|-----------|--------|
| Python | 3.8.x (Win7) |
| UI | PyQt5 |
| БД | SQLite |
| Сеть | WB Marketplace + Content; Ozon Seller API |

## Главный вывод

- **ВБ ФБС:** ядро сборки уже близко к вебу (новые → поставка → КИЗ / без КИЗ → стикеры → TRBX). Не хватает вкладки **«В доставке»** и ops-контура (водитель/TTN/автосинк/ТСД/возвраты).
- **ОЗОН ФБС:** разрыв больше. Веб держит **локальные поставки** + **containers (грузоместа с привязкой)**. Десктоп сейчас на **Ozon carriage API** без GM-bind и без веб-модели вкладок («Ожидают сборки / отгрузки / Доставляются»).

## ВБ ФБС — gap

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки Новые / На сборке / В доставке | 3 видимые | Нет «В доставке» |
| Ручной sync | new+orders+status+supplies+trbx | То же ✓ |
| Автосинк / ops-log | Есть | Нет |
| Create/add поставку + МГТ | Есть | Есть ✓ |
| Товары с КИЗ / без КИЗ | metaDetails + pick | Есть ✓ |
| Стикеры / лист подбора / TRBX | Есть | Есть ✓ |
| QR поставки | `/barcode` после deliver | Локальный QR |
| Отменённые в поставке | Есть | Есть ✓ |
| Водитель / TTN / ТСД / Вывод КИЗ / возвраты | Есть | Нет (P2) |

### WB Marketplace методы (эталон)

`orders/new`, `orders`, `orders/status`, `orders/stickers`, `orders/meta`, `meta/sgtin`, `supplies`, `order-ids`, `supplies/{id}/orders` (PATCH), `trbx` CRUD+stickers, `barcode`.  
Deliver в вебе — через портал ВБ, не из app.

## ОЗОН ФБС — gap

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки | Ожидают сборки / отгрузки / Доставляются | Новые/сборка/доставка/завершённые (WB-style) |
| Локальные поставки после ship | Основа UX | Нет (carriage API) |
| Ship-all / split multi | Есть | Нет |
| Товары с КИЗ (exemplars) | create/set/validate/status + import | Частично |
| Товары без КИЗ | pick-verify | Есть ✓ |
| Грузоместа containers + bind | Полный цикл | **Нет** |
| Стикеры v3 async + старые v2 | Есть | Частично (labels) |
| Лист подбора / по категориям | Есть | Нет |
| Акт / QR / PDF / approve | Есть | Частично |
| Move to delivering + гейты | Есть | Нет |
| Отменённые / frozen | Есть | Нет |
| Водитель / TTN / ПВЗ / штрафы / ТСД | Есть | Нет (P2) |

### Ozon Seller методы (эталон)

Postings: `/v4/.../list`, `/v3/.../get`, `/v4/.../ship`, `/v1/.../split`, package-label v2/v3, exemplar v5/v6.  
Carriage/act: delivery-method, carriage/delivery/list, carriage/get/create/set-postings/approve, act create/check/barcode.  
**Containers:** list/create/get/fill/remove-postings/approve/cancel/label/get/task/info — критичный пробел десктопа.

## Приоритет внедрения

### P0 — обязательный паритет сборки
1. WB: вкладка «В доставке» (done supplies + QR/scanDt).
2. Ozon: вкладки как в вебе; локальные поставки после ship; КИЗ/без КИЗ в карточке; стикеры v3; лист подбора.
3. Ozon: **грузоместа** (container bind/approve/labels).

### P1 — карточка и печать
- Ozon: ship-all, split multi, cancelled/frozen, move-to-delivering с гейтами.
- WB: QR через `/barcode` после deliver (как веб).

### P2 — ops (явно согласовать)
Автосинк, водитель, TTN/эТрН, ТСД, Вывод КИЗ, возвраты/ПВЗ, штрафы, driver PIN.

## Файлы эталона в архиве

- WB: `review_processor/wb_fbs.py`, `wb_fbs_detail.py`
- Ozon: `ozon_fbs.py`, `ozon_fbs_supplies.py`, `ozon_fbs_marking.py`, `ozon_fbs_containers.py`, `ozon_fbs_shipments.py`, `ozon_fbs_pick_verify.py`
- UI: `web_static/app.js`, `ozon_fbs*.js`, `web_templates/app.html`
