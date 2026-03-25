"""
Телеграм-бот: мониторинг судебных дел по двум фиксированным персонам
Суды: 1) Мировые судьи СПб (mirsud.spb.ru)
      2) Калининский районный суд СПб (kln--spb.sudrf.ru)
Отчёт: каждый день в 11:00 МСК в TELEGRAM_CHAT_ID

Запуск:
    pip install -r requirements.txt
    playwright install chromium && playwright install-deps chromium
    export BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."   # ваш chat_id (или группы)
    python bot.py
"""

import asyncio
import logging
import os
import re
from datetime import datetime

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ══════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════

BOT_TOKEN       = "8760718814:AAGzC9fciHKlxnzxSdQeuy4duHaQCnU17Jo"
TELEGRAM_CHAT_ID = "488361906"

DB_PATH     = "court_monitor.db"
CHECK_HOUR  = 11   # МСК
CHECK_MIN   = 0

# ── Два отслеживаемых человека ────────────────────────────────
PERSONS = [
    {
        "id":         1,
        "fio":        "Иевлева Любовь Васильевна",
        "fio_short":  "Иевлева Л.В.",
        "last_name":  "Иевлева",
        "birth_year": "1942",
    },
    {
        "id":         2,
        "fio":        "Иевлев Дмитрий Вячеславович",
        "fio_short":  "Иевлев Д.В.",
        "last_name":  "Иевлев",
        "birth_year": "1979",
    },
]

# ── Два суда ──────────────────────────────────────────────────
COURTS = {
    "mirsud":     "Мировые судьи СПб (mirsud.spb.ru)",
    "kalininskiy": "Калининский районный суд СПб (kln--spb.sudrf.ru)",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id    INTEGER NOT NULL,
                court_key    TEXT NOT NULL,
                case_number  TEXT NOT NULL,
                case_type    TEXT,
                parties      TEXT,
                judge        TEXT,
                hearing_date TEXT,
                status       TEXT,
                found_at     TEXT NOT NULL,
                UNIQUE(person_id, court_key, case_number)
            );
            CREATE TABLE IF NOT EXISTS last_report (
                id       INTEGER PRIMARY KEY CHECK (id = 1),
                sent_at  TEXT
            );
        """)
        await db.commit()


async def save_new_cases(person_id: int, court_key: str, cases: list[dict]) -> list[dict]:
    """Сохраняет дела; возвращает только действительно новые."""
    new_cases = []
    async with aiosqlite.connect(DB_PATH) as db:
        for c in cases:
            try:
                await db.execute(
                    """INSERT INTO cases
                       (person_id, court_key, case_number, case_type,
                        parties, judge, hearing_date, status, found_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        person_id, court_key, c["number"], c.get("type"),
                        c.get("parties"), c.get("judge"), c.get("hearing_date"),
                        c.get("status"), datetime.now().isoformat(),
                    ),
                )
                new_cases.append(c)
            except aiosqlite.IntegrityError:
                pass  # уже в базе
        await db.commit()
    return new_cases


async def get_known_cases(person_id: int, court_key: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT case_number FROM cases WHERE person_id=? AND court_key=?",
            (person_id, court_key),
        )
        return [r["case_number"] for r in await cur.fetchall()]


# ══════════════════════════════════════════════════════════════
#  ПАРСЕР 1: Калининский районный суд (kln--spb.sudrf.ru)
#  — обычный HTML, кодировка windows-1251, GET-запрос
# ══════════════════════════════════════════════════════════════

KLN_BASE = "https://kln--spb.sudrf.ru/modules.php"

# delo_id коды на платформе sudrf.ru:
# 1540005 — гражданские дела
# 1540006 — административные
# 1540007 — уголовные
# 1500001 — все дела

KLN_DELO_IDS = [
    ("1540005", "Гражданское"),
    ("1540006", "Административное"),
    ("1540007", "Уголовное"),
]


