# FeedPilot Desktop — ВБ ФБС и ОЗОН ФБС

Локальная программа для Windows 7+ с разделами **«Поставки — ВБ ФБС»** и **«ОЗОН ФБС»**.  
Веб-сервис FeedPilot не меняется: эталон UX — архив `AI-reviews-main.zip`; десктоп ходит напрямую в API WB / Ozon и хранит данные в SQLite.

Актуальный разбор паритета с вебом: [`ANALYSIS.md`](ANALYSIS.md).

## Что есть сейчас

### ВБ ФБС
| Включено | Пока нет (как в вебе P2) |
|----------|--------------------------|
| Источники WB FBS, товары, категории | Водитель, TTN |
| Ручной sync + автосинк (Настройки → Автосинк) | ТСД / PIN водителя |
| Собрать МГТ, создать/добавить поставку | КИЗ-оборот / Честный знак |
| Карточка поставки, TRBX, стикеры, лист подбора | Полный эТрН как в вебе |
| QR поставки: WB `/barcode` (+ локальный fallback) | |
| Водители + назначение + черновик ТН | |
| Маркировка (КИЗ → WB meta/sgtin), проверка ШК | |
| Отменённые в поставке | |

### ОЗОН ФБС
| Включено | Пока нет / частично |
|----------|---------------------|
| Вкладки как в портале: Ожидают сборки / отгрузки / Доставляются / Спорные / Доставлены / Отменены | Web PNG sticker image pages |
| Локальные поставки `OZ-FBS-*` после ship | Полный эТрН / ТСД / штрафы |
| «Собрать все заказы» (create / add) + «Создать поставку» из выбора | |
| «Разделить мультизаказы» (`/v1/posting/fbs/split`) | |
| Карточка: КИЗ / ШК / этикетки / стикеры / ГМ / водитель / ТН-черновик / отменённые | |
| ГМ: локальный архив + сверка binds с порталом Ozon | |
| Возвраты ПВЗ (ReceivedBySeller sync + таблица) | |
| Скан QR/ШК этикетки → открыть отправление/поставку | |
| Автосинк (Настройки → Автосинк) | |
| Carriage API (отгрузки) как fallback + акт QR/PDF | |

## Стек (Win7+)

| Компонент | Выбор | Почему |
|-----------|--------|--------|
| Python | **3.8.x** | Последняя ветка с официальной поддержкой Windows 7 |
| UI | **PyQt5** | Qt5 на Win7; Qt6 / PySide6 требуют Win10+ |
| БД | **SQLite** | Без отдельного сервера |
| HTTP | `urllib` (stdlib) | Как в серверных клиентах |
| Упаковка | PyInstaller + Inno Setup | Один `Setup.exe` / `FeedPilotFBS.exe` |

## Запуск (разработка)

```bash
cd desktop_wb_fbs
python -m pip install -r requirements.txt
python run.py
```

На Windows:
- **`FeedPilot FBS.vbs`** — без чёрной консоли
- **`FeedPilot FBS.bat`** — через `pythonw`; если нет — консоль с ошибкой

Данные: `%APPDATA%/FeedPilotFBS/` (Windows) или `~/.local/share/FeedPilotFBS/` (Linux).

## Структура

```
desktop_wb_fbs/
  run.py
  ANALYSIS.md            # паритет с веб-архивом
  app/
    db/                  # SQLite (WB + ozon_fbs_*)
    wb/                  # Marketplace + Content API, sync
    ozon/                # Ozon Seller client, sync, tabs
    services/            # WB/Ozon бизнес-логика (поставки, КИЗ, labels, GM…)
    ui/                  # PyQt5: fbs_page, ozon_fbs_page, supply/ozon detail…
  tests/
```

## Сборка под Windows

1. Python 3.8.10 x64 на машине сборки.  
2. `pip install -r requirements.txt pyinstaller`  
3. `pyinstaller --noconfirm FeedPilotFBS.spec`  
4. Code Signing + Inno Setup → установщик.

## Важно

- Имя источника должно содержать **«ФБС»** / **FBS** (WB и Ozon).  
- WB: токену нужны **Marketplace** и (для названий) **Контент**.  
- Ozon: `Client-Id` + `Api-Key` Seller API.  
- Коды КИЗ хранят GS (`\\u001D`) — не использовать обычный `str.strip()`.
