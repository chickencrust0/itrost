"""
bot/handlers/manager.py — рассылка, заявки на перенос, сводка за период.

Портировано из alfacrm-bot: пункты меню — callback_query
(F.data == "menu:manager:...") вместо F.text == "<подпись кнопки>".
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from cache import LessonCache
from database import Database
from freezes import FreezeCounter
from bot.formatting import (
    answer_blocks,
    split_messages,
    esc,
    fmt_date_long,
    fmt_db_time,
    freeze_deadline_hint,
    freeze_when,
    safe_call,
)
from bot.handlers.common import (
    fetch_lessons,
    get_lesson_snapshot,
    get_lesson_summary,
    is_manager,
    manager_ids,
)
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import BroadcastStates, ManagerReplyStates, ManagerSummaryStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    absence_decision_keyboard,
    support_manager_keyboard,
    support_user_keyboard,
    lead_decision_keyboard,
    recipients_keyboard,
    transfer_decision_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="manager")


def _is_manager(max_user_id: int, db: Database = None) -> bool:
    # Менеджером можно стать и по паролю (/manager), а не только через .env.
    if db is not None:
        return is_manager(db, max_user_id)
    return max_user_id in settings.MANAGER_IDS


def _parse_date(text: str):
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ, возвращает date или None."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ==================== РАССЫЛКА ====================

@router.callback_query(F.data == "menu:manager:broadcast")
async def broadcast_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await state.set_state(BroadcastStates.waiting_for_content)
    await callback.message.answer(
        "📢 Отправьте текст рассылки или фото (можно с подписью).\n"
        "Если нужно только фото — отправьте его без текста."
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_for_content, F.photo)
async def broadcast_content_photo(message: Msg, state: FSMContext) -> None:
    await state.update_data(
        broadcast_text=message.text or "",
        broadcast_photo=message.photo.token,
        broadcast_photo_url=message.photo.url,
    )
    await _ask_recipients(message, state)


@router.message(BroadcastStates.waiting_for_content, F.text)
async def broadcast_content_text(message: Msg, state: FSMContext) -> None:
    await state.update_data(
        broadcast_text=message.text,
        broadcast_photo=None,
        broadcast_photo_url=None,
    )
    await _ask_recipients(message, state)


async def _ask_recipients(message: Msg, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_for_recipient)
    await message.answer("Кому отправить?", reply_markup=recipients_keyboard())


@router.message(BroadcastStates.waiting_for_recipient)
async def broadcast_recipient_ignore(message: Msg) -> None:
    await message.answer(
        "Пожалуйста, выберите получателей с помощью кнопок ниже.",
        reply_markup=recipients_keyboard(),
    )


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_execute(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    action = callback.data.split(":")[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    text = data.get("broadcast_text") or ""
    photo_token = data.get("broadcast_photo")
    photo_url = data.get("broadcast_photo_url")
    if not text and not photo_token and not photo_url:
        await callback.message.edit_text("❌ Нечего отправлять — начните заново.")
        await callback.answer()
        return

    # База разделена на две принципиально разные аудитории:
    #   * ЗАРЕГИСТРИРОВАННЫЕ — вошли в бота и найдены в CRM
    #     (родители и преподаватели): им пишут про занятия;
    #   * НОВЫЕ — оставили заявку, но в CRM не заведены: они лежат в
    #     отдельной таблице leads, и текст для действующих клиентов
    #     им не подходит.
    audiences = {
        "teacher": lambda: db.get_all_users_by_role("teacher"),
        "parent": lambda: db.get_all_users_by_role("parent"),
        "registered": lambda: (
            db.get_all_users_by_role("parent")
            + db.get_all_users_by_role("teacher")
        ),
        "lead": db.get_lead_recipients,
        "all": lambda: (
            db.get_all_users_by_role("parent")
            + db.get_all_users_by_role("teacher")
            + db.get_lead_recipients()
        ),
    }
    labels = {
        "teacher": "преподавателям",
        "parent": "родителям",
        "registered": "зарегистрированным клиентам",
        "lead": "новым клиентам (заявки)",
        "all": "всем",
    }
    recipients = audiences.get(action, audiences["all"])()

    # Один и тот же человек может быть и родителем, и лидом (оставил
    # заявку, а потом вошёл) — без дедупликации он получил бы два
    # одинаковых сообщения.
    seen = set()
    unique = []
    for user in recipients:
        uid = user.get("max_user_id")
        if uid and uid not in seen and db.notification_enabled(uid, "broadcasts"):
            seen.add(uid)
            unique.append(user)
    recipients = unique

    if not recipients:
        await callback.message.edit_text(
            f"📭 В этой базе никого нет — рассылать {labels.get(action, '')} некому."
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"📤 Отправляю {labels.get(action, '')}… ({len(recipients)} получателей)"
    )
    await callback.answer()

    success = failed = 0
    for i, user in enumerate(recipients):
        if i:
            # Без паузы рассылка на сотню человек упирается в лимит MAX
            # (2 сообщения/сек в один диалог) и часть просто теряется.
            await asyncio.sleep(settings.MAX_BROADCAST_DELAY)
        try:
            if photo_token or photo_url:
                # У входящего изображения token может быть непригоден для
                # повторной отправки. URL из payload — официальный способ
                # отправки уже существующего изображения без повторной
                # загрузки. Если URL не пришёл (старое состояние FSM),
                # оставляем совместимость с сохранённым token.
                photo_payload = {"url": photo_url} if photo_url else {"token": photo_token}
                await safe_call(lambda u=user: callback.bot.send_message(
                    user_id=u["max_user_id"],
                    text=text,
                    attachments=[{"type": "image", "payload": photo_payload}],
                ))
            else:
                await safe_call(lambda u=user: callback.bot.send_message(
                    user_id=u["max_user_id"], text=text
                ))
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Рассылка не дошла до {user['max_user_id']}: {e}")

    await callback.message.edit_text(
        f"✅ Отправлено: {success}/{len(recipients)}"
        + (f"\n⚠️ Не доставлено: {failed}" if failed else "")
    )


# ==================== ЗАЯВКИ ОТ НОВЫХ КЛИЕНТОВ ====================

@router.callback_query(F.data == "menu:manager:leads")
async def leads_list(callback: Callback, db: Database) -> None:
    """
    Заявки, оставленные через воронку нового посетителя: телефон,
    выбранное направление и возраст ребёнка.
    """
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    leads = db.get_leads(status="new")
    drafts = db.get_draft_leads()

    if not leads:
        counts = db.count_leads()
        done = counts.get("done", 0) + counts.get("rejected", 0)
        tail = f"\n\n<i>Обработано ранее: {done}</i>" if done else ""
        await callback.message.answer(
            f"📭 Новых заявок нет.{tail}", parse_mode="HTML"
        )
        await _show_drafts(callback, drafts)
        return

    await callback.message.answer(
        f"📋 <b>Новые заявки: {len(leads)}</b>", parse_mode="HTML"
    )

    for i, lead in enumerate(leads):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda r=lead: callback.message.answer(
            f"🆕 <b>Заявка №{r['id']}</b>\n\n"
            f"📞 <b>Телефон:</b> {esc(r.get('phone') or '—')}\n"
            f"🎯 <b>Направление:</b> {esc(r.get('direction') or '—')}\n"
            f"👶 <b>Возраст ребёнка:</b> {esc(r.get('ages') or '—')}\n"
            f"👤 <b>Имя в MAX:</b> {esc(r.get('full_name') or '—')}\n"
            f"📅 <b>Оставлена:</b> {esc(fmt_db_time(r.get('created_at')))}",
            parse_mode="HTML",
            reply_markup=lead_decision_keyboard(r["id"]),
        ))

    await _show_drafts(callback, drafts)


async def _show_drafts(callback: Callback, drafts) -> None:
    """
    Незавершённые обращения: человек выбрал направление, но номер не
    оставил. Раньше такие обращения нигде не сохранялись и менеджер о
    них не узнавал вовсе. Связаться с ними напрямую нельзя — телефона
    нет, — но видеть спрос полезно.
    """
    if not drafts:
        return

    lines = [f"📝 <b>Незавершённые обращения: {len(drafts)}</b>",
             "<i>Выбрали направление, но номер не оставили.</i>", ""]
    for d in drafts[:15]:
        lines.append(
            f"• {esc(d.get('full_name') or '—')} — "
            f"{esc(d.get('direction') or 'направление не выбрано')}"
            f" (возраст {esc(d.get('ages') or '—')}, "
            f"{esc(fmt_db_time(d.get('created_at')))})"
        )
    if len(drafts) > 15:
        lines.append(f"…и ещё {len(drafts) - 15}")

    await callback.message.answer("\n".join(lines), parse_mode="HTML")


async def _resolve_lead(callback: Callback, db: Database, status: str, label: str) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    lead_id = int(callback.data.split(":")[1])
    lead = db.get_lead(lead_id)
    if not lead:
        await callback.answer("Заявка не найдена.")
        return

    # False — заявку уже закрыл другой менеджер.
    if not db.resolve_lead(lead_id, status, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Заявка №{lead_id} уже обработана.")
        await callback.answer()
        return

    await callback.message.edit_text(
        f"{label} Заявка №{lead_id} — {esc(lead.get('phone') or '—')}, "
        f"{esc(lead.get('direction') or '—')}",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lead_ok:"))
async def lead_done(callback: Callback, db: Database) -> None:
    await _resolve_lead(callback, db, "done", "✅ Обработана:")


@router.callback_query(F.data.startswith("lead_no:"))
async def lead_rejected(callback: Callback, db: Database) -> None:
    await _resolve_lead(callback, db, "rejected", "❌ Отклонена:")


# ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

@router.callback_query(F.data == "menu:manager:transfers")
async def transfer_list(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    requests = db.get_pending_transfer_requests()
    if not requests:
        await callback.message.answer("📭 Заявок нет.")
        return

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}

    for i, req in enumerate(requests):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        author_role = req.get("author_role") or "teacher"
        lesson_id = req.get("lesson_id") or "—"
        await safe_call(lambda r=req, al=author_role, lid=lesson_id: callback.message.answer(
            f"🔁 <b>Заявка №{r['id']}</b>\n\n"
            f"📅 <b>Создана:</b> {esc(fmt_db_time(r.get('created_at')))}\n"
            f"{role_label.get(al, '👤 Автор')}: {esc(r.get('author_name') or '—')}\n"
            f"📞 <b>Телефон:</b> {esc(r.get('author_phone') or '—')}\n"
            f"🆔 <b>Урок ID:</b> {esc(lid)}\n"
            f"💬 <b>Комментарий:</b> {esc(r.get('comment') or '—')}",
            parse_mode="HTML",
            reply_markup=transfer_decision_keyboard(r["id"]),
        ))


async def _resolve_transfer(
    callback: Callback, db: Database, status: str, label: str
) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    request_id = int(callback.data.split(":")[1])
    request = db.get_transfer_request(request_id)
    if not request:
        await callback.answer("Заявка не найдена.")
        return

    # resolve_* возвращает False, если заявку уже закрыл другой менеджер.
    if not db.resolve_transfer_request(request_id, status, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Заявка №{request_id} уже обработана.")
        await callback.answer()
        return

    try:
        await callback.bot.send_message(
            user_id=request["teacher_max_id"], text=f"{label} Заявка №{request_id}."
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить автора заявки {request_id}: {e}")

    await callback.message.edit_text(f"{label} Заявка №{request_id}.")
    await callback.answer()


@router.callback_query(F.data.startswith("transfer_ok:"))
async def transfer_approve(callback: Callback, db: Database) -> None:
    await _resolve_transfer(callback, db, "approved", "✅ Одобрено:")


@router.callback_query(F.data.startswith("transfer_no:"))
async def transfer_reject(callback: Callback, db: Database) -> None:
    await _resolve_transfer(callback, db, "rejected", "❌ Отклонено:")


# ==================== СВОДКА ЗА ПЕРИОД ====================

@router.callback_query(F.data == "menu:manager:summary")
async def summary_period_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await state.set_state(ManagerSummaryStates.waiting_for_date_from)
    today = settings.today()
    await callback.message.answer(
        "📅 Введите <b>начальную</b> дату.\n"
        f"Формат: <code>{today.isoformat()}</code> или <code>{today.strftime('%d.%m.%Y')}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ManagerSummaryStates.waiting_for_date_from, F.text)
async def summary_date_from(message: Msg, state: FSMContext) -> None:
    parsed = _parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
            parse_mode="HTML",
        )
        return

    await state.update_data(date_from=parsed.isoformat())
    await state.set_state(ManagerSummaryStates.waiting_for_date_to)
    await message.answer(
        "📅 Теперь <b>конечную</b> дату.\n"
        "<i>Отправьте «-», чтобы взять тот же день.</i>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_to, F.text)
async def summary_date_to(
    message: Msg,
    state: FSMContext,
    db: Database,
    crm: AlfaCRMClient,
    cache: LessonCache,
) -> None:
    if not _is_manager(message.from_user.id, db):
        await state.clear()
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    if not date_from:
        await state.clear()
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return

    if message.text.strip() in ("-", "—"):
        date_to = date_from
    else:
        parsed = _parse_date(message.text)
        if not parsed:
            await message.answer(
                "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
                parse_mode="HTML",
            )
            return
        date_to = parsed.isoformat()

    await state.clear()

    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await message.answer("🔍 Собираю сводку…")
    logger.info(f"📅 Сводка за {date_from} – {date_to} (branch_id={settings.BRANCH_ID})")

    try:
        lessons = await fetch_lessons(crm, cache, db=db, date_from=date_from, date_to=date_to)
        logger.info(f"📊 Уроков в периоде: {len(lessons)}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения уроков: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка получения уроков: {esc(str(e))}", parse_mode="HTML")
        return

    if date_from == date_to:
        day = datetime.strptime(date_from, "%Y-%m-%d").date()
        period_label = f"за {fmt_date_long(day)}"
    else:
        period_label = f"за период {date_from} – {date_to}"

    try:
        blocks = await get_lesson_summary(lessons, db, crm, period_label)
        await answer_blocks(message, blocks)
        logger.info(f"✅ Сводка отправлена ({len(blocks)} сообщений)")
    except Exception as e:
        logger.error(f"❌ Ошибка формирования/отправки сводки: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")


# ==================== НЕЯВКИ ====================
#
# Преподаватель отмечает неявку в боте (она хранится в БД бота, т.к. в
# AlfaCRM статуса «не пришёл» нет). Менеджер решает: списать занятие
# с абонемента (POST check_visits/burn_one) или признать причину
# уважительной — тогда в CRM ничего не уходит.

async def send_absence_list(message, db: Database, date_iso: str) -> None:
    absences = db.get_absences_for_date(date_iso, status="pending")
    if not absences:
        await message.answer(f"✅ Неявок за {esc(date_iso)} нет.", parse_mode="HTML")
        return

    await message.answer(
        f"❌ <b>Неявки за {esc(date_iso)}</b>: {len(absences)}", parse_mode="HTML"
    )
    for i, a in enumerate(absences):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        await safe_call(lambda r=a: message.answer(
            f"👤 <b>{esc(r.get('client_name') or r['client_id'])}</b>\n"
            f"📅 <b>Дата:</b> {esc(r['lesson_date'])}\n"
            f"🆔 <b>Занятие:</b> {esc(r['lesson_id'])}\n"
            f"🕐 <b>Отмечено:</b> {esc(fmt_db_time(r.get('created_at')))}",
            parse_mode="HTML",
            reply_markup=absence_decision_keyboard(r["id"]),
        ))


@router.callback_query(F.data == "menu:manager:absences")
async def manager_absences(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()
    await send_absence_list(callback.message, db, settings.today().isoformat())


@router.callback_query(F.data.startswith("burn:"))
async def absence_burn(
    callback: Callback, db: Database, crm: AlfaCRMClient, cache: LessonCache
) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return
    if absence["status"] != "pending":
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return

    lesson = await get_lesson_snapshot(absence["lesson_id"], crm, cache)
    if not lesson:
        await callback.answer("❌ Занятие не найдено в CRM.")
        return

    # Списание занятия = пропуск БЕЗ уважительной причины: в AlfaCRM
    # такая деталь урока (is_attend=0 без reason_id) и списывает занятие
    # с абонемента. Отдельного «сжигающего» вызова, как в impulseCRM
    # (check_visits/burn_one через внутренний API с cookie сессии), тут
    # не нужно — правило живёт в самой CRM, в справочнике причин.
    try:
        await crm.mark_absent(
            absence["lesson_id"], absence["client_id"], current=lesson
        )
    except AlfaCRMError as e:
        await callback.answer()
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    # resolve_* вернёт False, если неявку уже закрыл другой менеджер.
    if not db.resolve_absence(absence_id, "burned", callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return

    await _notify_parent_burned(callback, db, absence)

    await callback.message.edit_text(
        f"🔥 Занятие списано (сгорело): "
        f"{esc(absence.get('client_name') or absence['client_id'])}, "
        f"{esc(absence['lesson_date'])}"
    )
    await callback.answer("🔥 Списано")


async def _notify_parent_burned(callback: Callback, db: Database, absence) -> None:
    """
    Лояльно сообщить родителю, что занятие сгорело.

    Ключ дедупликации тот же, что у планировщика
    (scheduler.notify_burned_lessons): иначе родитель получил бы два
    сообщения — от автоматики и от менеджера.
    """
    client_id = absence.get("client_id")
    lesson_id = absence.get("lesson_id")
    parent = (
        db.get_user_by_crm_id(client_id, "parent") if client_id is not None else None
    )
    if not parent or not lesson_id:
        return

    reminder_type = f"burned:{client_id}"
    if db.was_reminder_sent(
        lesson_id, reminder_type, parent["max_user_id"], hours=24 * 30
    ):
        return

    text = (
        "😔 <b>Занятие сгорело</b>\n\n"
        f"Ребёнок не был на занятии {esc(absence.get('lesson_date') or '')}, "
        f"и заморозить его уже не получилось — это возможно не позднее "
        f"чем за {freeze_deadline_hint()} до начала.\n\n"
        "Если это ошибка или была уважительная причина — просто напишите "
        "менеджеру, он разберётся и вернёт занятие. Ничего страшного "
        "не произошло 🙂"
    )
    try:
        await safe_call(lambda: callback.message.bot.send_message(
            user_id=parent["max_user_id"], text=text, fmt="html",
        ))
        db.mark_reminder_sent(lesson_id, reminder_type, parent["max_user_id"])
    except Exception as e:
        logger.warning(f"Не удалось уведомить родителя о сгоревшем занятии: {e}")


async def _resolve_absence(
    callback: Callback, db: Database, status: str, note: str
) -> Optional[Dict[str, Any]]:
    """Закрывает неявку. None — если её уже обработал другой менеджер."""
    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return None

    if not db.resolve_absence(absence_id, status, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Неявка №{absence_id} уже обработана.")
        await callback.answer()
        return None

    await callback.message.edit_text(
        f"{note}: {esc(absence.get('client_name') or absence['client_id'])}, "
        f"{esc(absence['lesson_date'])}"
    )
    return absence


@router.callback_query(F.data.startswith("frz_no:"))
async def absence_freeze_no_reason(
    callback: Callback, db: Database, freezes: FreezeCounter, crm: AlfaCRMClient
) -> None:
    """
    Беспричинная заморозка: занятие не сгорает, но расходуется одна
    беспричинная заморозка клиента (счётчик в поле адреса проживания).
    """
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return

    try:
        left = await freezes.spend(absence["client_id"])
    except AlfaCRMError as e:
        await callback.answer()
        await callback.message.answer(f"❌ {esc(str(e))}", parse_mode="HTML")
        return

    # В absences статус остаётся 'excused' — CHECK в таблице разрешает
    # только pending/burned/excused, а менять схему у работающей базы
    # ради оттенка статуса рискованно. Детали (беспричинная это заморозка
    # или уважительная) хранятся в таблице freezes, откуда их и читают
    # отчёты и раздел справок.
    resolved = await _resolve_absence(
        callback, db, "excused", "❄️ Заморозка без причины"
    )
    if resolved is None:
        return

    db.add_freeze(
        absence["client_id"],
        kind="no_reason",
        client_name=absence.get("client_name") or "",
        lesson_id=absence.get("lesson_id"),
        lesson_date=absence.get("lesson_date") or "",
        created_by=callback.from_user.id,
        created_by_role="manager",
    )
    await callback.message.answer(
        f"❄️ Заморозка списана. У клиента осталось <b>{left}</b> беспричинных заморозок.",
        parse_mode="HTML",
    )
    await callback.answer("❄️ Заморожено")


@router.callback_query(F.data.startswith("frz_ok:"))
async def absence_freeze_valid(callback: Callback, db: Database) -> None:
    """Уважительная причина: ни занятие, ни счётчик заморозок не трогаем."""
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return

    absence_id = int(callback.data.split(":")[1])
    absence = db.get_absence(absence_id)
    if not absence:
        await callback.answer("Неявка не найдена.")
        return

    resolved = await _resolve_absence(
        callback, db, "excused", "🙏 Уважительная причина"
    )
    if resolved is None:
        return

    db.add_freeze(
        absence["client_id"],
        kind="valid",
        reason="manager",
        client_name=absence.get("client_name") or "",
        lesson_id=absence.get("lesson_id"),
        lesson_date=absence.get("lesson_date") or "",
        created_by=callback.from_user.id,
        created_by_role="manager",
    )
    await callback.message.answer("🙏 Занятие не списано, лимит заморозок не тронут.")
    await callback.answer("🙏 Уважительная")


# ==================== ПОДДЕРЖКА (сторона менеджера) ====================

@router.callback_query(F.data == "menu:manager:support")
async def manager_support_list(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    tickets = db.get_open_tickets()
    if not tickets:
        await callback.message.answer("📭 Открытых обращений нет.")
        return

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}
    for i, t in enumerate(tickets):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)
        messages = db.get_ticket_messages(t["id"], limit=5)
        tail = "\n".join(
            f"{'👤' if m['sender_side'] == 'user' else '👔'} {esc(m['text'])[:200]}"
            for m in messages[-3:]
        )
        await safe_call(lambda r=t, body=tail: callback.message.answer(
            f"🆘 <b>Обращение №{r['id']}</b>\n"
            f"{role_label.get(r.get('user_role'), '👤 Пользователь')}: "
            f"{esc(r.get('user_name') or '—')}\n"
            f"📞 <b>Телефон:</b> {esc(r.get('user_phone') or '—')}\n"
            f"📅 <b>Создано:</b> {esc(fmt_db_time(r.get('created_at')))}\n\n"
            f"{body}",
            parse_mode="HTML",
            reply_markup=support_manager_keyboard(r["id"]),
        ))


@router.callback_query(F.data.startswith("sup_reply:"))
async def manager_support_reply_start(callback: Callback, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    ticket_id = int(callback.data.split(":")[1])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(ManagerReplyStates.waiting_for_reply)
    await callback.message.answer(f"✍️ Напишите ответ по обращению №{ticket_id}:")
    await callback.answer()


@router.message(ManagerReplyStates.waiting_for_reply, F.text)
async def manager_support_reply_send(message: Msg, state: FSMContext, db: Database) -> None:
    if not _is_manager(message.from_user.id, db):
        await state.clear()
        return

    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    await state.clear()

    ticket = db.get_ticket(ticket_id) if ticket_id else None
    if not ticket:
        await message.answer("❌ Обращение не найдено.")
        return
    if ticket["status"] != "open":
        await message.answer(f"ℹ️ Обращение №{ticket_id} уже закрыто.")
        return

    db.add_ticket_message(ticket_id, message.from_user.id, "manager", message.text)
    try:
        await message.bot.send_message(
            user_id=ticket["user_max_id"],
            text=f"{esc(message.text)}",
            fmt="html",
            attachments=support_user_keyboard(ticket_id),
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        logger.warning(f"Не удалось отправить ответ по обращению {ticket_id}: {e}")
        await message.answer("⚠️ Не удалось доставить ответ пользователю.")


# ==================== АКТИВНОСТЬ В БОТЕ ====================
#
# Раньше каждое действие посетителя приходило менеджеру отдельным
# сообщением (см. bot/notifier.py). При десятке посетителей лента
# состояла почти целиком из активности, и в ней терялось главное —
# заявки, обращения и решения по неявкам. Теперь активность копится
# в БД и показывается только по этой кнопке.

@router.callback_query(F.data == "menu:manager:activity")
async def manager_activity(callback: Callback, db: Database) -> None:
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    visitors = db.get_recent_activity(
        users_limit=settings.ACTIVITY_USERS_LIMIT,
        events_per_user=settings.ACTIVITY_EVENTS_PER_USER,
    )
    if not visitors:
        await callback.message.answer(
            "🙈 Активности пока не было.\n\n"
            "<i>Здесь появятся действия посетителей бота: кто заходил, "
            "что нажимал и что писал.</i>",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(
        f"👀 <b>Активность в боте</b>\n"
        f"Последние посетители: {len(visitors)} "
        f"(всего событий в журнале: {db.count_activity()})",
        parse_mode="HTML",
    )

    for i, v in enumerate(visitors):
        if i:
            await asyncio.sleep(settings.MAX_SEND_DELAY)

        # Телефон подставляется из привязки к CRM или из ранее
        # оставленной заявки: без него запись бесполезна — менеджеру
        # некуда позвонить.
        phone = _lookup_phone(db, v["max_user_id"])
        events = v.get("events") or []
        body = "\n".join(
            f"{n}. {esc(e['event'])} "
            f"<i>({esc(fmt_db_time(e.get('created_at')))})</i>"
            for n, e in enumerate(events, 1)
        )
        more = (
            f"\n<i>… и ещё {v['total'] - len(events)} действий раньше</i>"
            if v.get("total", 0) > len(events) else ""
        )

        await safe_call(lambda r=v, ph=phone, b=body, m=more: callback.message.answer(
            f"👤 <b>{esc(_describe_visitor(db, r))}</b>\n"
            f"📞 <b>Телефон:</b> {esc(ph or 'не оставлен')}\n"
            f"🆔 MAX id: <code>{r['max_user_id']}</code>\n"
            f"🕐 Последнее действие: {esc(fmt_db_time(r.get('last_at')))}\n\n"
            f"{b}{m}",
            parse_mode="HTML",
        ))


def _lookup_phone(db: Database, max_user_id: int):
    """Телефон из привязки к CRM, иначе из оставленной заявки."""
    try:
        user = db.get_user(max_user_id)
        if user and user.get("phone"):
            return user["phone"]
        for lead in db.get_leads(status=None):
            if lead.get("max_user_id") == max_user_id and lead.get("phone"):
                return lead["phone"]
    except Exception as e:
        logger.debug(f"Не удалось определить телефон для {max_user_id}: {e}")
    return None


def _describe_visitor(db: Database, visitor) -> str:
    try:
        user = db.get_user(visitor["max_user_id"])
    except Exception:
        user = None
    if user:
        roles = {"parent": "родитель", "teacher": "преподаватель",
                 "manager": "менеджер"}
        role = roles.get(user.get("role"), user.get("role") or "")
        return f"{user.get('full_name') or '—'} ({role}, есть в CRM)"
    return f"{visitor.get('user_name') or '—'} (нет в CRM)"


# ==================== КТО ВОШЁЛ В БОТА ====================

@router.callback_query(F.data == "menu:manager:logins")
async def manager_logins(callback: Callback, db: Database) -> None:
    """
    Кто вошёл в бота, а кто нет — имя и роль.

    Практический смысл: до тех, кто не вошёл, бот не достучится —
    ни расписанием, ни напоминанием, ни рассылкой. Это список людей,
    которых нужно доводить до входа вручную.
    """
    if not _is_manager(callback.from_user.id, db):
        await callback.answer("❌ Недоступно.")
        return
    await callback.answer()

    report = db.get_login_report()
    roles = {"parent": "родитель", "teacher": "преподаватель", "manager": "менеджер"}

    def block(title: str, rows, empty: str, with_role: bool = True):
        out = [title]
        if not rows:
            out.append(f"<i>{empty}</i>")
            return out
        for r in rows:
            role = roles.get(r.get("role"), r.get("role") or "заявка")
            name = esc(r.get("full_name") or "—")
            phone = esc(r.get("phone") or "телефон не указан")
            suffix = f" — {role}" if with_role else ""
            out.append(f"• <b>{name}</b>{suffix}\n  📞 {phone}")
        return out

    lines = [
        "🔐 <b>Кто вошёл в бота</b>\n",
        *block(
            f"✅ <b>Вошли ({len(report['active'])})</b>",
            report["active"],
            "Пока никто не вошёл.",
        ),
        "",
        *block(
            f"🚪 <b>Вышли из профиля ({len(report['inactive'])})</b>",
            report["inactive"],
            "Никто не выходил.",
        ),
        "",
        *block(
            f"🆕 <b>Не входили — только заявка ({len(report['leads'])})</b>",
            report["leads"],
            "Таких нет.",
            with_role=False,
        ),
    ]

    await answer_blocks(callback.message, split_messages(["\n".join(lines)]))