async def parse_kalininskiy(last_name: str, birth_year: str,
                             session: aiohttp.ClientSession) -> list[dict]:
    """
    Поиск по Калининскому районному суду СПб через стандартный
    GET-интерфейс ГАС «Правосудие».
    Ищем по фамилии во всех типах дел.
    """
    results = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://kln--spb.sudrf.ru/",
    }

    for delo_id, delo_type in KLN_DELO_IDS:
        params = {
            "name":      "sud_delo",
            "srv_num":   "1",
            "name_op":   "sf",
            "delo_id":   delo_id,
            "delo_table": "g",
            "case_type": "0",
            "new":       "0",
            "u1_fio":    last_name,   # фамилия в поле «участник»
            "u1_from":   "",
            "u1_to":     "",
            "Submit":    "Найти",
        }
        try:
            async with session.get(
                KLN_BASE, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw = await resp.read()
                html = raw.decode("windows-1251", errors="replace")

            soup = BeautifulSoup(html, "html.parser")
            rows = _extract_sudrf_rows(soup)
            for r in rows:
                r["type"] = delo_type
                results.append(r)

        except Exception as e:
            log.error("kln sudrf parse error (%s): %s", delo_type, e)

        await asyncio.sleep(1)  # вежливая пауза между запросами

    log.info("Калининский суд → %s: найдено %d дел", last_name, len(results))
    return results


def _extract_sudrf_rows(soup: BeautifulSoup) -> list[dict]:
    """Парсит таблицу результатов поиска на сайтах *.sudrf.ru."""
    cases = []
    # Ищем таблицу с делами — обычно class содержит 'list' или просто первая большая таблица
    table = soup.find("table", id="tablcont")
    if not table:
        # fallback: ищем любую таблицу с 5+ колонками
        for t in soup.find_all("table"):
            if t.find("tr") and len(t.find("tr").find_all("td")) >= 4:
                table = t
                break

    if not table:
        return cases

    rows = table.find_all("tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        texts = [c.get_text(" ", strip=True) for c in cols]

        # Ищем номер дела — ссылка в первой или второй колонке
        case_link = row.find("a", href=re.compile(r"case_id=|delo_id="))
        number = case_link.get_text(strip=True) if case_link else texts[0]
        if not number or number.lower() in ("№", "номер"):
            continue

        cases.append({
            "number":       number,
            "parties":      texts[3] if len(texts) > 3 else None,
            "judge":        texts[4] if len(texts) > 4 else None,
            "hearing_date": texts[1] if len(texts) > 1 else None,
            "status":       texts[5] if len(texts) > 5 else None,
        })
    return cases


# ══════════════════════════════════════════════════════════════
#  ПАРСЕР 2: Мировые судьи СПб (mirsud.spb.ru)
#  — JavaScript SPA, нужен Playwright
# ══════════════════════════════════════════════════════════════

MIRSUD_URL = "https://mirsud.spb.ru/cases/"


async def parse_mirsud(fio: str, birth_year: str) -> list[dict]:
    """
    Playwright-парсер mirsud.spb.ru.
    Открывает браузер, вводит ФИО, нажимает поиск, читает таблицу.
    """
    results = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
            )
            page = await ctx.new_page()

            log.info("mirsud.spb.ru → открываем страницу для %s ...", fio)
            await page.goto(MIRSUD_URL, wait_until="networkidle", timeout=60_000)
            await asyncio.sleep(2)

            # ── Переключаемся на гражданские дела (если есть вкладки) ─────
            for selector in [
                "text=Гражданские",
                "[data-type='civil']",
                "button:has-text('Гражданские')",
                "a:has-text('Гражданские')",
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.click()
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # ── Поле ФИО ─────────────────────────────────────────────────
            fio_field = None
            for sel in [
                "input[name*='fio']",
                "input[placeholder*='ФИО']",
                "input[placeholder*='участника']",
                "input[placeholder*='Участник']",
                "input[ng-model*='fio']",
                "input[ng-model*='participant']",
                "input[type='text']:first-of-type",
            ]:
                try:
                    fio_field = await page.wait_for_selector(sel, timeout=4_000)
                    if fio_field:
                        break
                except Exception:
                    pass

            if not fio_field:
                log.error("mirsud: поле ФИО не найдено")
                await browser.close()
                return []

            await fio_field.triple_click()
            await fio_field.fill(fio)

            # ── Год рождения ──────────────────────────────────────────────
            for sel in [
                "input[name*='year']", "input[name*='birth']",
                "input[placeholder*='год']", "input[ng-model*='year']",
            ]:
                try:
                    yf = await page.wait_for_selector(sel, timeout=3_000)
                    if yf:
                        await yf.triple_click()
                        await yf.fill(birth_year)
                        break
                except Exception:
                    pass

            # ── Кнопка поиска ─────────────────────────────────────────────
            for sel in [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Найти')",
                "button:has-text('Поиск')",
                "a:has-text('Найти')",
                ".search-btn",
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    pass

            # ── Ждём результатов ──────────────────────────────────────────
            try:
                await page.wait_for_selector(
                    "table tr, .result-row, .no-results, [class*='result']",
                    timeout=20_000,
                )
            except PWTimeout:
                log.warning("mirsud: таймаут ожидания результатов")

            await asyncio.sleep(3)

            # ── Читаем таблицу ────────────────────────────────────────────
            rows = await page.query_selector_all("table tbody tr")
            if not rows:
                rows = await page.query_selector_all("tr.case-row, .result-row, table tr")

            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) < 2:
                    continue
                texts = [(await c.inner_text()).strip() for c in cols]
                if all(t == "" for t in texts):
                    continue
                case = _parse_mirsud_row(texts)
                if case:
                    results.append(case)

            await browser.close()

    except Exception as e:
        log.error("mirsud parse error для %s: %s", fio, e, exc_info=True)

    log.info("mirsud.spb.ru → %s: найдено %d дел", fio, len(results))
    return results


def _parse_mirsud_row(texts: list[str]) -> dict | None:
    """Извлекает поля дела из списка текстов ячеек строки таблицы."""
    # Ищем номер дела по шаблону NN-NNN/YYYY
    number = None
    for t in texts:
        if re.search(r"\d{1,5}[-/]\d{1,5}[-/]?\d{2,4}", t):
            number = t
            break
    if not number:
        non_empty = [t for t in texts if t]
        if not non_empty:
            return None
        number = non_empty[0]

    def safe(i): return texts[i] if i < len(texts) else None

    return {
        "number":       number,
        "type":         safe(1),
        "parties":      safe(2),
        "court_site":   safe(3),
        "hearing_date": safe(4),
        "status":       safe(5),
    }


# ══════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ЛОГИКА: СБОР ДАННЫХ И ФОРМИРОВАНИЕ ОТЧЁТА
# ══════════════════════════════════════════════════════════════

async def check_all() -> str:
    """
    Проверяет оба суда для обоих людей.
    Возвращает готовый текст отчёта для Telegram.
    """
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    report_lines = [
        f"📋 <b>Ежедневный отчёт по судебным делам</b>",
        f"🕐 {now_str} МСК\n",
    ]

    async with aiohttp.ClientSession() as session:
        for person in PERSONS:
            report_lines.append(
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>{person['fio']}</b> (г.р. {person['birth_year']})\n"
            )

            # ── 1. Калининский районный суд ───────────────────────────
            kln_cases = await parse_kalininskiy(
                person["last_name"], person["birth_year"], session
            )
            kln_new = await save_new_cases(person["id"], "kalininskiy", kln_cases)

            report_lines.append(f"🏛 <b>Калининский районный суд СПб</b>")
            if kln_cases:
                report_lines.append(f"   Найдено дел: <b>{len(kln_cases)}</b>")
                if kln_new:
                    report_lines.append(f"   🆕 Новых с последней проверки: <b>{len(kln_new)}</b>")
                for c in kln_cases:
                    report_lines.append(_format_case(c))
            else:
                report_lines.append("   ✅ Дел не обнаружено")

            report_lines.append("")

            # ── 2. Мировые судьи СПб ─────────────────────────────────
            mir_cases = await parse_mirsud(person["fio"], person["birth_year"])
            mir_new = await save_new_cases(person["id"], "mirsud", mir_cases)

            report_lines.append(f"⚖️ <b>Мировые судьи СПб</b>")
            if mir_cases:
                report_lines.append(f"   Найдено дел: <b>{len(mir_cases)}</b>")
                if mir_new:
                    report_lines.append(f"   🆕 Новых с последней проверки: <b>{len(mir_new)}</b>")
                for c in mir_cases:
                    report_lines.append(_format_case(c))
            else:
                report_lines.append("   ✅ Дел не обнаружено")

            report_lines.append("")

    report_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    report_lines.append("🔄 Следующая проверка завтра в 11:00 МСК")
    return "\n".join(report_lines)


def _format_case(c: dict) -> str:
    lines = [f"   📁 <code>{c['number']}</code>"]
    if c.get("type"):         lines.append(f"      Тип: {c['type']}")
    if c.get("parties"):      lines.append(f"      Стороны: {c['parties'][:120]}")
    if c.get("judge"):        lines.append(f"      Судья: {c['judge']}")
    if c.get("hearing_date"): lines.append(f"      Дата: {c['hearing_date']}")
    if c.get("status"):       lines.append(f"      Статус: {c['status']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  TELEGRAM ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

router = Router()


@router.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 <b>Бот мониторинга судебных дел</b>\n\n"
        "Отслеживаю дела для:\n"
        "• Иевлева Любовь Васильевна (1942)\n"
        "• Иевлев Дмитрий Вячеславович (1979)\n\n"
        "Суды:\n"
        "• Мировые судьи СПб (mirsud.spb.ru)\n"
        "• Калининский районный суд СПб\n\n"
        "⏰ Ежедневный отчёт в <b>11:00 МСК</b>\n\n"
        "Команды:\n"
        "/check — запустить проверку прямо сейчас\n"
        "/status — информация о боте",
        parse_mode="HTML",
    )


@router.message(Command("check"))
async def cmd_check(msg: Message):
    await msg.answer("🔍 Запускаю проверку обоих судов... (~1–2 минуты)")
    report = await check_all()
    # Telegram ограничивает 4096 символов — разбиваем при необходимости
    await send_long_message(msg.bot, msg.chat.id, report)


@router.message(Command("status"))
async def cmd_status(msg: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM cases")
        total = (await cur.fetchone())[0]
        cur2 = await db.execute(
            "SELECT COUNT(*) FROM cases WHERE court_key='kalininskiy'"
        )
        kln_total = (await cur2.fetchone())[0]
        cur3 = await db.execute(
            "SELECT COUNT(*) FROM cases WHERE court_key='mirsud'"
        )
        mir_total = (await cur3.fetchone())[0]

    await msg.answer(
        f"ℹ️ <b>Статус бота</b>\n\n"
        f"📦 Всего дел в базе: <b>{total}</b>\n"
        f"   — Калининский суд: {kln_total}\n"
        f"   — Мировые судьи: {mir_total}\n\n"
        f"⏰ Следующий отчёт в <b>11:00 МСК</b>",
        parse_mode="HTML",
    )


async def send_long_message(bot: Bot, chat_id: str | int, text: str):
    """Отправляет сообщение, разбивая на части по 4000 символов."""
    max_len = 4000
    if len(text) <= max_len:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    for part in parts:
        await bot.send_message(chat_id, part, parse_mode="HTML")
        await asyncio.sleep(0.3)


# ══════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК И ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════

async def scheduled_report(bot: Bot):
    log.info("▶ Ежедневный отчёт запущен")
    try:
        report = await check_all()
        await send_long_message(bot, TELEGRAM_CHAT_ID, report)
        log.info("✔ Отчёт отправлен в чат %s", TELEGRAM_CHAT_ID)
    except Exception as e:
        log.error("Ошибка формирования отчёта: %s", e, exc_info=True)
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"⚠️ Ошибка при формировании отчёта: {e}",
        )


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        scheduled_report, "cron",
        hour=CHECK_HOUR, minute=CHECK_MIN,
        args=[bot],
    )
    scheduler.start()

    log.info(
        "Бот запущен. Ежедневный отчёт в %02d:%02d МСК → чат %s",
        CHECK_HOUR, CHECK_MIN, TELEGRAM_CHAT_ID,
    )

    # Сразу шлём отчёт при старте (можно убрать если не нужно)
    # await scheduled_report(bot)

    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
