"""
bot/formatting.py — единый форматтер дат, времени и карточек уроков.

Портировано из alfacrm-bot почти без изменений: логика формирования
карточек урока, сводок и расписаний не зависит ни от мессенджера, ни
от CRM. Изменения относительно исходника:
  * TG_LIMIT/SAFE_LIMIT переименованы под MAX (лимит текста сообщения —
    4000 символов, см. dev.max.ru/docs-api/objects/NewMessageBody);
  * safe_call/answer_blocks/send_blocks адаптированы под API MaxBot
    (клиент MAX сам делает ретраи на 429/5xx, поэтому здесь остаётся
    только защита "не уронить всю рассылку из-за одного сбоя").

Основные точки входа:
    build_summary(...)   -> list[str]   сводка менеджера, сгруппированная по дням
    build_schedule(...)  -> list[str]   расписание для роли parent/teacher/manager
    format_lesson(...)   -> str         одна карточка урока
    lesson_sort_key(...)                корректная сортировка (дата, время)
    answer_blocks / send_blocks         отправка с защитой от сбоев
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import settings

logger = logging.getLogger(__name__)

# ==================== СЛОВАРИ ====================

WEEK_FULL = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
WEEK_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Статусы берём из settings, чтобы они не расходились с alfacrm_client.
STATUS_PLANNED = settings.STATUS_PLANNED
STATUS_CANCELLED = settings.STATUS_CANCELLED
STATUS_CONDUCTED = settings.STATUS_CONDUCTED

STATUS_LABELS = {
    STATUS_PLANNED: "📌 запланирован",
    STATUS_CANCELLED: "❌ отменён",
    STATUS_CONDUCTED: "✅ проведён",
}

MAX_TEXT_LIMIT = 4000  # dev.max.ru/docs-api/objects/NewMessageBody
SAFE_LIMIT = 3900  # запас на теги и служебные строки
DIVIDER = "━━━━━━━━━━━━━━━━━━"


# ==================== ПАРСИНГ ====================

def esc(value: Any) -> str:
    """Экранирует текст из CRM и от пользователей — он уходит в format=html."""
    return escape(str(value if value is not None else ""), quote=False)


_esc = esc


def _parse_date_str(raw: Any) -> Optional[date]:
    """Понимает YYYY-MM-DD, DD.MM.YYYY и полный datetime в обоих вариантах."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("T", " ").split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_lesson_date(lesson: Dict[str, Any]) -> Optional[date]:
    """Дата урока. Сначала поле `date`, при неудаче — дата из `time_from`."""
    return _parse_date_str(lesson.get("date")) or _parse_date_str(lesson.get("time_from"))


def lesson_date_iso(lesson: Dict[str, Any]) -> str:
    """Нормализованная дата YYYY-MM-DD — для фильтрации сравнением строк."""
    d = parse_lesson_date(lesson)
    return d.isoformat() if d else ""


def parse_lesson_datetime(lesson: Dict[str, Any]) -> Optional[datetime]:
    """
    Начало урока как aware-datetime в часовом поясе филиала.

    Нужно потому, что settings.now() возвращает aware-время: сравнение
    с наивным datetime из CRM падало бы с TypeError.
    """
    day = parse_lesson_date(lesson)
    if not day:
        return None
    raw = str(lesson.get("time_from") or "").replace("T", " ").strip()
    tail = raw.split(" ")[-1] if raw else ""
    parts = tail.split(":") if tail else []
    try:
        hour = int(parts[0]) if len(parts) >= 1 else 0
        minute = int(parts[1]) if len(parts) >= 2 else 0
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=settings.TZ)


def fmt_db_time(value: Any) -> str:
    """
    Отметка времени из SQLite -> '13.08.2026 17:45' в поясе филиала.

    CURRENT_TIMESTAMP в SQLite всегда UTC, поэтому без явного пересчёта
    время заявок «уезжает» на разницу с локальным поясом.
    """
    s = str(value or "").strip()
    if not s:
        return "—"
    s = s.replace("T", " ").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(s, fmt)
        except ValueError:
            continue
        local = parsed.replace(tzinfo=timezone.utc).astimezone(settings.TZ)
        return local.strftime("%d.%m.%Y %H:%M")
    return s


