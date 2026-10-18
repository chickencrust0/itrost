"""
bot/handlers/parent.py — расписание, ДЗ, баланс, заявка на перенос со
стороны родителя.

Портировано из alfacrm-bot: пункты меню теперь callback_query
(F.data == "menu:parent:...") вместо F.text == "<подпись кнопки>" —
см. докстринг bot/handlers/teacher.py про отсутствие reply-клавиатуры в MAX.
"""

import asyncio
import logging
from datetime import date, timedelta

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    fmt_date_short,
    fmt_db_time,
    parse_lesson_date,
    answer_blocks,
    build_schedule,
    esc,
    format_homework_card,
    lesson_sort_key,
    format_lesson,  # noqa: F401  (используется через build_schedule)
    safe_call,
)
from bot.handlers.common import fetch_lessons, get_lesson_snapshot, manager_ids
from bot.dispatcher import F, Router
from bot.states import ParentDateRangeStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    contact_admin_keyboard,
    parent_schedule_period_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="parent")


def _lesson_word(n: int) -> str:
    """занятие / занятия / занятий — иначе «Осталось 5 занятие»."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "занятий"
    return {1: "занятие", 2: "занятия", 3: "занятия", 4: "занятия"}.get(n % 10, "занятий")


def _parent(db: Database, max_user_id: int):
    user = db.get_user(max_user_id)
    return user if user and user["role"] == "parent" else None


async def _crm_error(callback: Callback, error: Exception) -> None:
    """
    Сбой CRM — словами, а не текстом исключения.

    Раньше сюда подставлялся str(e), и родителю в чат уезжало
    «❌ Ошибка: HTTP 404 на client/load: <!DOCTYPE html> <html> <head>…».
    Техническая подробность нужна в логе, а человеку — понятная фраза.
    """
    logger.error(f"Сбой CRM для {callback.from_user.id}: {error}")
    await callback.message.answer(
        "⚠️ Не удалось получить данные из системы школы.\n\n"
        "Попробуйте ещё раз через несколько минут. "
        "Если не заработает — напишите менеджеру.",
        parse_mode="HTML",
    )


async def _deny(callback: Callback, db: Database) -> None:
    """
    Объяснить, почему раздел недоступен.

    Раньше здесь был только всплывающий ответ на кнопку
    (callback.answer). В MAX он показывается мельком или не показывается
    вовсе, и снаружи это выглядело как «кнопка не работает». Теперь
    приходит обычное сообщение с причиной и способом её устранить.
    """
    user = db.get_user(callback.from_user.id)
    await callback.answer()

    if not user:
        await callback.message.answer(
            "🔒 Вы не авторизованы в боте.\n\n"
            "Отправьте /start и войдите по номеру телефона, "
            "записанному у нас в CRM.",
            parse_mode="HTML",
        )
        return

    roles = {"teacher": "преподавателя", "manager": "менеджера"}
    role = roles.get(user["role"], user["role"])
    await callback.message.answer(
        f"🔒 Этот раздел только для родителей, а вы вошли как {esc(role)}.\n\n"
        "Отправьте /start, чтобы открыть своё меню.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu:parent:schedule")
async def parent_schedule_menu(
    callback: Callback, db: Database, state: FSMContext
) -> None:
    """
    Выбор периода — такой же, как у преподавателя и в сводке менеджера.

    Раньше родителю жёстко показывалась неделя вперёд, и посмотреть
    «что сегодня» или «что в следующем месяце» было нельзя вовсе.
    """
    if not _parent(db, callback.from_user.id):
        await _deny(callback, db)
        return
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "📅 <b>Выберите период:</b>",
        reply_markup=parent_schedule_period_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "pschedule:custom")
async def parent_schedule_custom(
    callback: Callback, db: Database, state: FSMContext
) -> None:
    if not _parent(db, callback.from_user.id):
        await _deny(callback, db)
        return
    await state.set_state(ParentDateRangeStates.waiting_for_date_from)
    await callback.answer()
    await callback.message.answer(
        "📅 Введите начальную дату в формате <b>ГГГГ-ММ-ДД</b>\n"
        f"Пример: <code>{settings.today().isoformat()}</code>",
        parse_mode="HTML",
    )


@router.message(ParentDateRangeStates.waiting_for_date_from, F.text)
async def parent_schedule_date_to(message: Msg, state: FSMContext) -> None:
    if not _parse_iso(message.text):
        await message.answer(
            "❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML"
        )
        return
    await state.update_data(date_from=message.text.strip())
    await state.set_state(ParentDateRangeStates.waiting_for_date_to)
    await message.answer(
        "📅 Введите конечную дату в формате <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML"
    )


@router.message(ParentDateRangeStates.waiting_for_date_to, F.text)
async def parent_schedule_custom_show(
    message: Msg, state: FSMContext, db: Database,
    crm: AlfaCRMClient, cache: LessonCache,
) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        await state.clear()
        return
    if not _parse_iso(message.text):
        await message.answer(
            "❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML"
        )
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    date_to = message.text.strip()
    await state.clear()

    if not date_from:
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await _show_parent_schedule(message, db, crm, cache, user, date_from, date_to)


@router.callback_query(F.data.startswith("pschedule:"))
async def parent_schedule_period(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return

    period = callback.data.split(":")[1]
    today = settings.today()
    if period == "today":
        date_from = date_to = today.isoformat()
    elif period == "tomorrow":
        date_from = date_to = (today + timedelta(days=1)).isoformat()
    elif period == "week":
        date_from, date_to = today.isoformat(), (today + timedelta(days=7)).isoformat()
    elif period == "month":
        date_from, date_to = today.isoformat(), (today + timedelta(days=30)).isoformat()
    else:
        await callback.answer()
        return

    await callback.answer()
    await _show_parent_schedule(
        callback.message, db, crm, cache, user, date_from, date_to
    )


def _parse_iso(text: str):
    try:
        return date.fromisoformat((text or "").strip())
    except (ValueError, AttributeError):
        return None


async def _show_parent_schedule(
    message, db: Database, crm: AlfaCRMClient, cache: LessonCache,
    user, date_from: str, date_to: str,
) -> None:
    title = (
        f"Расписание на {fmt_date_short(date.fromisoformat(date_from))}"
        if date_from == date_to
        else (
            f"Расписание: {fmt_date_short(date.fromisoformat(date_from))}"
            f" – {fmt_date_short(date.fromisoformat(date_to))}"
        )
    )

    try:
        lessons = await fetch_lessons(
            crm, cache, db=db,
            customer_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except AlfaCRMError as e:
        logger.error(f"Ошибка загрузки расписания: {e}")
        await message.answer(
            "❌ CRM сейчас не отвечает. Попробуйте позже.", parse_mode="HTML"
        )
        return

    lessons = [l for l in lessons if l.get("status") != STATUS_CANCELLED]

    if not lessons:
        await message.answer(
            f"📅 <b>{esc(title)}</b>\n\n"
            "Занятий в этот период нет.\n\n"
            "<i>Если занятия должны быть — возможно, ученик не привязан к группе "
            "в CRM. Напишите администратору.</i>",
            parse_mode="HTML",
        )
        return

    # Расписание — просто расписание. Кнопки заморозки из карточек
    # убраны: родителю было неочевидно, что заморозка живёт под
    # занятием, а из-за них каждое занятие приходилось слать отдельным
    # сообщением. Заморозка теперь в своём пункте меню
    # («❄️ Заморозить занятие»), где сразу видно и остаток заморозок.
    blocks = build_schedule(
        lessons,
        role="parent",
        title=f"{title} ({len(lessons)})",
        today=settings.today(),
    )
    await answer_blocks(message, blocks)


@router.callback_query(F.data == "menu:parent:homework")
async def parent_homework(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return
    await callback.answer()

    today = settings.today()
    date_from = (today - timedelta(days=14)).isoformat()

    try:
        # Период указывается всегда — без него уходил бы запрос вообще
        # без ограничений, выгружая всю историю занятий ученика ради
        # двух недель ДЗ.
        #
        # Фильтра по статусу здесь БОЛЬШЕ НЕТ: раньше стоял
        # status=STATUS_CONDUCTED, но AlfaCRM не хранит статус занятия
        # вовсе, и все занятия числились запланированными — условие не
        # выполнялось никогда, и раздел ДЗ был пуст при любом ДЗ.
        # Показываем всё, у чего есть текст ДЗ или файлы.
        lessons = await fetch_lessons(
            crm, cache, db=db,
            customer_id=user["crm_id"],
            date_from=date_from,
            date_to=today.isoformat(),
        )
    except AlfaCRMError as e:
        await _crm_error(callback, e)
        return

    # ДЗ теперь адресное: у ребёнка может быть своё задание, отличное от
    # группового. Подставляем персональное поверх того, что пришло из
    # lesson_notes — get_lesson_homework сам откатывается на групповое,
    # если персонального нет.
    for lesson in lessons:
        if lesson.get("id"):
            personal = db.get_lesson_homework(lesson["id"], user["crm_id"])
            if personal.strip():
                lesson["homework"] = personal

    lessons_with_hw = [
        l for l in lessons
        if (l.get("homework") or "").strip()
        or db.get_homework_files(l.get("id"), user["crm_id"])
    ]
    if not lessons_with_hw:
        await callback.message.answer("📚 Домашних заданий за последние 2 недели нет.")
        return

    await callback.message.answer(
        f"📚 <b>Домашние задания</b> ({len(lessons_with_hw)})", parse_mode="HTML"
    )

    for lesson in sorted(lessons_with_hw, key=lesson_sort_key, reverse=True):
        files = (
            db.get_homework_files(lesson["id"], user["crm_id"]) if lesson.get("id") else []
        )
        card = format_homework_card(lesson, files_count=len(files), today=today)

        await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda c=card: callback.message.answer(c, parse_mode="HTML"))

        for f in files:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
            att_type = "image" if f.get("file_type") == "photo" else "file"
            try:
                await safe_call(lambda token=f["file_id"], t=att_type: callback.bot.send_message(
                    user_id=callback.from_user.id,
                    attachments=[{"type": t, "payload": {"token": token}}],
                ))
            except Exception as e:
                logger.warning(f"Не удалось отправить файл ДЗ {f.get('file_id')}: {e}")


@router.callback_query(F.data == "menu:parent:balance")
async def parent_balance(callback: Callback, db: Database, crm: AlfaCRMClient) -> None:
    """
    ОСТАТОК ЗАНЯТИЙ В АБОНЕМЕНТЕ.

    Слово «баланс» из интерфейса убрано: денег бот не показывает вовсе
    (client.deposit в этой CRM не используется), а «баланс» заставлял
    родителей искать здесь оплату. Считается по trainingsLeft всех
    действующих абонементов — см. alfacrm_client.get_subscriptions.

    Дата окончания абонемента здесь НЕ показывается: она сбивала с толку
    там, где вопрос ровно один — сколько занятий осталось.
    """
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return
    await callback.answer()

    try:
        customer = await crm.get_customer_info(user["crm_id"])
    except AlfaCRMError as e:
        await _crm_error(callback, e)
        return

    if not customer:
        await callback.message.answer("❌ Не удалось получить данные.")
        return

    subscriptions = customer.get("subscriptions") or []
    total_left = int(customer.get("lessons_left") or 0)

    if not subscriptions:
        await callback.message.answer(
            "🎟 <b>Абонемент</b>\n\n"
            "Действующих абонементов не найдено.\n"
            "<i>Если абонемент оплачен — напишите менеджеру, возможно он ещё "
            "не проведён в CRM.</i>",
            parse_mode="HTML",
        )
        return

    lines = [
        "🎟 <b>Остаток занятий</b>\n",
        f"<b>Всего осталось: {total_left}</b> {_lesson_word(total_left)}\n",
    ]

    for sub in subscriptions:
        lines.append(f"\n📦 <b>{esc(sub['name'])}</b>")
        # Только остаток. «4 из 4 (проведено 0)» родителю ничего не
        # даёт: вопрос у него ровно один — сколько занятий осталось.
        # Полный размер абонемента и число проведённых только загромождают
        # строку и заставляют вычитать в уме.
        lines.append(
            f"   🎟 Осталось: <b>{sub['left']}</b> {_lesson_word(sub['left'])}"
        )
        if sub.get("frozen"):
            lines.append("   ❄️ Заморожен")

    if total_left <= settings.LOW_BALANCE_THRESHOLD:
        lines.append("\n⚠️ <b>Занятия заканчиваются.</b> Пора продлить абонемент.")

    await callback.message.answer("\n".join(lines), parse_mode="HTML")


