# FeedPilot Desktop — Поставки ВБ ФБС

Локальная программа для Windows 7+ с функционалом раздела **«Поставки — ВБ ФБС»**.  
Веб-сервис FeedPilot **не используется** и **не изменяется**: только API Wildberries + локальная SQLite.

## Что входит / что нет

| Включено | Исключено |
|----------|-----------|
| Источники (WB FBS) | ТСД |
| Товары + категории | Вывод КИЗ / Честный знак |
| Синхронизация заказов/поставок | CryptoPro / Analytics |
| Новые / На сборке / В доставке | |
| Собрать МГТ, новая поставка, добавить к существующей | |
| Карточка поставки, TRBX, стикеры, лист подбора | |
| Маркировка (КИЗ → WB meta/sgtin) | |
| Проверка ШК (локально) | |
| Ручная синхронизация | Автоматика sync / collect MGT (шестерёнка) |

## Стек (Win7+)

| Компонент | Выбор | Почему |
|-----------|--------|--------|
| Python | **3.8.x** | Последняя ветка с официальной поддержкой Windows 7 |
| UI | **PyQt5** | Qt5 работает на Win7; Qt6 / PySide6 требуют Win10+ |
| БД | **SQLite** | Без отдельного сервера БД |
| HTTP | `urllib` (stdlib) | Как в серверном клиенте WB |
| Упаковка | PyInstaller + Inno Setup | Один `Setup.exe` / `FeedPilotFBS.exe` |

## Запуск (разработка)

```bash
cd desktop_wb_fbs
python -m pip install -r requirements.txt
python run.py
```

На Windows можно просто дважды кликнуть:
- **`FeedPilot FBS.vbs`** — без чёрного окна (удобно каждый день)
- **`FeedPilot FBS.bat`** — с консолью (если нужна ошибка на экране)

Данные: `%APPDATA%/FeedPilotFBS/` (Windows) или `~/.local/share/FeedPilotFBS/` (Linux).

## Структура

```
desktop_wb_fbs/
  run.py                 # точка входа
  app/
    db/                  # SQLite схема и доступ
    wb/                  # клиент Marketplace + Content API, sync
    services/            # бизнес-логика (источники, товары, поставки, КИЗ…)
    ui/                  # PyQt5 окна и диалоги
```

## Сборка под Windows

1. Python 3.8.10 x64 на машине сборки (можно Win10).  
2. `pip install -r requirements.txt pyinstaller`  
3. `pyinstaller --noconfirm FeedPilotFBS.spec` (spec добавится при упаковке).  
4. Подпись Code Signing + Inno Setup → установщик.

## Важно

- Имя источника должно содержать **«ФБС»** или **FBS**.  
- Токену нужны категории **Marketplace** и (для названий/цвета) **Контент**.  
- Коды КИЗ хранят разделитель GS (`\\u001D`) — не использовать обычный `str.strip()`.
