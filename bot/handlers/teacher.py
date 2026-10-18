"""
bot/handlers/teacher.py — расписание, отчёт, закрытие урока, ДЗ, заявка
на перенос со стороны преподавателя.

Портировано из alfacrm-bot. Изменения:
  * пункты меню («📅 Моё расписание» и т.п.) были кнопками постоянной
    reply-клавиатуры и матчились по тексту — в MAX это inline-кнопки
    меню, поэтому здесь они callback_query с data="menu:teacher:...";
  * все inline-клавиатуры собираются через max_api.keyboards;
  * message.bot.send_message(...) вызывается с именованными аргументами
    MaxBot вместо позиционных aiogram.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    STATUS_CANCELLED,
    answer_blocks,
    build_schedule,
    day_header,
    esc,
    format_lesson,
    group_by_day,
    lesson_sort_key,
    parse_lesson_date,
    safe_call,
)
from bot.handlers.common import (
    fetch_lessons,
    get_lesson_snapshot,
    load_customer_map,
    manager_ids,
)
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import DateRangeStates, HomeworkStates, TeacherTransferStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    contact_admin_keyboard,
    homework_targets_keyboard,
    lesson_action_keyboard,
    lesson_attendance_keyboard,
    schedule_period_keyboard,
    transfer_decision_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="teacher")


def _teacher(db: Database, max_user_id: int):
    user = db.get_user(max_user_id)
    return user if user and user["role"] == "teacher" else None


def _parse_iso(text: str):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# ==================== ВЫБОР ПЕРИОДА ====================

@router.callback_query(F.data == "menu:teacher:schedule")
async def teacher_schedule_menu(callback: Callback, db: Database, state: FSMContext) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Только для преподавателей.")
        return
    await state.clear()
    await callback.message.answer(
        "📅 <b>Выберите период:</b>", reply_markup=schedule_period_keyboard(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "schedule:custom")
async def custom_date_from(callback: Callback, db: Database, state: FSMContext) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Только для преподавателей.")
        return
    await state.set_state(DateRangeStates.waiting_for_date_from)
    await callback.message.edit_text(
        "📅 Введите начальную дату в формате <b>ГГГГ-ММ-ДД</b>\n"
        f"Пример: <code>{settings.today().isoformat()}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DateRangeStates.waiting_for_date_from, F.text)
async def custom_date_to(message: Msg, state: FSMContext) -> None:
    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
        return
    await state.update_data(date_from=message.text.strip())
    await state.set_state(DateRangeStates.waiting_for_date_to)
    await message.answer("📅 Введите конечную дату в формате <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")


@router.message(DateRangeStates.waiting_for_date_to, F.text)
async def show_custom_schedule(
    message: Msg,
    state: FSMContext,
    db: Database,
    crm: AlfaCRMClient,
    cache: LessonCache,
) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
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

    await show_schedule(message, crm, cache, user, date_from, date_to, db=db)


@router.callback_query(F.data.startswith("schedule:"))
async def handle_schedule_period(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для преподавателей.")
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
    await show_schedule(callback.message, crm, cache, user, date_from, date_to, db=db)


async def show_schedule(
    message,
    crm: AlfaCRMClient,
    cache: LessonCache,
    user,
    date_from: str,
    date_to: str,
    db: Database = None,
) -> None:
    """
    Короткий период — карточки с кнопками действий.
    Длинный — сгруппированный текст: сотня сообщений подряд упирается
    в лимит MAX (2 сообщения/сек в диалог), и часть просто не доходит.
    """
    try:
        lessons = await fetch_lessons(
            crm, cache, db=db,
            teacher_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка получения расписания: {esc(str(e))}", parse_mode="HTML")
        return

    if not lessons:
        hint = "" if cache.is_ready() else "\n\n<i>Кеш ещё обновляется, попробуйте через минуту.</i>"
        await message.answer(
            f"📅 Нет уроков в период с <b>{esc(date_from)}</b> по <b>{esc(date_to)}</b>{hint}",
            parse_mode="HTML",
        )
        return

    customers = await load_customer_map(crm)
    today = settings.today()

    if len(lessons) > settings.MAX_LESSON_CARDS:
        blocks = build_schedule(
            lessons,
            role="teacher",
            title=f"Расписание {date_from} – {date_to}",
            customers=customers,
            today=today,
            note="Кнопки действий доступны при выборе более короткого периода.",
        )
        await answer_blocks(message, blocks)
        return

    await message.answer(
        f"📅 <b>Расписание</b>\n"
        f"Период: {esc(date_from)} – {esc(date_to)}\n"
        f"Всего уроков: <b>{len(lessons)}</b>",
        parse_mode="HTML",
    )

    for day, day_lessons in group_by_day(lessons):
        await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda d=day: message.answer(day_header(d, today), parse_mode="HTML"))
        for lesson in day_lessons:
            card = format_lesson(lesson, role="teacher", customers=customers)
            # Ученики занятия: у каждого кнопки «пришёл» и «не пришёл».
            students = [
                (cid, customers.get(cid, f"ID:{cid}"))
                for cid in lesson.get("customer_ids") or []
            ]
            if students and lesson.get("id"):
                absent_ids = set(db.get_absent_client_ids(lesson["id"])) if db else set()
                keyboard = lesson_attendance_keyboard(
                    lesson["id"], students, absent=absent_ids
                )
            elif lesson.get("id"):
                # Учеников в занятии нет — отмечать некого, но ДЗ и перенос
                # преподавателю всё равно нужны. Отметки «проведено» в этой
                # клавиатуре больше нет (см. max_api/keyboards.py).
                keyboard = lesson_action_keyboard(lesson["id"])
            else:
                keyboard = None
            await asyncio.sleep(settings.MAX_SEND_DELAY)
            await safe_call(
                lambda c=card, k=keyboard: message.answer(c, parse_mode="HTML", reply_markup=k)
            )


# ==================== ОТМЕТКА ПОСЕЩЕНИЯ ====================
#
# Пишется в поле details сущности lesson обычным lesson/update — это
# документированная схема AlfaCRM, обычный API-токен, никаких обходных
# путей.
#
# В боте «Колибри» на impulseCRM этот же раздел ходил во внутренний API
# веб-интерфейса (check_visits/check), найденный разбором фронтенда: он
# не принимал API-ключ и требовал вручную подставлять cookie сессии
# браузера, которую надо было периодически обновлять. Отсюда исчезли
# сразу и флаг включения механизма, и подбор абонемента, и вычисление
# minutesBegin, и timestamp даты — в AlfaCRM занятие адресуется своим
# id, и этого достаточно.

async def _reply(callback: Callback, text: str) -> None:
    """
    Ответ на нажатие кнопки, который человек ТОЧНО увидит.

    callback.answer() показывает всплывающее уведомление — в MAX оно
    короткое и легко пропускается, из-за чего ранние выходы из
    обработчика выглядели как «кнопка не работает». Поэтому важные
    ответы дублируются обычным сообщением в чат.
    """
    await callback.answer(text)
    try:
        await callback.message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не удалось отправить ответ в чат: {e}")


async def _toggle_attendance(
    callback: Callback,
    db: Database,
    crm: AlfaCRMClient,
    cache: LessonCache,
    *,
    present: bool,
) -> None:
    action = "присутствие" if present else "снятие отметки"
    logger.info(
        f"▶️ Отметка посещения ({action}): payload={callback.data!r} "
        f"от {callback.from_user.id}"
    )

    if not _teacher(db, callback.from_user.id):
        logger.info(f"⛔ {callback.from_user.id} не преподаватель — отметка отклонена")
        await _reply(callback, "❌ Доступно только преподавателям.")
        return

    # callback.data: att:<lesson_id>:<client_id>, где lesson_id у
    # повторяющегося занятия сам содержит двоеточие ("116:2026-09-01"),
    # поэтому client_id отделяем справа.
    payload = callback.data.split(":", 1)[1]
    lesson_id, _, client_id_raw = payload.rpartition(":")
    if not lesson_id or not client_id_raw:
        logger.warning(f"⚠️ Не разобран payload отметки: {callback.data!r}")
        await _reply(callback, "❌ Не разобрал занятие/ученика.")
        return

    try:
        client_id = int(client_id_raw)
    except ValueError:
        client_id = client_id_raw

    lesson = await get_lesson_snapshot(lesson_id, crm, cache, db=db)
    if not lesson:
        logger.warning(f"⚠️ Занятие {lesson_id!r} не найдено ни в кеше, ни в CRM")
        await _reply(callback, "❌ Занятие не найдено.")
        return

    changed = False
    try:
        if present:
            changed = await crm.mark_present(lesson_id, client_id, current=lesson)
        else:
            # СНЯТИЕ отметки возвращает ученика в состояние «не
            # отмечен», а НЕ помечает ему пропуск. Раньше повторное
            # нажатие списывало занятие как неявку — прямо
            # противоположное тому, чего ждёт преподаватель. Списание за
            # неявку живёт отдельно, в кнопке «❌» (см. _toggle_absence).
            changed = await crm.clear_attendance(lesson_id, client_id, current=lesson)
    except AlfaCRMError as e:
        logger.error(f"❌ CRM отклонила отметку посещения {lesson_id!r}/{client_id}: {e}")
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    logger.info(
        f"✅ {'Присутствие отмечено' if present else 'Отметка снята'} "
        f"в CRM: ученик {client_id}, занятие {lesson_id}"
        + ("" if changed else " (значение не изменилось)")
    )

    # Отметка присутствия снимает ранее поставленную неявку и наоборот —
    # ученик не может быть одновременно и на занятии, и в списке неявок.
    if present:
        db.unmark_absent(lesson_id, client_id)

    await _refresh_attendance_card(callback, db, crm, lesson, lesson_id)

    if present:
        text = "✅ Отмечено присутствие"
    elif changed:
        text = "↩️ Отметка снята"
    else:
        # Отметки в CRM не было — снимать было нечего. Молчать нельзя:
        # карточка перерисуется, и преподаватель решит, что снятие
        # прошло, хотя в CRM ничего не изменилось.
        text = "↩️ Отметки в CRM не было — снимать нечего"
    await _reply(callback, text)


def _sched_id(reservation) -> Any:
    sched = reservation.get("schedule")
    return sched.get("id") if isinstance(sched, dict) else sched


def _minutes_of(value) -> Any:
    """'16:00' или '2026-09-08 16:00:00' -> 960 минут от полуночи."""
    text = str(value or "").strip()
    if not text:
        return None
    tail = text.replace("T", " ").split(" ")[-1]
    parts = tail.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


async def _refresh_attendance_card(
    callback: Callback,
    db: Database,
    crm: AlfaCRMClient,
    lesson,
    lesson_id: Any,
) -> None:
    customers = await load_customer_map(crm)
    students = [
        (cid, customers.get(cid, f"ID:{cid}")) for cid in lesson.get("customer_ids") or []
    ]
    marked = await _marked_set(crm, lesson_id)
    absent = set(db.get_absent_client_ids(lesson_id))
    logger.info(
        f"🔄 Перерисовка карточки {lesson_id}: учеников {len(students)}, "
        f"присутствуют {sorted(marked)}, неявки {sorted(absent)}"
    )

    # Сбой перерисовки не должен выглядеть как «кнопка не сработала»:
    # само действие уже выполнено, поэтому ошибку логируем и сообщаем,
    # а не роняем обработчик.
    try:
        await callback.message.edit_text(
            callback.message.text or format_lesson(lesson, role="teacher", customers=customers),
            reply_markup=lesson_attendance_keyboard(
                lesson_id, students, marked, absent
            ),
        )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось обновить карточку занятия {lesson_id}: {e}")


async def _marked_set(crm: AlfaCRMClient, lesson_id) -> set:
    """
    Кто на занятии уже отмечен присутствующим — по данным CRM.

    Читается тем же способом, которым отметка ставится: поле details
    сущности lesson. Единственный источник и единственное правило.

    В боте «Колибри» здесь была ВТОРАЯ копия правила сопоставления
    (visit -> абонемент -> клиент), и именно на ней всё сломалось:
    карточка отметку видела, а снятие — нет, потому что копии разошлись.
    В AlfaCRM сопоставлять нечего — customer_id лежит прямо в записи.

    Если CRM недоступна, возвращаем пустое множество: кнопки просто
    останутся неотмеченными, а не покажут неправду.
    """
    try:
        attendance = await crm.get_attendance(lesson_id)
    except Exception as e:
        logger.warning(f"Не удалось прочитать посещаемость занятия {lesson_id}: {e}")
        return set()
    marked = set()
    for cid, is_attend in attendance.items():
        if is_attend:
            # id ученика в кнопках — число, а в ответе CRM бывает
            # строкой; без приведения множество никогда не совпадёт.
            marked.add(int(cid) if str(cid).lstrip("-").isdigit() else cid)
    return marked


@router.callback_query(F.data.startswith("att:"))
async def mark_attendance(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    await _toggle_attendance(callback, db, crm, cache, present=True)


@router.callback_query(F.data.startswith("unatt:"))
async def unmark_attendance(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    await _toggle_attendance(callback, db, crm, cache, present=False)


# ==================== НЕЯВКА («не пришёл») ====================
#
# Неявка НЕ пишется в CRM сразу: в AlfaCRM нет статуса «не пришёл»,
# есть только списание занятия (burn_one). Решение списывать или нет —
# за менеджером, поэтому отметка преподавателя живёт в БД бота и
# попадает в вечернюю сводку менеджеру (см. scheduler.send_absence_digest).

async def _toggle_absence(
    callback: Callback,
    db: Database,
    crm: AlfaCRMClient,
    cache: LessonCache,
    *,
    absent: bool,
) -> None:
    logger.info(
        f"▶️ Неявка ({'отметить' if absent else 'снять'}): "
        f"payload={callback.data!r} от {callback.from_user.id}"
    )

    if not _teacher(db, callback.from_user.id):
        logger.info(f"⛔ {callback.from_user.id} не преподаватель — неявка отклонена")
        await _reply(callback, "❌ Доступно только преподавателям.")
        return

    payload = callback.data.split(":", 1)[1]
    lesson_id, _, client_id_raw = payload.rpartition(":")
    if not lesson_id or not client_id_raw:
        logger.warning(f"⚠️ Не разобран payload отметки: {callback.data!r}")
        await _reply(callback, "❌ Не разобрал занятие/ученика.")
        return
    try:
        client_id = int(client_id_raw)
    except ValueError:
        await callback.answer("❌ Некорректный ID ученика.")
        return

    lesson = await get_lesson_snapshot(lesson_id, crm, cache, db=db)
    if not lesson:
        logger.warning(f"⚠️ Занятие {lesson_id!r} не найдено ни в кеше, ни в CRM")
        await _reply(callback, "❌ Занятие не найдено.")
        return

    lesson_day = parse_lesson_date(lesson)
    if not lesson_day:
        await _reply(callback, "❌ У занятия не определена дата.")
        return

    if absent:
        customers = await load_customer_map(crm)
        db.mark_absent(
            lesson_id,
            client_id,
            customers.get(client_id, f"ID:{client_id}"),
            lesson_day.isoformat(),
            callback.from_user.id,
        )
    else:
        db.unmark_absent(lesson_id, client_id)

    # Неявка пишется и в CRM: у детали урока есть is_attend=0, и без
    # этого пропуск не списался бы с абонемента, а администратор видел
    # бы занятие незакрытым. Локальная отметка уже сделана выше и
    # остаётся, даже если CRM недоступна: отчёт преподавателя и вечерняя
    # сводка менеджеру строятся по базе бота.
    try:
        if absent:
            await crm.mark_absent(lesson_id, client_id, current=lesson)
        else:
            await crm.clear_attendance(lesson_id, client_id, current=lesson)
    except AlfaCRMError as e:
        logger.warning(
            f"⚠️ Неявка ученика {client_id} на {lesson_id!r} не записана в CRM: {e}"
        )

    logger.info(
        f"{'❌ Неявка отмечена' if absent else '↩️ Неявка снята'}: "
        f"клиент {client_id}, {lesson_id}"
    )
    await _refresh_attendance_card(callback, db, crm, lesson, lesson_id)
    await _reply(callback, "❌ Отмечена неявка" if absent else "↩️ Неявка снята")


@router.callback_query(F.data.startswith("abs:"))
async def mark_absent(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    await _toggle_absence(callback, db, crm, cache, absent=True)


@router.callback_query(F.data.startswith("unabs:"))
async def unmark_absent(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    await _toggle_absence(callback, db, crm, cache, absent=False)


# ==================== ОТЧЁТ ====================

@router.callback_query(F.data == "menu:teacher:report")
async def teacher_report(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для преподавателей.")
        return
    await callback.answer()

    today = settings.today()
    date_from = (today - timedelta(days=30)).isoformat()

    try:
        lessons = await fetch_lessons(
            crm, cache, db=db,
            teacher_id=user["crm_id"], date_from=date_from, date_to=today.isoformat(),
        )
    except AlfaCRMError as e:
        await callback.message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    # Отчёт строится по посещаемости, а не по отметкам «проведено»:
    # преподаватель их больше не ставит (см. close_lesson_obsolete).
    total = len(lessons)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    actual = [l for l in lessons if l.get("status") != STATUS_CANCELLED]
    with_hw = sum(1 for l in lessons if (l.get("homework") or "").strip())

    # Неявки лежат в БД бота (в AlfaCRM статуса «не пришёл» нет), а
    # присутствия — в самой CRM. Полная сверка присутствий за 30 дней
    # стоила бы десятков запросов к check_visits, поэтому в отчёте
    # показываем то, что считается дёшево и достоверно.
    absences = 0
    for lesson in actual:
        if lesson.get("id"):
            absences += len(db.get_absent_client_ids(lesson["id"]))

    await callback.message.answer(
        f"📊 <b>Отчёт за 30 дней</b>\n"
        f"Период: {date_from} – {today.isoformat()}\n\n"
        f"📅 <b>Всего занятий:</b> {total}\n"
        f"❌ <b>Отменено:</b> {cancelled}\n"
        f"🚫 <b>Отмечено неявок:</b> {absences}\n\n"
        f"📚 <b>С ДЗ:</b> {with_hw}\n"
        f"📝 <b>Без ДЗ:</b> {max(0, len(actual) - with_hw)}",
        parse_mode="HTML",
    )


# ==================== ЗАКРЫТИЕ УРОКА (ОТКЛЮЧЕНО) ====================
#
# Отметка «занятие проведено» от преподавателя больше не требуется: школе
# нужна только посещаемость конкретных детей, а «проведено» дублировало
# её и создавало лишний шаг. Кнопка убрана из max_api/keyboards.py, но
# обработчик оставлен: сообщения со старыми кнопками живут в истории чата
# сколько угодно долго, и без него нажатие уходило бы в пустоту.

@router.callback_query(F.data.startswith("close:"))
async def close_lesson_obsolete(callback: Callback, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return
    await callback.answer()
    await callback.message.answer(
        "ℹ️ Отмечать занятие как проведённое больше не нужно.\n\n"
        "Достаточно отметить, кто из детей пришёл, а кто нет — "
        "кнопками под карточкой занятия.",
        parse_mode="HTML",
    )


# ==================== ДОМАШНЕЕ ЗАДАНИЕ ====================

@router.callback_query(F.data.startswith("hw:"))
async def attach_hw_start(
    callback: Callback, state: FSMContext, db: Database,
    crm: AlfaCRMClient, cache: LessonCache,
) -> None:
    """
    Шаг 1: кому прикрепить ДЗ.

    Раньше ДЗ было одно на занятие. В группе дети идут разными темпами,
    поэтому теперь педагог сначала выбирает адресата: конкретного ребёнка
    или всю группу сразу. Групповой вариант стоит первым — он самый
    частый, и без него педагогу пришлось бы проходить список по одному.
    """
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    lesson_id = callback.data.partition(":")[2]
    if not lesson_id:
        await callback.answer("❌ Не удалось определить занятие.")
        return

    students = await _lesson_students(lesson_id, crm, cache, db)
    await callback.answer()

    if not students:
        # Учеников в занятии нет — выбирать не из кого, но ДЗ всё равно
        # можно оставить общим: состав может подтянуться позже.
        await state.update_data(lesson_id=lesson_id, hw_client_id=0, hw_target="всей группе")
        await state.set_state(HomeworkStates.waiting_for_text_or_file)
        await callback.message.answer(
            "📝 Учеников в занятии пока нет — ДЗ сохраню как общее для группы.\n\n"
            "Отправьте текст или файл."
        )
        return

    await callback.message.answer(
        "📝 <b>Кому прикрепить домашнее задание?</b>",
        parse_mode="HTML",
        reply_markup=homework_targets_keyboard(lesson_id, students),
    )


@router.callback_query(F.data.startswith("hw_all:"))
async def attach_hw_all(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return
    lesson_id = callback.data.partition(":")[2]
    await state.update_data(lesson_id=lesson_id, hw_client_id=0, hw_target="всей группе")
    await state.set_state(HomeworkStates.waiting_for_text_or_file)
    await callback.answer()
    await callback.message.answer(
        "👥 ДЗ <b>для всей группы</b>.\n\nОтправьте текст или файл.", parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("hw_one:"))
async def attach_hw_one(
    callback: Callback, state: FSMContext, db: Database,
    crm: AlfaCRMClient, cache: LessonCache,
) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    # rsplit, а НЕ split: id занятия сам содержит двоеточие
    # ("hw_one:116:2026-09-01:754") — client_id всегда последний.
    rest = callback.data.partition(":")[2]
    lesson_id, _, client_raw = rest.rpartition(":")
    if not lesson_id or not client_raw.isdigit():
        await callback.answer("❌ Не удалось определить ученика.")
        return

    client_id = int(client_raw)
    students = dict(await _lesson_students(lesson_id, crm, cache, db))
    name = students.get(client_id, f"ID:{client_id}")

    await state.update_data(lesson_id=lesson_id, hw_client_id=client_id, hw_target=name)
    await state.set_state(HomeworkStates.waiting_for_text_or_file)
    await callback.answer()
    await callback.message.answer(
        f"📝 ДЗ для <b>{esc(name)}</b>.\n\nОтправьте текст или файл.", parse_mode="HTML"
    )


@router.callback_query(F.data == "menu:teacher:cancel")
async def teacher_cancel(callback: Callback, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Отменено.")


async def _lesson_students(lesson_id, crm, cache, db):
    """[(client_id, имя)] по занятию — общий помощник для ДЗ и заморозки."""
    lesson = await get_lesson_snapshot(lesson_id, crm, cache, db=db)
    if not lesson:
        return []
    try:
        customers = await load_customer_map(crm)
    except Exception:
        customers = {}
    return [
        (cid, customers.get(cid, f"ID:{cid}"))
        for cid in lesson.get("customer_ids") or []
    ]


@router.message(HomeworkStates.waiting_for_text_or_file, F.text)
async def attach_hw_text(
    message: Msg, state: FSMContext, db: Database, cache: LessonCache
) -> None:
    """
    ДЗ сохраняется в БД бота, а не в CRM: у сущности schedule в
    AlfaCRM нет поля домашнего задания (см. schema.md). Прежняя версия
    отправляла его в CRM, запись молча терялась, и родитель никогда не
    видел ДЗ.

    Адресат берётся из состояния: 0 — всей группе, иначе конкретный
    ученик. Персональное ДЗ перекрывает групповое при показе родителю.
    """
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    client_id = int(data.get("hw_client_id") or 0)
    target = data.get("hw_target") or "всей группе"

    db.set_lesson_homework(lesson_id, client_id, message.text, message.from_user.id)
    if client_id == 0:
        # Групповое ДЗ дублируем в lesson_notes и в кеш — оттуда его
        # читают сводка менеджера и карточки занятий.
        db.set_lesson_note(
            lesson_id, homework=message.text, updated_by=message.from_user.id
        )
        await cache.patch_lesson(lesson_id, {"homework": message.text})

    await message.answer(
        f"✅ ДЗ сохранено ({esc(target)}).\n"
        f"Родители увидят его в разделе «Домашнее задание».",
        parse_mode="HTML",
    )
    await state.clear()


@router.message(HomeworkStates.waiting_for_text_or_file, F.document | F.photo)
async def attach_hw_file(
    message: Msg, state: FSMContext, db: Database, cache: LessonCache
) -> None:
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    if message.document:
        file_token = message.document.token
        file_name = message.document.file_name or "файл"
        file_type = "document"
    else:
        file_token = message.photo.token
        file_name = "фото"
        file_type = "photo"

    client_id = int(data.get("hw_client_id") or 0)
    target = data.get("hw_target") or "всей группе"

    db.add_homework_file(lesson_id, file_token, file_name, file_type, client_id=client_id)

    # Отметка о файле дописывается в текст ДЗ того же адресата, чтобы в
    # карточке было видно, что вложение есть, даже до его загрузки.
    existing_hw = (db.get_lesson_homework(lesson_id, client_id) or "").strip()
    note = f"[📎 {file_name}]"
    new_hw = f"{existing_hw}\n{note}" if existing_hw else note
    db.set_lesson_homework(lesson_id, client_id, new_hw, message.from_user.id)
    if client_id == 0:
        db.set_lesson_note(lesson_id, homework=new_hw, updated_by=message.from_user.id)
        await cache.patch_lesson(lesson_id, {"homework": new_hw})

    await message.answer(f"✅ Файл прикреплён ({esc(target)}).", parse_mode="HTML")
    await state.clear()


# ==================== ЗАЯВКА НА ПЕРЕНОС ====================

@router.callback_query(F.data.startswith("transfer:"))
async def transfer_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.")
        return

    lesson_id = callback.data.partition(":")[2]
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(TeacherTransferStates.waiting_for_comment)
    await callback.message.answer("🔁 Напишите желаемую дату/время и причину переноса.")
    await callback.answer()


@router.message(TeacherTransferStates.waiting_for_comment, F.text)
async def transfer_finish(message: Msg, state: FSMContext, db: Database) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    await state.clear()

    request_id = db.create_transfer_request(
        message.from_user.id, lesson_id, message.text, author_role="teacher"
    )
    sent_at = settings.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in manager_ids(db):
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                user_id=mid,
                text=(
                    f"🔁 <b>Заявка на перенос №{request_id}</b>\n\n"
                    f"📅 <b>Получена:</b> {sent_at}\n"
                    f"👨‍🏫 <b>Преподаватель:</b> {esc(user['full_name'])}\n"
                    f"🆔 <b>Урок ID:</b> {esc(lesson_id or '—')}\n"
                    f"💬 <b>Комментарий:</b> {esc(message.text)}"
                ),
                fmt="html",
                attachments=transfer_decision_keyboard(request_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
