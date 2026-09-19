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
from freezes import FreezeCounter
from bot.formatting import (
    fmt_date_short,
    fmt_db_time,
    parse_lesson_date,
    STATUS_CANCELLED,
    answer_blocks,
    build_schedule,
    can_freeze,
    esc,
    format_homework_card,
    freeze_when,
    freeze_deadline,
    freeze_deadline_hint,
    lesson_sort_key,
    format_lesson,  # noqa: F401  (используется через build_schedule)
    safe_call,
)
from bot.handlers.common import fetch_lessons, get_lesson_snapshot, manager_ids
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import FreezeStates, ParentDateRangeStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    certificate_upload_keyboard,
    contact_admin_keyboard,
    freeze_confirm_keyboard,
    freeze_reason_keyboard,
    lesson_freeze_keyboard,
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
async def parent_balance(callback: Callback, db: Database, freezes: FreezeCounter, crm: AlfaCRMClient) -> None:
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

    # Остаток беспричинных заморозок — вторая величина, которая
    # расходуется у клиента, поэтому показываем рядом с занятиями.
    #
    # Строка выводится ВСЕГДА, даже если прочитать не удалось. Раньше
    # при ошибке блок молча пропадал, и было не отличить «заморозок нет»
    # от «бот не смог их прочитать» — именно так выглядела ситуация,
    # когда число в CRM стояло, а в боте не показывалось.
    lines.append(await _freezes_line(freezes, user["crm_id"]))

    await callback.message.answer("\n".join(lines), parse_mode="HTML")


async def _freezes_line(freezes: FreezeCounter, client_id) -> str:
    """Строка про остаток беспричинных заморозок — одна на все разделы."""
    try:
        left = await freezes.get(client_id)
    except AlfaCRMError as e:
        logger.warning(f"Не удалось получить остаток заморозок {client_id}: {e}")
        return (
            "\n❄️ <b>Беспричинные заморозки:</b> не удалось получить из CRM.\n"
            "<i>Напишите менеджеру — он посмотрит.</i>"
        )
    return (
        f"\n❄️ <b>Беспричинных заморозок осталось: {left}</b>\n"
        f"<i>Заморозить занятие можно не позднее чем за "
        f"{freeze_deadline_hint()} до его начала.</i>"
    )


# ==================== ЗАМОРОЗКИ ====================
#
# Две ветки с разными последствиями:
#   * без причины — расходует одну беспричинную заморозку клиента
#     (счётчик хранится в CRM в поле адреса проживания, см. alfacrm_client);
#   * по уважительной причине — ничего не расходует, но по здоровью
#     ожидается справка, которую родитель прикладывает в этом же разделе.
#
# ОБЩЕЕ ПРАВИЛО ВРЕМЕНИ: заморозить занятие любым способом можно только
# пока до его начала осталось не меньше settings.FREEZE_DEADLINE_HOURS
# часов. Позже занятие сгорает — родителю приходит уведомление об этом
# (scheduler.notify_burned_lessons), а разбирает спорные случаи менеджер.


