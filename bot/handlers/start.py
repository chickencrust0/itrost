"""
bot/handlers/start.py — вход по номеру телефона, меню роли, выход.

Порядок диалога на входе (изменён): бот ВСЕГДА начинает с запроса
номера, без развилки «вы впервые или уже занимаетесь?». Причина: без
номера бот не может ничего — ни показать расписание, ни принять заявку,
ни связать человека с менеджером. Спрашивать, кто он такой, чтобы потом
всё равно попросить номер, — лишний шаг.

Схема:
    /start -> «поделитесь номером»
        номер есть в CRM  -> вход как преподаватель / родитель
        номера в CRM нет  -> «такого номера в базе нет… давайте
                              познакомимся» -> воронка заявки
                              (bot/handlers/lead.py), причём номер уже
                              собран и повторно не запрашивается

Кнопки «Отмена» на этом пути нет нигде: отменить — значит остаться в
состоянии, где бот бесполезен. В тупиковых точках предлагается
единственное действие — вернуться в начало (start_keyboard).
"""

import logging

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.formatting import esc
from bot.dispatcher import Command, CommandStart, F, Router
from bot.fsm import FSMContext
from bot.states import LeadStates, LoginStates, ManagerLoginStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    confirm_logout_keyboard,
    manager_menu_keyboard,
    parent_menu_keyboard,
    request_phone_keyboard,
    teacher_menu_keyboard,
    notification_settings_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="start")

ROLE_LABELS = {
    "teacher": "👨‍🏫 Преподаватель",
    "parent": "👨‍👩‍👧 Родитель",
    "manager": "👑 Менеджер",
}

ASK_PHONE_TEXT = (
    "👋 <b>Здравствуйте!</b>\n\n"
    "Это бот школы «{school}».\n\n"
    "📱 <b>Для начала работы поделитесь номером телефона.</b>\n"
    "Нажмите кнопку ниже или напишите номер сообщением.\n\n"
    "<i>Формат: +7(XXX)XXX-XX-XX или 8XXXXXXXXXX</i>"
)