def fmt_time(value: Any) -> str:
    """'2026-08-04 15:00:00' / '15:00:00' / '15:00' -> '15:00'."""
    s = str(value or "").strip()
    if not s:
        return "??:??"
    tail = s.replace("T", " ").split(" ")[-1]
    return tail[:5] if len(tail) >= 5 else "??:??"


def fmt_time_range(lesson: Dict[str, Any]) -> str:
    time_from = fmt_time(lesson.get("time_from"))
    time_to = fmt_time(lesson.get("time_to"))
    if time_to == "??:??":
        return time_from
    return f"{time_from}–{time_to}"


def lesson_sort_key(lesson: Dict[str, Any]) -> Tuple[str, str]:
    """Сортировка сначала по дате, затем по времени начала."""
    return (lesson_date_iso(lesson) or "9999-99-99", fmt_time(lesson.get("time_from")))


def in_date_range(
    lesson: Dict[str, Any],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> bool:
    """Единая проверка попадания урока в период (по нормализованной дате)."""
    iso = lesson_date_iso(lesson)
    if not iso:
        return False
    if date_from and iso < date_from:
        return False
    if date_to and iso > date_to:
        return False
    return True


# ==================== ДАТЫ ====================

def fmt_date_short(d: Optional[date]) -> str:
    """04.08.2026, Пн"""
    if not d:
        return "дата не указана"
    return f"{d.strftime('%d.%m.%Y')}, {WEEK_SHORT[d.weekday()]}"


def fmt_date_long(d: Optional[date]) -> str:
    """4 августа, понедельник"""
    if not d:
        return "дата не указана"
    return f"{d.day} {MONTHS_GEN[d.month - 1]}, {WEEK_FULL[d.weekday()]}"


def relative_hint(d: Optional[date], today: Optional[date] = None) -> str:
    """'сегодня' / 'завтра' / 'вчера' — или пустая строка."""
    if not d:
        return ""
    today = today or settings.today()
    delta = (d - today).days
    return {0: "сегодня", 1: "завтра", -1: "вчера"}.get(delta, "")


def day_header(d: Optional[date], today: Optional[date] = None) -> str:
    hint = relative_hint(d, today)
    text = fmt_date_long(d)
    if hint:
        text += f" — {hint}"
    return f"📅 <b>{esc(text)}</b>"


def status_label(status: Any) -> str:
    try:
        return STATUS_LABELS.get(int(status), f"статус {status}")
    except (TypeError, ValueError):
        return "статус неизвестен"


# ==================== КАРТОЧКА УРОКА ====================

def format_lesson(
    lesson: Dict[str, Any],
    *,
    role: str = "manager",
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    hw_files_count: int = 0,
    show_date: bool = False,
    hw_limit: int = 120,
) -> str:
    """
    Карточка одного урока в виде строк «Метка: значение».

    role:
        "parent"  — дата, время, тип занятия, тема (без статуса и имени ребёнка)
        "teacher" — дата, время, ребёнок, статус, тема
        "manager" — дата, время, ребёнок, педагог, статус, ДЗ

    show_date=False — когда карточки уже сгруппированы под заголовком дня.
    """
    teachers = teachers or {}
    customers = customers or {}
    lines: List[str] = []

    if show_date:
        lines.append(f"📅 <b>Дата:</b> {esc(fmt_date_short(parse_lesson_date(lesson)))}")

    lines.append(f"🕐 <b>Время:</b> {esc(fmt_time_range(lesson))}")

    if role in ("teacher", "manager"):
        names = [customers.get(cid, f"ID:{cid}") for cid in lesson.get("customer_ids") or []]
        lines.append(f"👤 <b>Ребёнок:</b> {esc(', '.join(names)) if names else '—'}")

    if role == "manager":
        names = [teachers.get(tid, f"ID:{tid}") for tid in lesson.get("teacher_ids") or []]
        lines.append(f"👨‍🏫 <b>Педагог:</b> {esc(', '.join(names)) if names else '—'}")

    if role == "parent":
        lines.append(f"📘 <b>Занятие:</b> {esc(lesson.get('lesson_type_name') or 'Урок')}")
    else:
        lines.append(f"📊 <b>Статус:</b> {esc(status_label(lesson.get('status')))}")

    topic = (lesson.get("topic") or "").strip()
    if topic:
        lines.append(f"📝 <b>Тема:</b> {esc(topic)}")

    if role in ("manager", "parent"):
        homework = (lesson.get("homework") or "").strip()
        if homework:
            if len(homework) > hw_limit:
                homework = homework[:hw_limit].rstrip() + "…"
            hw_line = f"📚 <b>ДЗ:</b> {esc(homework)}"
            if hw_files_count:
                hw_line += f" (📎 {hw_files_count})"
            lines.append(hw_line)
        elif hw_files_count:
            lines.append(f"📎 <b>ДЗ:</b> файлов: {hw_files_count}")
        elif role == "manager":
            lines.append("📚 <b>ДЗ:</b> не указано")

    return "\n".join(lines)


def format_homework_card(
    lesson: Dict[str, Any],
    *,
    files_count: int = 0,
    today: Optional[date] = None,
) -> str:
    """Карточка домашнего задания для родителя (с экранированием текста из CRM)."""
    day = parse_lesson_date(lesson)
    hint = relative_hint(day, today)
    date_line = fmt_date_short(day) + (f" — {hint}" if hint else "")

    lines = [f"📅 <b>Дата:</b> {esc(date_line)}"]
    topic = (lesson.get("topic") or "").strip()
    if topic:
        lines.append(f"📝 <b>Тема:</b> {esc(topic)}")
    lines.append("")
    lines.append(esc((lesson.get("homework") or "").strip()))
    if files_count:
        lines.append("")
        lines.append(f"📎 Файлов: {files_count}")
    return "\n".join(lines)


# ==================== ГРУППИРОВКА И НАРЕЗКА ====================

def group_by_day(lessons: Iterable[Dict[str, Any]]) -> List[Tuple[Optional[date], List[Dict]]]:
    """[(дата, [уроки]), ...] в хронологическом порядке; уроки без даты — в конце."""
    buckets: Dict[str, List[Dict]] = {}
    for lesson in lessons:
        buckets.setdefault(lesson_date_iso(lesson), []).append(lesson)

    result: List[Tuple[Optional[date], List[Dict]]] = []
    for key in sorted(buckets, key=lambda k: k or "9999-99-99"):
        day_lessons = sorted(buckets[key], key=lambda l: fmt_time(l.get("time_from")))
        result.append((_parse_date_str(key), day_lessons))
    return result


def split_messages(blocks: Sequence[str], limit: int = SAFE_LIMIT) -> List[str]:
    """
    Склеивает блоки в сообщения не длиннее limit, не разрывая блок посередине
    (иначе ломаются HTML-теги).
    """
    messages: List[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            messages.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        messages.append(current)
    return messages


def _day_counters(lessons: Sequence[Dict[str, Any]]) -> str:
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    planned = len(lessons) - conducted - cancelled
    parts = []
    if conducted:
        parts.append(f"✅ {conducted}")
    if cancelled:
        parts.append(f"❌ {cancelled}")
    if planned:
        parts.append(f"📌 {planned}")
    return "  ·  ".join(parts)


# ==================== ГОТОВЫЕ СООБЩЕНИЯ ====================

def build_summary(
    lessons: Sequence[Dict[str, Any]],
    *,
    period_label: str,
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    hw_counts: Optional[Dict[str, int]] = None,
    today: Optional[date] = None,
) -> List[str]:
    """
    Сводка менеджеру, сгруппированная по дням.
    Возвращает список готовых к отправке сообщений (format="html").
    """
    hw_counts = hw_counts or {}
    today = today or settings.today()

    if not lessons:
        return [f"📊 <b>Сводка {esc(period_label)}</b>\n\nЗа этот период уроков не было."]

    total = len(lessons)
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    planned = total - conducted - cancelled

    blocks: List[str] = [
        f"📊 <b>Сводка {esc(period_label)}</b>\n"
        f"Всего: {total}  ·  ✅ {conducted}  ·  ❌ {cancelled}  ·  📌 {planned}"
    ]

    for day, day_lessons in group_by_day(lessons):
        counters = _day_counters(day_lessons)
        header = f"{DIVIDER}\n{day_header(day, today)}"
        if counters:
            header += f"\n{counters}"
        header += f"\n{DIVIDER}"
        blocks.append(header)
        for lesson in day_lessons:
            blocks.append(format_lesson(
                lesson,
                role="manager",
                teachers=teachers,
                customers=customers,
                hw_files_count=hw_counts.get(str(lesson.get("id")), 0),
            ))

    return split_messages(blocks)


def build_schedule(
    lessons: Sequence[Dict[str, Any]],
    *,
    role: str,
    title: str = "Расписание",
    teachers: Optional[Dict[int, str]] = None,
    customers: Optional[Dict[int, str]] = None,
    empty_text: str = "На этот период занятий нет.",
    today: Optional[date] = None,
    note: str = "",
) -> List[str]:
    """Расписание с группировкой по дням. Возвращает список сообщений."""
    today = today or settings.today()

    if not lessons:
        return [f"📅 <b>{esc(title)}</b>\n\n{esc(empty_text)}"]

    head = f"📅 <b>{esc(title)}</b>\nВсего занятий: {len(lessons)}"
    if note:
        head += f"\n<i>{esc(note)}</i>"

    blocks: List[str] = [head]
    for day, day_lessons in group_by_day(lessons):
        blocks.append(f"{DIVIDER}\n{day_header(day, today)}\n{DIVIDER}")
        for lesson in day_lessons:
            blocks.append(format_lesson(
                lesson, role=role, teachers=teachers, customers=customers,
            ))
    return split_messages(blocks)


def format_reminder(
    lesson: Dict[str, Any],
    *,
    when: str,
    role: str,
    customers: Optional[Dict[int, str]] = None,
    today: Optional[date] = None,
) -> str:
    """Напоминание об уроке. Дата подставляется, только если урок не сегодня."""
    customers = customers or {}
    today = today or settings.today()
    lesson_day = parse_lesson_date(lesson)

    head = "Урок" if role == "teacher" else "Занятие"
    lines = [f"⏰ <b>{head} через {esc(when)}</b>", ""]

    if lesson_day and lesson_day != today:
        lines.append(f"📅 <b>Дата:</b> {esc(fmt_date_short(lesson_day))}")
    lines.append(f"🕐 <b>Время:</b> {esc(fmt_time_range(lesson))}")

    if role == "teacher":
        names = [customers.get(cid, f"ID:{cid}") for cid in lesson.get("customer_ids") or []]
        if names:
            lines.append(f"👤 <b>Ребёнок:</b> {esc(', '.join(names))}")

    topic = (lesson.get("topic") or "").strip()
    if topic:
        lines.append(f"📝 <b>Тема:</b> {esc(topic)}")

    return "\n".join(lines)


# ==================== ОТПРАВКА СООБЩЕНИЙ ====================
#
# MAX: не более 2 сообщений/сек в один диалог (документировано).
# Клиент max_api/client.py уже ретраит 429/5xx с бэкоффом — здесь только
# защита "один неотправленный блок не должен обрывать всю рассылку".

async def safe_call(factory, *, attempts: int = 2):
    """
    factory — функция без аргументов, возвращающая корутину
    (её нельзя переиспользовать между попытками, поэтому именно фабрика).
    """
    for attempt in range(attempts):
        try:
            return await factory()
        except Exception as e:
            if attempt == attempts - 1:
                logger.warning(f"⚠️ Не удалось отправить сообщение: {e}")
                return None
            await asyncio.sleep(1.0)
    return None


async def answer_blocks(message: Any, blocks: Sequence[str]) -> None:
    """Отправить список блоков ответом на сообщение (объект Msg)."""
    for i, block in enumerate(blocks):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda b=block: message.answer(b, parse_mode="HTML"))


async def send_blocks(bot: Any, user_id: int, blocks: Sequence[str]) -> None:
    """Отправить список блоков конкретному пользователю (для планировщика и рассылок)."""
    for i, block in enumerate(blocks):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda b=block: bot.send_message(user_id=user_id, text=b, fmt="html"))


