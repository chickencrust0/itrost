"""
bot/handlers/support.py — диалог «пользователь ↔ менеджер» прямо в боте.

Как это работает:
  * пользователь (родитель или преподаватель) жмёт «🆘 Связаться с
    менеджером» — открывается обращение (support_tickets) и включается
    состояние SupportStates.chatting;
  * пока состояние активно, ЛЮБОЙ текст пользователя уходит менеджерам
    как сообщение обращения, а не в другие сценарии бота — поэтому этот
    роутер подключается ПЕРВЫМ (см. main.build_dispatcher);
  * менеджер отвечает кнопкой «✍️ Ответить» (bot/handlers/manager.py);
  * закрыть обращение может любая сторона — кнопкой «🔒».

Переписка хранится в БД бота (support_messages) — в AlfaCRM для неё
нет подходящей сущности.
"""

import logging

import settings
from database import Database
from bot.formatting import esc, safe_call
from bot.handlers.common import is_manager, manager_ids
from bot.dispatcher import F, Router
from bot.fsm import FSMContext
from bot.states import SupportStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    manager_menu_keyboard,
    parent_menu_keyboard,
    support_manager_keyboard,
    support_user_keyboard,
    teacher_menu_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="support")


def _menu_for(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "parent":
        return parent_menu_keyboard()
    return manager_menu_keyboard()


@router.callback_query(F.data == "menu:support")
async def support_start(callback: Callback, db: Database, state: FSMContext) -> None:
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ Сначала войдите в профиль.")
        return

    ticket_id = db.create_ticket(
        callback.from_user.id,
        user.get("full_name") or callback.from_user.full_name,
        user.get("role") or "",
        user.get("phone") or "",
    )
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(SupportStates.chatting)

    await callback.message.answer(
        f"👤 <b>Обращение №{ticket_id}</b>\n\n"
        "Напишите ваш вопрос — он уйдёт администратору. Все следующие сообщения "
        "тоже попадут в это обращение, пока вы его не завершите.",
        parse_mode="HTML",
        reply_markup=support_user_keyboard(ticket_id),
    )
    await callback.answer()


@router.message(SupportStates.chatting, F.text)
async def support_user_message(message: Msg, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    ticket = db.get_ticket(ticket_id) if ticket_id else None

    if not ticket or ticket["status"] != "open":
        await state.clear()
        user = db.get_user(message.from_user.id)
        await message.answer(
            "ℹ️ Обращение закрыто. Чтобы задать новый вопрос, нажмите "
            "«👤 Связаться с администратором».",
            reply_markup=_menu_for((user or {}).get("role", "")),
        )
        return

    db.add_ticket_message(ticket_id, message.from_user.id, "user", message.text)

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}
    delivered = 0
    for manager_id in manager_ids(db):
        result = await safe_call(lambda mid=manager_id: message.bot.send_message(
            user_id=mid,
            text=(
                f"👤 <b>Обращение №{ticket_id}</b>\n"
                f"{role_label.get(ticket.get('user_role'), '👤 Пользователь')}: "
                f"{esc(ticket.get('user_name') or '—')}\n"
                f"📞 {esc(ticket.get('user_phone') or '—')}\n\n"
                f"💬 {esc(message.text)}"
            ),
            fmt="html",
            attachments=support_manager_keyboard(ticket_id),
        ))
        if result is not None:
            delivered += 1

    if delivered:
        await message.answer("✅ Отправлено администратору. Ожидайте ответа.")
    else:
        # Либо ADMIN_MAX_IDS пуст, либо никому не доставилось — честно
        # говорим об этом, а не создаём иллюзию, что вопрос принят.
        await message.answer(
            "⚠️ Сообщение сохранено, но администратору доставить не удалось.\n\n"
            "<i>Похоже, ни один менеджер ещё не вошёл в бота. "
            "Свяжитесь с центром по телефону.</i>"
        )


@router.callback_query(F.data.startswith("sup_close:"))
async def support_close(callback: Callback, db: Database, state: FSMContext) -> None:
    ticket_id = int(callback.data.split(":")[1])
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Обращение не найдено.")
        return

    is_manager_user = is_manager(db, callback.from_user.id)
    is_owner = callback.from_user.id == ticket["user_max_id"]
    if not (is_manager_user or is_owner):
        await callback.answer("❌ Недоступно.")
        return

    if not db.close_ticket(ticket_id, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Обращение №{ticket_id} уже закрыто.")
        await callback.answer()
        return

    # Состояние диалога снимаем только у самого пользователя: у менеджера
    # его нет, а чужое состояние из этого контекста не достать.
    if is_owner:
        await state.clear()

    await callback.message.edit_text(f"🔒 Обращение №{ticket_id} закрыто.")

    if is_manager_user and not is_owner:
        user = db.get_user(ticket["user_max_id"])
        await safe_call(lambda: callback.bot.send_message(
            user_id=ticket["user_max_id"],
            text=f"🔒 Обращение №{ticket_id} закрыто администратором.",
            attachments=_menu_for((user or {}).get("role", "")),
        ))
    elif is_owner:
        for manager_id in manager_ids(db):
            await safe_call(lambda mid=manager_id: callback.bot.send_message(
                user_id=mid,
                text=f"🔒 Обращение №{ticket_id} закрыто пользователем.",
            ))
        user = db.get_user(callback.from_user.id)
        await callback.message.answer(
            "Чем ещё помочь?", reply_markup=_menu_for((user or {}).get("role", ""))
        )

    await callback.answer("🔒 Закрыто")