def get_menu_by_role(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "parent":
        return parent_menu_keyboard()
    return manager_menu_keyboard()


def _login_manager(db: Database, message: Msg, phone: str = "") -> None:
    db.link_user(
        max_user_id=message.from_user.id,
        crm_id=0,
        role="manager",
        phone=phone,
        full_name=message.from_user.full_name or "Менеджер",
    )


async def ask_phone(message: Msg, state: FSMContext) -> None:
    """Единая точка запроса номера — и на /start, и при возврате в начало."""
    await state.clear()
    await state.set_state(LoginStates.waiting_for_phone)
    await message.answer(
        ASK_PHONE_TEXT.format(school=esc(settings.SCHOOL_NAME)),
        reply_markup=request_phone_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("id", "myid", "whoami"))
async def show_my_id(message: Msg, db: Database) -> None:
    """
    Отдаёт человеку его MAX id.

    Нужно для первичной настройки: права менеджера выдаются по списку
    ADMIN_MAX_IDS, а чтобы его заполнить, надо где-то узнать свой id.
    Команда доступна всем — id не секрет, по нему нельзя ни войти, ни
    получить доступ: он лишь позволяет ВАМ вписать себя в .env на
    сервере, куда посторонний не попадёт.
    """
    user_id = message.from_user.id
    is_manager = user_id in settings.MANAGER_IDS
    known = db.get_user(user_id)

    lines = [
        "🆔 <b>Ваш MAX id</b>",
        "",
        f"<code>{user_id}</code>",
        "",
        f"Имя: {esc(message.from_user.full_name or '—')}",
    ]
    if known:
        roles = {"parent": "родитель", "teacher": "преподаватель", "manager": "менеджер"}
        lines.append(f"Роль в боте: {roles.get(known['role'], known['role'])}")
    if is_manager:
        lines.append("Права менеджера: ✅ есть")
    else:
        lines.append(
            "Права менеджера: нет\n\n"
            "<i>Чтобы выдать их, впишите число выше в ADMIN_MAX_IDS "
            "в файле .env и перезапустите бота. Несколько id — через запятую.</i>"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Msg, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)

    if not user and message.from_user.id in settings.MANAGER_IDS:
        # Менеджеру телефон не нужен — он опознаётся по ID в MAX.
        _login_manager(db, message)
        user = db.get_user(message.from_user.id)

    if user:
        role = user["role"]
        crm_line = (
            f"🆔 CRM ID: <code>{user['crm_id']}</code>\n" if user["crm_id"] else ""
        )
        await message.answer(
            f"👋 С возвращением, <b>{esc(user['full_name'])}</b>!\n\n"
            f"📊 Роль: {ROLE_LABELS.get(role, esc(role))}\n"
            f"{crm_line}\n"
            f"👇 Выберите раздел:",
            reply_markup=get_menu_by_role(role),
            parse_mode="HTML",
        )
        return

    await ask_phone(message, state)



# ==================== ВХОД МЕНЕДЖЕРА ПО ПАРОЛЮ ====================
#
# Менеджером можно стать двумя путями: попасть в ADMIN_MAX_IDS в .env
# или ввести пароль по команде /manager. Второй нужен, чтобы подключать
# новых сотрудников без правки окружения и перезапуска бота.

@router.message(Command("manager", "admin"))
async def manager_login_start(message: Msg, db: Database, state: FSMContext) -> None:
    user = db.get_user(message.from_user.id)
    if (user and user["role"] == "manager") or message.from_user.id in settings.MANAGER_IDS:
        await message.answer(
            "Вы уже вошли как менеджер.", reply_markup=manager_menu_keyboard()
        )
        return

    await state.set_state(ManagerLoginStates.waiting_for_password)
    await message.answer(
        "🔐 <b>Вход для менеджера</b>\n\nВведите пароль:", parse_mode="HTML"
    )


@router.message(ManagerLoginStates.waiting_for_password, F.text)
async def manager_login_password(message: Msg, db: Database, state: FSMContext) -> None:
    entered = (message.text or "").strip()
    await state.clear()

    if not settings.MANAGER_PASSWORD or entered != settings.MANAGER_PASSWORD:
        logger.warning(
            f"⛔ Неверный пароль менеджера от {message.from_user.id} "
            f"({message.from_user.full_name})"
        )
        await message.answer("❌ Неверный пароль.")
        return

    _login_manager(db, message)
    logger.info(
        f"✅ {message.from_user.id} ({message.from_user.full_name}) вошёл как менеджер по паролю"
    )
    await message.answer(
        "✅ <b>Вы вошли как менеджер.</b>\n\n"
        "Теперь вам будут приходить заявки, обращения и вечерняя сводка по неявкам.",
        parse_mode="HTML",
        reply_markup=manager_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:start")
async def back_to_start(callback: Callback, db: Database, state: FSMContext) -> None:
    """Кнопка «В начало» из тупиковых точек диалога."""
    await state.clear()
    user = db.get_user(callback.from_user.id)
    if user:
        await callback.message.answer(
            "👇 Выберите раздел:", reply_markup=get_menu_by_role(user["role"])
        )
    else:
        await ask_phone(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:notifications")
async def notification_settings(callback: Callback, db: Database) -> None:
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала войдите в профиль через /start.")
        return
    await callback.message.answer(
        "🔔 <b>Настройки уведомлений</b>\n\nНажмите на категорию, чтобы включить или выключить её.",
        parse_mode="HTML",
        reply_markup=notification_settings_keyboard(
            db.get_notification_preferences(callback.from_user.id), user["role"]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("notifications:"))
async def notification_toggle(callback: Callback, db: Database) -> None:
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала войдите в профиль через /start.")
        return
    category = callback.data.partition(":")[2]
    allowed = ("summaries", "updates", "broadcasts") if user["role"] == "manager" else (
        "lessons", "updates", "broadcasts"
    )
    if category not in allowed:
        await callback.answer("Неизвестная настройка.")
        return
    preferences = db.toggle_notification(callback.from_user.id, category)
    await callback.message.edit_text(
        "🔔 <b>Настройки уведомлений</b>\n\nНажмите на категорию, чтобы включить или выключить её.",
        parse_mode="HTML",
        reply_markup=notification_settings_keyboard(preferences, user["role"]),
    )
    await callback.answer("Настройки сохранены")


@router.callback_query(F.data.startswith("auth:"))
async def legacy_auth_gate(callback: Callback, db: Database, state: FSMContext) -> None:
    """
    Старая развилка «впервые / уже занимаюсь» убрана, но кнопки из ранее
    отправленных сообщений в MAX остаются нажимаемыми. Чтобы нажатие не
    проваливалось в тишину, ведём человека по новому пути — к номеру.
    """
    await back_to_start(callback, db, state)


@router.message(F.contact)
async def handle_contact(
    message: Msg, db: Database, crm: AlfaCRMClient, state: FSMContext
) -> None:
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        # Чужой контакт: иначе можно войти под любым номером из адресной книги.
        await message.answer(
            "❌ Поделитесь, пожалуйста, <b>своим</b> контактом.",
            parse_mode="HTML",
            reply_markup=request_phone_keyboard(),
        )
        return
    await process_phone_login(message, db, crm, message.contact.phone_number, state)


@router.message(F.text.regexp(r"^(?=(?:\D*\d){10,}\D*$)\+?\d[\d\-\(\)\s]+$"))
async def handle_manual_phone(
    message: Msg, db: Database, crm: AlfaCRMClient, state: FSMContext
) -> None:
    user = db.get_user(message.from_user.id)
    if user:
        if not (user.get("phone") or "").strip():
            await process_phone_login(message, db, crm, message.text.strip(), state)
            return
        # Уже авторизован. Раньше здесь стоял голый return, и сообщение
        # пропадало без ответа: диспетчер останавливается на первом
        # совпавшем обработчике и до других роутеров дело не доходило.
        await message.answer(
            "Вы уже вошли. Выберите раздел:",
            reply_markup=get_menu_by_role(user["role"]),
        )
        return
    await process_phone_login(message, db, crm, message.text.strip(), state)


async def process_phone_login(
    message: Msg,
    db: Database,
    crm: AlfaCRMClient,
    phone: str,
    state: FSMContext,
) -> None:
    if len("".join(ch for ch in (phone or "") if ch.isdigit())) < 10:
        await message.answer(
            "❌ В номере должно быть не меньше 10 цифр. Отправьте полный номер телефона.",
            reply_markup=request_phone_keyboard(),
        )
        await state.set_state(LoginStates.waiting_for_phone)
        return
    max_user_id = message.from_user.id
    logger.info(f"Вход по телефону для {max_user_id}")

    if max_user_id in settings.MANAGER_IDS:
        await state.clear()
        _login_manager(db, message, phone)
        await message.answer("✅ Вы вошли как менеджер.", reply_markup=manager_menu_keyboard())
        return

    try:
        teacher = await crm.find_teacher_by_phone(phone)
    except AlfaCRMError as e:
        logger.warning(f"Поиск преподавателя не удался: {e}")
        teacher = None

    if teacher:
        await state.clear()
        name = crm.extract_user_name(teacher)
        db.link_user(
            max_user_id=max_user_id, crm_id=teacher["id"], role="teacher",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>! (Преподаватель)",
            parse_mode="HTML",
            reply_markup=teacher_menu_keyboard(),
        )
        return

    try:
        customer = await crm.find_customer_by_phone(phone)
    except AlfaCRMError as e:
        logger.warning(f"Поиск клиента не удался: {e}")
        customer = None

    if customer:
        await state.clear()
        name = crm.extract_user_name(customer)
        db.link_user(
            max_user_id=max_user_id, crm_id=customer["id"], role="parent",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>!",
            parse_mode="HTML",
            reply_markup=parent_menu_keyboard(),
        )
        return

    # Номера в CRM нет — не тупик, а вход в воронку заявки. Номер уже
    # собран, поэтому в конце воронки его не переспрашивают: он кладётся
    # в состояние и в черновик заявки прямо сейчас.
    await start_lead_funnel(message, db, phone, state)


async def start_lead_funnel(
    message: Msg, db: Database, phone: str, state: FSMContext
) -> None:
    """
    Переход «номер не найден в CRM» -> знакомство.

    Импорт локальный: lead.py тянет общие вещи из start.py, и импорт на
    уровне модуля замкнул бы их друг на друга.
    """
    from bot.handlers.lead import WELCOME_TEXT

    await state.clear()
    await state.set_state(LeadStates.waiting_for_age)
    await state.update_data(lead_phone=phone)

    # Черновик заводим сразу: человек может уйти, не дойдя до возраста, —
    # но номер у нас уже есть, и менеджеру будет с чем работать.
    try:
        db.upsert_draft_lead(
            message.from_user.id,
            phone=phone,
            full_name=message.from_user.full_name or "",
        )
    except Exception as e:
        logger.warning(f"Не удалось сохранить черновик заявки: {e}")

    await message.answer(
        "🤔 <b>Такого номера в нашей базе нет.</b>\n\n"
        "Возможно, у нас записан другой номер — попробуйте ещё раз или "
        "напишите менеджеру.\n\n"
        "А если вы у нас ещё не занимаетесь — давайте познакомимся:",
        parse_mode="HTML",
    )
    await message.answer(WELCOME_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "menu:logout")
async def logout_start(callback: Callback) -> None:
    await callback.message.answer(
        "⚠️ Вы уверены, что хотите выйти?", reply_markup=confirm_logout_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("logout:"))
async def logout_process(callback: Callback, db: Database, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "confirm":
        await state.clear()
        db.deactivate_user(callback.from_user.id)
        await callback.message.edit_text("👋 Вы вышли.")
        await callback.message.answer(
            "Для входа поделитесь номером телефона:",
            reply_markup=request_phone_keyboard(),
        )
        await state.set_state(LoginStates.waiting_for_phone)
    else:
        await callback.message.edit_text("❌ Отменено.")
    await callback.answer()
