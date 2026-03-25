# 🤖 Бот мониторинга судебных дел — Иевлевы, СПб

Ежедневно в **11:00 МСК** присылает в Telegram отчёт о судебных делах
для двух человек по двум судам Санкт-Петербурга.

---

## Кого и где отслеживаем

| Человек | Год рождения |
|---------|-------------|
| Иевлева Любовь Васильевна | 1942 |
| Иевлев Дмитрий Вячеславович | 1979 |

| Суд | Сайт | Метод парсинга |
|-----|------|----------------|
| Мировые судьи СПб | mirsud.spb.ru | Playwright (JS-приложение) |
| Калининский районный суд СПб | kln--spb.sudrf.ru | aiohttp + BeautifulSoup (обычный HTML) |

---

## Установка

### 1. Создайте бота в Telegram

1. Откройте [@BotFather](https://t.me/BotFather)
2. `/newbot` → введите имя и username
3. Скопируйте токен: `7123456789:AAFxxxxxxxx`

### 2. Узнайте ваш chat_id

Напишите боту [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш `id`.
Это и будет `TELEGRAM_CHAT_ID`. Если хотите получать отчёты в группу — добавьте
бота в группу и используйте id группы (он отрицательный, например `-1001234567890`).

### 3. Установите зависимости

```bash
pip install -r requirements.txt
```

### 4. Установите браузер Playwright (один раз)

```bash
# macOS / Windows:
playwright install chromium

# Linux-сервер (Ubuntu/Debian) — нужны системные зависимости:
playwright install chromium
playwright install-deps chromium
```

### 5. Задайте переменные окружения и запустите

```bash
export BOT_TOKEN="ВАШ_ТОКЕН"
export TELEGRAM_CHAT_ID="ВАШ_CHAT_ID"
python bot.py
```

---

## Запуск через Docker (рекомендуется для VPS)

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .
VOLUME ["/app/court_monitor.db"]

CMD ["python", "bot.py"]
```

```bash
docker build -t ievlev-court-bot .

docker run -d \
  --name ievlev-court-bot \
  --restart unless-stopped \
  -e BOT_TOKEN="ВАШ_ТОКЕН" \
  -e TELEGRAM_CHAT_ID="ВАШ_CHAT_ID" \
  -v $(pwd)/db:/app \
  ievlev-court-bot
```

> Образ `mcr.microsoft.com/playwright/python` уже содержит Chromium —
> никаких дополнительных установок не требуется.

---

## Запуск через systemd (VPS без Docker)

```ini
# /etc/systemd/system/ievlev-court-bot.service
[Unit]
Description=Ievlev Court Monitor Bot
After=network.target

[Service]
WorkingDirectory=/opt/ievlev-court-bot
Environment=BOT_TOKEN=ВАШ_ТОКЕН
Environment=TELEGRAM_CHAT_ID=ВАШ_CHAT_ID
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now ievlev-court-bot
journalctl -fu ievlev-court-bot    # просмотр логов в реальном времени
```

---

## Команды бота

| Команда | Действие |
|---------|----------|
| `/start` | Информация о боте |
| `/check` | Запустить проверку прямо сейчас |
| `/status` | Сколько дел в базе |

---

## Пример отчёта

```
📋 Ежедневный отчёт по судебным делам
🕐 25.03.2026 11:00 МСК

━━━━━━━━━━━━━━━━━━━━━━━━
👤 Иевлева Любовь Васильевна (г.р. 1942)

🏛 Калининский районный суд СПб
   Найдено дел: 1
   📁 2-1234/2026
      Тип: Гражданское
      Стороны: Иевлева Л.В. → ООО "Пример"
      Судья: Иванов А.А.
      Дата: 15.04.2026
      Статус: Назначено

⚖️ Мировые судьи СПб
   ✅ Дел не обнаружено

━━━━━━━━━━━━━━━━━━━━━━━━
👤 Иевлев Дмитрий Вячеславович (г.р. 1979)

🏛 Калининский районный суд СПб
   ✅ Дел не обнаружено

⚖️ Мировые судьи СПб
   ✅ Дел не обнаружено

━━━━━━━━━━━━━━━━━━━━━━━━
🔄 Следующая проверка завтра в 11:00 МСК
```

---

## Как хранятся данные

SQLite-база `court_monitor.db` с двумя таблицами:
- **`cases`** — все найденные дела с уникальным индексом `(person_id, court_key, case_number)`,
  чтобы в отчёте всегда видно сколько дел **новых** с прошлого раза
- **`last_report`** — время последней отправки

---

## Частые вопросы

**Почему mirsud.spb.ru требует Playwright?**
Сайт мировых судей СПб — Angular-приложение. При обычном HTTP-запросе
вы получите пустой HTML-шаблон. Playwright запускает реальный браузер в фоне.

**Сколько занимает проверка?**
Калининский суд — ~5–10 сек (обычный HTTP).
Мировые судьи — ~30–60 сек (браузер).
Итого на два человека: ~2–3 минуты.

**Что если сайт изменил вёрстку?**
Для kln--spb.sudrf.ru парсер ищет таблицу по нескольким критериям.
Для mirsud.spb.ru — перебирает несколько CSS-селекторов.
При кардинальных изменениях нужно обновить селекторы в коде.
