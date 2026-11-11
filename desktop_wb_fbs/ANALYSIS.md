# Анализ паритета: веб FeedPilot ↔ desktop_wb_fbs

Эталон: `AI-reviews-main.zip` (ветка/архив веб-сервиса).  
Цель: разделы **ВБ ФБС** и **ОЗОН ФБС** на ПК — тот же поток сборки/печати/КИЗ/грузомест, адаптированный под PyQt5.

Сверка: **11.11.2026, Europe/Moscow** · `main` + GM archive/reconcile + WB `/barcode`.

## Главный вывод

**Полного паритета с вебом ещё нет**, но дневное ядро и P1 (GM reconcile/archive, WB supply QR) закрыты. Остаётся ops P2.

| Раздел | Оценка | Комментарий |
|--------|--------|-------------|
| **ВБ ФБС** | ~85–90% ядра | Сборка/КИЗ/стикеры/TRBX/автосинк ✓. QR поставки через Marketplace `/barcode` (+ local fallback). Ops нет. |
| **ОЗОН ФБС** | ~78–85% продукта | Portal, поставки, labels/stickers/scan, автосинк, ГМ create/fill/approve + **локальный архив** + **reconcile binds**. Нет driver/TTN/TSD/returns/fines. |

---

## ВБ ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Вкладки / sync / МГТ / поставка | ✓ | ✓ |
| КИЗ / pick / стикеры / лист / TRBX | ✓ | ✓ |
| QR поставки Marketplace `/barcode` | ✓ | ✓ (+ local fallback) |
| Автосинк | ✓ | ✓ |
| Водитель / TTN / ТСД / возвраты | ✓ | ✗ P2 |

---

## ОЗОН ФБС

| Функция | Веб | Десктоп |
|---------|-----|---------|
| Portal tabs / ship / selection / split | ✓ | ✓ |
| Labels v3 + sticker bind + scan | ✓ | ✓ |
| ГМ create/fill/approve/labels | ✓ | ✓ |
| ГМ local archive + restore | ✓ | ✓ |
| ГМ reconcile binds (portal wins) | ✓ | ✓ |
| Автосинк | ✓ | ✓ |
| PNG sticker image pages | ✓ | ◐ PDF + HTML |
| Водитель / TTN / ТСД / ПВЗ / штрафы | ✓ | ✗ P2 |

---

## Приоритет дальше

### P2
Водитель, TTN / эТрН, ТСД, возвраты ПВЗ, штрафы.

### Опционально
Ozon PNG sticker pages ближе к web image job.