# ==================== ОКНО ЗАМОРОЗКИ ====================
#
# Заморозить занятие (и уважительно, и беспричинно) можно только пока до
# его начала осталось не меньше settings.FREEZE_DEADLINE_HOURS часов.
# Проверка живёт здесь, а не в parent.py, потому что нужна в трёх местах:
# при отрисовке карточки расписания, при нажатии любой кнопки заморозки
# и в планировщике при поиске сгоревших занятий.


def freeze_deadline(lesson: Dict[str, Any]) -> Optional[datetime]:
    """Момент, после которого заморозка уже невозможна. None — если у
    занятия не удалось определить дату/время."""
    start = parse_lesson_datetime(lesson)
    if not start:
        return None
    return start - timedelta(hours=settings.FREEZE_DEADLINE_HOURS)


def can_freeze(lesson: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """
    Успевает ли родитель заморозить это занятие.

    Занятия без распознанной даты/времени НЕ блокируются: лучше
    пропустить заморозку, которую потом разберёт менеджер, чем молча
    отказать из-за кривой записи в CRM.

    Отказ пишется в лог вместе с исходными полями занятия. Расхождение
    часовых поясов между хостом (UTC) и филиалом проявляется именно
    здесь — как «заморозка не работает вообще», — и без этих цифр
    отличить смещение времени от честного «уже поздно» невозможно.
    """
    deadline = freeze_deadline(lesson)
    if deadline is None:
        logger.info(
            f"🕐 Заморозка разрешена без проверки времени: у занятия "
            f"{lesson.get('id')!r} не распознаны дата/время "
            f"(date={lesson.get('date')!r}, time_from={lesson.get('time_from')!r})"
        )
        return True

    moment = now or settings.now()
    allowed = moment < deadline
    if not allowed:
        hours = (moment - deadline).total_seconds() / 3600
        logger.info(
            f"🕐 Заморозка занятия {lesson.get('id')!r} отклонена: "
            f"начало {freeze_deadline(lesson) + timedelta(hours=settings.FREEZE_DEADLINE_HOURS)}, "
            f"дедлайн {deadline}, сейчас {moment} (TZ={settings.TIMEZONE}), "
            f"просрочено на {hours:.1f} ч; "
            f"date={lesson.get('date')!r}, time_from={lesson.get('time_from')!r}"
        )
    return allowed


def hours_word(n: int) -> str:
    """час / часа / часов."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "часов"
    return {1: "час", 2: "часа", 3: "часа", 4: "часа"}.get(n % 10, "часов")


def freeze_deadline_hint() -> str:
    """Человеческая формулировка правила — одинаковая во всех сообщениях."""
    hours = settings.FREEZE_DEADLINE_HOURS
    shown = int(hours) if float(hours).is_integer() else hours
    if isinstance(shown, int):
        return f"{shown} {hours_word(shown)}"
    return f"{shown} ч"


def freeze_when(freeze: Dict[str, Any]) -> str:
    """
    Дата занятия по записи о заморозке, в человеческом виде.

    Дата берётся из lesson_date, а при его отсутствии — из самого
    lesson_id: у повторяющихся занятий id синтетический и уже содержит
    дату ("116:2026-09-08"). Именно поэтому у части заморозок дата
    показывалась как «—»: поле осталось пустым (например, у записей,
    созданных до появления этого поля), хотя дата всё это время лежала
    рядом, в идентификаторе.
    """
    raw = (freeze.get("lesson_date") or "").strip()
    if not raw:
        lesson_id = str(freeze.get("lesson_id") or "")
        tail = lesson_id.rpartition(":")[2]
        if len(tail) == 10 and tail.count("-") == 2:
            raw = tail

    day = _parse_date_str(raw)
    if not day:
        return "—"
    return f"{day.strftime('%d.%m.%Y')} ({WEEK_SHORT[day.weekday()]})"