@router.callback_query(F.data == "menu:parent:freeze")
async def parent_freeze_menu(
    callback: Callback, db: Database, freezes: FreezeCounter, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    """
    Раздел заморозки: сразу остаток заморозок и список занятий, которые
    ЕЩЁ можно заморозить.

    Показываются только доступные для заморозки занятия. Выводить те, по
    которым срок прошёл, значило бы предлагать нажать кнопку и получить
    отказ — ровно та неочевидность, из-за которой заморозку и вынесли из
    расписания в отдельный пункт меню.
    """
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return
    await callback.answer()

    today = settings.today()
    try:
        lessons = await fetch_lessons(
            crm, cache, db=db,
            customer_id=user["crm_id"],
            date_from=today.isoformat(),
            date_to=(today + timedelta(days=settings.FREEZE_LOOKAHEAD_DAYS)).isoformat(),
        )
    except AlfaCRMError as e:
        await _crm_error(callback, e)
        return

    freezable = [
        l for l in lessons
        if l.get("id") and l.get("status") != STATUS_CANCELLED and can_freeze(l)
    ]
    freezable.sort(key=lesson_sort_key)
    freezable = freezable[: settings.MAX_LESSON_CARDS]

    header = "❄️ <b>Заморозка занятия</b>\n" + await _freezes_line(freezes, user["crm_id"])

    if not freezable:
        await callback.message.answer(
            f"{header}\n\n"
            f"Сейчас нет занятий, которые можно заморозить.\n\n"
            f"<i>Заморозка возможна не позднее чем за {freeze_deadline_hint()} "
            f"до начала занятия. Если ситуация особенная — напишите менеджеру.</i>",
            parse_mode="HTML",
            reply_markup=contact_admin_keyboard(),
        )
        return

    await callback.message.answer(
        f"{header}\n\nВыберите занятие, которое нужно заморозить:",
        parse_mode="HTML",
    )

    for lesson in freezable:
        await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda l=lesson: callback.message.answer(
            # show_date=True: карточки заморозки идут сплошным списком,
            # без заголовков дня, и без даты видно только время — понять,
            # какое из занятий морозишь, нельзя.
            format_lesson(l, role="parent", show_date=True),
            parse_mode="HTML",
            reply_markup=lesson_freeze_keyboard(l["id"]),
        ))


async def _too_late(callback: Callback, lesson) -> None:
    """Мягкий отказ: срок заморозки прошёл."""
    deadline = freeze_deadline(lesson)
    when = f" (крайний срок был {esc(deadline.strftime('%d.%m в %H:%M'))})" if deadline else ""
    await callback.answer()
    await callback.message.answer(
        f"⏰ <b>Заморозить это занятие уже не получится.</b>\n\n"
        f"Заморозка возможна не позднее чем за {freeze_deadline_hint()} "
        f"до начала{when}.\n\n"
        f"Если произошла ошибка или ситуация особенная — напишите менеджеру, "
        f"он разберётся.",
        parse_mode="HTML",
        reply_markup=contact_admin_keyboard(),
    )


async def _freeze_lesson_or_deny(callback: Callback, db: Database, crm, cache):
    """
    Общая часть: проверить роль, достать занятие и убедиться, что срок
    заморозки ещё не прошёл.

    Проверка времени делается здесь, а не только при отрисовке кнопок:
    сообщение с расписанием живёт в истории чата сколько угодно долго, и
    кнопка под ним остаётся нажимаемой уже после дедлайна.
    """
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return None, None

    lesson_id = callback.data.partition(":")[2]
    lesson = await get_lesson_snapshot(lesson_id, crm, cache, db=db)
    if not lesson:
        await callback.answer("❌ Занятие не найдено.")
        return None, None

    if not can_freeze(lesson):
        logger.info(
            f"⏰ Заморозка отклонена по времени: клиент {user['crm_id']}, "
            f"занятие {lesson.get('id')}"
        )
        await _too_late(callback, lesson)
        return None, None

    return user, lesson


