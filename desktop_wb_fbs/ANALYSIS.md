# Анализ паритета: веб FeedPilot ↔ desktop_wb_fbs

Эталон: `AI-reviews-main.zip` (ветка/архив веб-сервиса).  
Цель: разделы **ВБ ФБС** и **ОЗОН ФБС** на ПК — тот же поток сборки/печати/КИЗ/грузомест, адаптированный под PyQt5.

Сверка: **11.11.2026, Europe/Moscow** · `main` (после selection guards / cancelled table / stickers sheet).

## Главный вывод

**Полного паритета с вебом ещё нет**, но дневное ядро сборки продолжает сближаться.

| Раздел | Оценка | Комментарий |
|--------|--------|-------------|
| **ВБ ФБС** | ~75–85% ядра | Новые → поставка → КИЗ/ШК → стикеры → TRBX → «В доставке». Ops нет. |
| **ОЗОН ФБС** | ~55–65% продукта | Portal, локальные поставки, ship-all/selection create|add, split, карточка, labels v3, sticker sheet, ГМ, cancelled table. Нет driver/TTN/TSD/returns/fines и полного image sticker pipeline. |

Масштаб: web `ozon_fbs_supplies.py` ≈ 8500 LOC · desktop `ozon_supplies.py` ≈ 1200+ LOC.

---

## ВБ ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки Новые / На сборке / В доставке | ✓ | ✓ |
| Sync, create/add, МГТ | ✓ | ✓ |
| КИЗ / pick / стикеры / лист / TRBX / отменённые | ✓ | ✓ |
| Автосинк / водитель / TTN / ТСД / возвраты | ✓ | ✗ P2 |

---

## ОЗОН ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки portal (6 шт.) | ✓ | ✓ |
| Локальные поставки после ship | ✓ | ✓ |
| «Собрать все» + create/add modal | ✓ | ✓ |
| Selection → create **или** add + preview guards | ✓ | ✓ |
| Split multi | ✓ | ✓ |
| Карточка поставки (КИЗ / ШК / этикетки / лист / ГМ / rename / move) | ✓ | ✓ |
| Отменённые в поставке (таблица) | ✓ | ✓ |
| Package-label v3 | ✓ | ✓ |
| Sticker/QR sheet + bind | ✓ image pages | ◐ HTML sheet + barcode bind |
| Полный PNG sticker job / scan resolve | ✓ | ✗ частичный |
| Автосинк / водитель / TTN / ТСД / ПВЗ / штрафы | ✓ | ✗ P2 |

### Ключевые файлы

- `app/ui/ozon_fbs_page.py`, `ozon_supply_detail.py`, `ozon_ship_all_dialog.py`, `ozon_selection_supply_dialog.py`
- `app/services/ozon_supplies.py`, `ozon_split.py`, `ozon_labels.py`
- `app/ozon/client.py`, `sync.py`, `__init__.py`

---

## Приоритет дальше

### P0
1. Ozon: image/PDF sticker pages ближе к web (`build_stickers_print`).  
2. Ozon: scan-resolve по `sticker_barcode`.  

### P1
- GM reconcile / archive.  
- WB: supply QR через Marketplace `/barcode` где ещё локальный.

### P2
Автосинк, водитель, TTN, ТСД, возвраты, штрафы.
