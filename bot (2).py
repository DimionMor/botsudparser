"""
Телеграм-бот: мониторинг судебных дел по двум фиксированным персонам
Суды: 1) Мировые судьи СПб (mirsud.spb.ru)
      2) Калининский районный суд СПб (kln--spb.sudrf.ru)
Отчёт: каждый день в 11:00 МСК в TELEGRAM_CHAT_ID
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
 
BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
 
DB_PATH    = "court_monitor.db"
CHECK_HOUR = 11   # МСК
CHECK_MIN  = 0
 
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
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
 
# Шаблон номера дела на sudrf.ru: 2-123/2026, 12-45/26, 2а-678/2025 и т.п.
CASE_NUMBER_RE = re.compile(r"^\d{1,5}[а-яА-Я]?[-\u2011]\d{1,6}/\d{2,4}$")
 
 
# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
 
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id     INTEGER NOT NULL,
                court_key     TEXT NOT NULL,
                case_number   TEXT NOT NULL,
                case_type     TEXT,
                case_category TEXT,
                parties       TEXT,
                judge         TEXT,
                hearing_date  TEXT,
                status        TEXT,
                found_at      TEXT NOT NULL,
                UNIQUE(person_id, court_key, case_number)
            );
        """)
        await db.commit()
 
 
async def save_new_cases(person_id: int, court_key: str, cases: list[dict]) -> list[dict]:
    new_cases = []
    async with aiosqlite.connect(DB_PATH) as db:
        for c in cases:
            try:
                await db.execute(
                    """INSERT INTO cases
                       (person_id, court_key, case_number, case_type, case_category,
                        parties, judge, hearing_date, status, found_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        person_id, court_key, c["number"],
                        c.get("type"), c.get("case_category"),
                        c.get("parties"), c.get("judge"),
                        c.get("hearing_date"), c.get("status"),
                        datetime.now().isoformat(),
                    ),
                )
                new_cases.append(c)
            except aiosqlite.IntegrityError:
                pass
        await db.commit()
    return new_cases
 
 
# ══════════════════════════════════════════════════════════════
#  ПАРСЕР 1: Калининский районный суд (kln--spb.sudrf.ru)
# ══════════════════════════════════════════════════════════════
 
KLN_BASE = "https://kln--spb.sudrf.ru/modules.php"
 
KLN_DELO_IDS = [
    ("1540005", "Гражданское"),
    ("1540006", "Административное"),
    ("1540007", "Уголовное"),
]
 
 
async def parse_kalininskiy(last_name: str, birth_year: str,
                             session: aiohttp.ClientSession) -> list[dict]:
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
            "name":       "sud_delo",
            "srv_num":    "1",
            "name_op":    "sf",
            "delo_id":    delo_id,
            "delo_table": "g",
            "case_type":  "0",
            "new":        "0",
            "u1_fio":     last_name,
            "u1_from":    "",
            "u1_to":      "",
            "Submit":     "Найти",
        }
        try:
            async with session.get(
                KLN_BASE, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw = await resp.read()
                html = raw.decode("windows-1251", errors="replace")
 
            soup = BeautifulSoup(html, "html.parser")
            rows = _extract_sudrf_rows(soup, last_name)
            for r in rows:
                r["type"] = delo_type
                results.append(r)
 
        except Exception as e:
            log.error("kln sudrf parse error (%s): %s", delo_type, e)
 
        await asyncio.sleep(1)
 
    log.info("Калининский суд => %s: найдено %d дел", last_name, len(results))
    return results
 
 
def _is_case_number(text: str) -> bool:
    """
    Проверяет, что строка является номером дела.
    Форматы: 2-123/2026, 12-45/26, 2а-678/2025, 2а-678/25
    Дефис может быть обычным (-) или неразрывным (U+2011).
    """
    t = text.strip()
    return bool(CASE_NUMBER_RE.match(t))
 
 
def _extract_sudrf_rows(soup: BeautifulSoup, search_name: str) -> list[dict]:
    """
    Парсит таблицу результатов поиска на сайтах *.sudrf.ru.
 
    Стратегия: ищем ячейки TD, текст которых совпадает с шаблоном
    номера дела (N-NNN/YYYY). Это надёжнее чем искать по ссылкам,
    потому что у разных судов разные параметры в href.
 
    Дополнительная проверка: строка должна содержать фамилию искомого
    человека (регистронезависимо) — это отсекает любые посторонние строки.
    """
    cases = []
    search_name_lower = search_name.lower()
 
    # Берём все строки всех таблиц
    for row in soup.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
 
        texts = [c.get_text(" ", strip=True) for c in cols]
 
        # Ищем колонку с номером дела
        number = None
        for t in texts:
            if _is_case_number(t):
                number = t
                break
 
        if not number:
            continue
 
        # Строка должна содержать фамилию искомого человека
        row_text = " ".join(texts).lower()
        if search_name_lower not in row_text:
            continue
 
        def safe(i):
            return texts[i].strip() if i < len(texts) and texts[i].strip() else None
 
        # Типовой порядок колонок sudrf.ru:
        # 0: номер дела, 1: дата слушания, 2: категория, 3: стороны, 4: судья, 5: статус
        # Но номер может быть не в колонке 0, поэтому ищем динамически
        num_idx = next(i for i, t in enumerate(texts) if _is_case_number(t))
 
        cases.append({
            "number":        number,
            "hearing_date":  safe(num_idx + 1),
            "case_category": safe(num_idx + 2),
            "parties":       safe(num_idx + 3),
            "judge":         safe(num_idx + 4),
            "status":        safe(num_idx + 5),
        })
 
    log.info("_extract_sudrf_rows: найдено %d дел для '%s'", len(cases), search_name)
    return cases
 
 
# ══════════════════════════════════════════════════════════════
#  ПАРСЕР 2: Мировые судьи СПб (mirsud.spb.ru) — Playwright
# ══════════════════════════════════════════════════════════════
 
MIRSUD_URL = "https://mirsud.spb.ru/cases/"
 
 
async def parse_mirsud(fio: str, birth_year: str) -> list[dict]:
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
 
            log.info("mirsud.spb.ru => %s ...", fio)
            await page.goto(MIRSUD_URL, wait_until="networkidle", timeout=60_000)
            await asyncio.sleep(2)
 
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
 
            try:
                await page.wait_for_selector(
                    "table tr, .result-row, .no-results, [class*='result']",
                    timeout=20_000,
                )
            except PWTimeout:
                log.warning("mirsud: таймаут ожидания результатов")
 
            await asyncio.sleep(3)
 
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
 
    log.info("mirsud.spb.ru => %s: найдено %d дел", fio, len(results))
    return results
 
 
def _parse_mirsud_row(texts: list[str]) -> dict | None:
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
 
    def safe(i):
        return texts[i] if i < len(texts) else None
 
    return {
        "number":       number,
        "type":         safe(1),
        "parties":      safe(2),
        "court_site":   safe(3),
        "hearing_date": safe(4),
        "status":       safe(5),
    }
 
 
# ══════════════════════════════════════════════════════════════
#  ОТЧЁТ
# ══════════════════════════════════════════════════════════════
 
def _format_case(c: dict) -> str:
    lines = ["   📁 <code>" + c["number"] + "</code>"]
    if c.get("type"):          lines.append("      Тип: " + c["type"])
    if c.get("case_category"): lines.append("      Категория: " + c["case_category"][:100])
    if c.get("parties"):       lines.append("      Стороны: " + c["parties"][:120])
    if c.get("court_site"):    lines.append("      Участок: " + c["court_site"][:80])
    if c.get("judge"):         lines.append("      Судья: " + c["judge"])
    if c.get("hearing_date"):  lines.append("      Дата: " + c["hearing_date"])
    if c.get("status"):        lines.append("      Статус: " + c["status"])
    return "\n".join(lines)
 
 
async def check_all() -> str:
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    report_lines = [
        "📋 <b>Ежедневный отчёт по судебным делам</b>",
        "🕐 " + now_str + " МСК\n",
    ]
 
    async with aiohttp.ClientSession() as session:
        for person in PERSONS:
            report_lines.append(
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + "👤 <b>" + person["fio"] + "</b> (г.р. " + person["birth_year"] + ")\n"
            )
 
            kln_cases = await parse_kalininskiy(
                person["last_name"], person["birth_year"], session
            )
            kln_new = await save_new_cases(person["id"], "kalininskiy", kln_cases)
 
            report_lines.append("🏛 <b>Калининский районный суд СПб</b>")
            if kln_cases:
                report_lines.append("   Найдено дел: <b>" + str(len(kln_cases)) + "</b>")
                if kln_new:
                    report_lines.append(
                        "   🆕 Новых с последней проверки: <b>" + str(len(kln_new)) + "</b>"
                    )
                for c in kln_cases:
                    report_lines.append(_format_case(c))
            else:
                report_lines.append("   ✅ Дел не обнаружено")
 
            report_lines.append("")
 
            mir_cases = await parse_mirsud(person["fio"], person["birth_year"])
            mir_new = await save_new_cases(person["id"], "mirsud", mir_cases)
 
            report_lines.append("⚖️ <b>Мировые судьи СПб</b>")
            if mir_cases:
                report_lines.append("   Найдено дел: <b>" + str(len(mir_cases)) + "</b>")
                if mir_new:
                    report_lines.append(
                        "   🆕 Новых с последней проверки: <b>" + str(len(mir_new)) + "</b>"
                    )
                for c in mir_cases:
                    report_lines.append(_format_case(c))
            else:
                report_lines.append("   ✅ Дел не обнаружено")
 
            report_lines.append("")
 
    report_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    report_lines.append("🔄 Следующая проверка завтра в 11:00 МСК")
    return "\n".join(report_lines)
 
 
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
        "/status — статистика по базе",
        parse_mode="HTML",
    )
 
 
@router.message(Command("check"))
async def cmd_check(msg: Message):
    await msg.answer("🔍 Запускаю проверку обоих судов... (~1–2 минуты)")
    report = await check_all()
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
        "ℹ️ <b>Статус бота</b>\n\n"
        + "📦 Всего дел в базе: <b>" + str(total) + "</b>\n"
        + "   — Калининский суд: " + str(kln_total) + "\n"
        + "   — Мировые судьи: " + str(mir_total) + "\n\n"
        + "⏰ Следующий отчёт в <b>11:00 МСК</b>",
        parse_mode="HTML",
    )
 
 
async def send_long_message(bot: Bot, chat_id, text: str):
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
            "⚠️ Ошибка при формировании отчёта: " + str(e),
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
        "Бот запущен. Ежедневный отчёт в %02d:%02d МСК => чат %s",
        CHECK_HOUR, CHECK_MIN, TELEGRAM_CHAT_ID,
    )
 
    await dp.start_polling(bot, skip_updates=True)
 
 
if __name__ == "__main__":
    asyncio.run(main())
