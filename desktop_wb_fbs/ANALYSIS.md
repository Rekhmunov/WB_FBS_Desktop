# Анализ паритета: веб FeedPilot ↔ desktop_wb_fbs

Эталон: `AI-reviews-main.zip` (ветка/архив веб-сервиса).  
Цель: разделы **ВБ ФБС** и **ОЗОН ФБС** на ПК — тот же поток сборки/печати/КИЗ/грузомест, адаптированный под PyQt5.

Сверка: **11.11.2026, Europe/Moscow** · ветка `cursor/ozon-stickers-scan-autosync-993e` (PDF/QR stickers + scan-resolve + автосинк).

## Главный вывод

**Полного паритета с вебом ещё нет**, но P0 по Ozon stickers/scan и автосинк закрыты на уровне desktop-паритета.

| Раздел | Оценка | Комментарий |
|--------|--------|-------------|
| **ВБ ФБС** | ~80–88% ядра | Новые → поставка → КИЗ/ШК → стикеры → TRBX → «В доставке». Автосинк ✓. Ops (водитель/TTN/TSD/returns) нет. |
| **ОЗОН ФБС** | ~70–78% продукта | Portal, локальные поставки, ship-all/selection, split, labels v3 + post-print bind, PDF+HTML sticker job, scan-resolve, автосинк. Нет driver/TTN/TSD/returns/fines и web PNG image pages. |

Масштаб: web `ozon_fbs_supplies.py` ≈ 8500 LOC · desktop `ozon_supplies.py` ≈ 1200+ LOC.

---

## ВБ ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки Новые / На сборке / В доставке | ✓ | ✓ |
| Sync, create/add, МГТ | ✓ | ✓ |
| КИЗ / pick / стикеры / лист / TRBX / отменённые | ✓ | ✓ |
| Автосинк (окно МСК + интервал) | ✓ | ✓ |
| Водитель / TTN / ТСД / возвраты | ✓ | ✗ P2 |

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
| Package-label v3 + post-print sticker bind | ✓ | ✓ |
| Sticker job (PDF labels + QR sheet + overwrite bind) | ✓ image pages | ✓ PDF + HTML sheet |
| Scan-resolve по `sticker_barcode` / lower / posting | ✓ | ✓ |
| Автосинк (окно МСК + интервал) | ✓ | ✓ |
| Водитель / TTN / ТСД / ПВЗ / штрафы | ✓ | ✗ P2 |

### Ключевые файлы

- `app/ui/ozon_fbs_page.py`, `ozon_supply_detail.py`, `settings_page.py`, `main.py`
- `app/services/ozon_supplies.py`, `ozon_labels.py`, `ozon_stickers.py`, `auto_sync.py`
- `app/ozon/client.py`, `sync.py`

---

## Приоритет дальше

### P1
- GM reconcile / archive.  
- WB: supply QR через Marketplace `/barcode` где ещё локальный.  
- Ozon: ближе к web PNG sticker pages (опционально).

### P2
Водитель, TTN, ТСД, возвраты, штрафы.