@router.callback_query(F.data.startswith("pfrz_no:"))
async def freeze_no_reason_confirm(
    callback: Callback, db: Database, freezes: FreezeCounter, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user, lesson = await _freeze_lesson_or_deny(callback, db, crm, cache)
    if not user:
        return

    try:
        left = await freezes.get(user["crm_id"])
    except AlfaCRMError as e:
        await _crm_error(callback, e)
        return

    if left <= 0:
        await callback.answer()
        await callback.message.answer(
            "❄️ <b>Беспричинных заморозок не осталось.</b>\n\n"
            "Заморозить занятие можно по уважительной причине — "
            "или напишите администратору.",
            parse_mode="HTML",
            reply_markup=contact_admin_keyboard(),
        )
        return

    lesson_id = lesson["id"]
    await callback.answer()
    await callback.message.answer(
        f"⚠️ <b>Вы уверены?</b>\n\n"
        f"Это расходует лимит ваших беспричинных заморозок.\n"
        f"Сейчас доступно: <b>{left}</b>.",
        parse_mode="HTML",
        reply_markup=freeze_confirm_keyboard(lesson_id),
    )


@router.callback_query(F.data.startswith("pfrz_cancel:"))
async def freeze_cancel(callback: Callback) -> None:
    await callback.message.edit_text("❌ Заморозка отменена.")
    await callback.answer()


@router.callback_query(F.data.startswith("pfrz_yes:"))
async def freeze_no_reason_apply(
    callback: Callback, db: Database, freezes: FreezeCounter, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user, lesson = await _freeze_lesson_or_deny(callback, db, crm, cache)
    if not user:
        return

    try:
        left = await freezes.spend(user["crm_id"])
    except AlfaCRMError as e:
        await callback.answer()
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    day = parse_lesson_date(lesson)
    await freeze_in_crm(crm, lesson["id"], user["crm_id"])
    db.add_freeze(
        user["crm_id"],
        kind="no_reason",
        client_name=user.get("full_name") or "",
        lesson_id=lesson["id"],
        lesson_date=day.isoformat() if day else "",
        created_by=callback.from_user.id,
        created_by_role="parent",
    )
    logger.info(
        f"❄️ Беспричинная заморозка: клиент {user['crm_id']}, "
        f"занятие {lesson['id']}, осталось {left}"
    )

    await callback.message.edit_text(
        f"❄️ <b>Абонемент заморожен.</b>\n\n"
        f"У вас осталось <b>{left}</b> беспричинных заморозок.",
        parse_mode="HTML",
    )
    await callback.answer("❄️ Заморожено")


@router.callback_query(F.data.startswith("pfrz_ok:"))
async def freeze_valid_reason(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user, lesson = await _freeze_lesson_or_deny(callback, db, crm, cache)
    if not user:
        return
    await callback.answer()
    await callback.message.answer(
        "🙏 <b>Заморозка по уважительной причине</b>\n\nВыберите причину:",
        parse_mode="HTML",
        reply_markup=freeze_reason_keyboard(lesson["id"]),
    )


@router.callback_query(F.data.startswith("pfrz_health:"))
async def freeze_health(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user, lesson = await _freeze_lesson_or_deny(callback, db, crm, cache)
    if not user:
        return

    day = parse_lesson_date(lesson)
    await freeze_in_crm(crm, lesson["id"], user["crm_id"])
    freeze_id = db.add_freeze(
        user["crm_id"],
        kind="valid",
        reason="health",
        client_name=user.get("full_name") or "",
        lesson_id=lesson["id"],
        lesson_date=day.isoformat() if day else "",
        created_by=callback.from_user.id,
        created_by_role="parent",
    )

    await callback.message.answer(
        "🏥 <b>Надеемся, что всё хорошо!</b>\n\n"
        "Выздоравливайте, занятие заморожено — оно не сгорит и лимит "
        "беспричинных заморозок не тронут.\n\n"
        "После выздоровления прикрепите, пожалуйста, фотографию справки "
        "в разделе «❄️ Мои заморозки».",
        parse_mode="HTML",
        reply_markup=certificate_upload_keyboard(freeze_id),
    )
    await callback.answer("🙏 Заморожено")


@router.callback_query(F.data.startswith("pfrz_other:"))
async def freeze_other(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user, lesson = await _freeze_lesson_or_deny(callback, db, crm, cache)
    if not user:
        return

    day = parse_lesson_date(lesson)
    await freeze_in_crm(crm, lesson["id"], user["crm_id"])
    db.add_freeze(
        user["crm_id"],
        kind="valid",
        reason="other",
        client_name=user.get("full_name") or "",
        lesson_id=lesson["id"],
        lesson_date=day.isoformat() if day else "",
        created_by=callback.from_user.id,
        created_by_role="parent",
    )

    await callback.message.answer(
        "📝 <b>Опишите причину администратору</b>\n\n"
        "Напишите, пожалуйста, что произошло — администратор рассмотрит "
        "и подтвердит заморозку.",
        parse_mode="HTML",
        reply_markup=contact_admin_keyboard(),
    )
    await callback.answer()


# ==================== МОИ ЗАМОРОЗКИ (родитель) ====================

REASON_LABELS = {"health": "по состоянию здоровья", "other": "другая причина"}


@router.callback_query(F.data == "menu:parent:freezes")
async def parent_freezes(callback: Callback, db: Database) -> None:
    user = _parent(db, callback.from_user.id)
    if not user:
        await _deny(callback, db)
        return
    await callback.answer()

    items = db.get_freezes_by_client(user["crm_id"])
    if not items:
        await callback.message.answer("❄️ Заморозок пока не было.")
        return

    await callback.message.answer(
        f"❄️ <b>Мои заморозки</b> ({len(items)})", parse_mode="HTML"
    )
    for i, f in enumerate(items[:20]):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        if f["kind"] == "no_reason":
            head = "❄️ Без причины"
        else:
            head = f"🙏 Уважительная ({REASON_LABELS.get(f.get('reason'), '—')})"
        cert = "📎 справка приложена" if f.get("certificate_file_id") else "—"
        keyboard = (
            certificate_upload_keyboard(f["id"])
            if f["kind"] == "valid" and not f.get("certificate_file_id")
            else None
        )
        await safe_call(lambda r=f, h=head, c=cert, kb=keyboard: callback.message.answer(
            f"{h}\n"
            f"📅 Занятие: {esc(freeze_when(r))}\n"
            f"🕐 Оформлено: {esc(fmt_db_time(r.get('created_at')))}\n"
            f"📄 Справка: {c}",
            parse_mode="HTML",
            reply_markup=kb,
        ))


@router.callback_query(F.data.startswith("cert:"))
async def certificate_start(callback: Callback, state: FSMContext, db: Database) -> None:
    freeze_id = int(callback.data.split(":")[1])
    freeze = db.get_freeze(freeze_id)
    if not freeze:
        await callback.answer("Заморозка не найдена.")
        return

    await state.update_data(freeze_id=freeze_id)
    await state.set_state(FreezeStates.waiting_for_certificate)
    await callback.message.answer(
        "📎 Пришлите фотографию или файл справки одним сообщением."
    )
    await callback.answer()


@router.message(FreezeStates.waiting_for_certificate, F.photo | F.document)
async def certificate_upload(
    message: Msg, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    freeze_id = data.get("freeze_id")
    await state.clear()

    freeze = db.get_freeze(freeze_id) if freeze_id else None
    if not freeze:
        await message.answer("❌ Заморозка не найдена, начните заново.")
        return

    if message.photo:
        file_id, file_type = message.photo.token, "photo"
    else:
        file_id, file_type = message.document.token, "document"

    db.attach_certificate(freeze_id, file_id, file_type)
    await message.answer("✅ Справка прикреплена. Спасибо!")

    for manager_id in manager_ids(db):
        await safe_call(lambda mid=manager_id: message.bot.send_message(
            user_id=mid,
            text=(
                f"📄 <b>Приложена справка</b>\n\n"
                f"👤 {esc(freeze.get('client_name') or freeze['client_id'])}\n"
                f"📅 Занятие: {esc(freeze.get('lesson_date') or '—')}"
            ),
            fmt="html",
            attachments=[{
                "type": "image" if file_type == "photo" else "file",
                "payload": {"token": file_id},
            }],
        ))


@router.message(FreezeStates.waiting_for_certificate)
async def certificate_wrong_type(message: Msg) -> None:
    await message.answer("Нужна фотография или файл справки — пришлите его сообщением.")
