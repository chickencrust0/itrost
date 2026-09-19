"""
bot/handlers/lead.py — знакомство с новым посетителем.

Как сюда попадают: бот на входе ВСЕГДА просит номер телефона
(bot/handlers/start.py). Если номера нет в CRM, start.py показывает
«такого номера в базе нет… давайте познакомимся», кладёт номер в
состояние (lead_phone) и передаёт разговор сюда.

Сценарий:
    возраст ребёнка (числом; несколько детей — через пробел)
        -> направление кнопками для КАЖДОГО ребёнка по очереди
        -> заявка менеджеру

Телефон здесь уже известен и повторно НЕ запрашивается. Шаг с телефоном
остался только как запасной путь: FSM живёт в памяти, и после
перезапуска бота номер из состояния пропадает.

Кнопки «Отмена» нет нигде: без номера бот всё равно ничего не может, и
«отмена» оставляла бы человека в бесполезном состоянии. В конце и в
тупиковых точках предлагается единственное действие — вернуться в
начало (start_keyboard).

ВАЖНО про порядок роутеров: этот роутер подключается раньше start.py
(см. main.build_dispatcher). Все его обработчики сообщений привязаны к
состояниям FSM, поэтому чужие сообщения он не перехватывает, зато его
собственные не достаются обработчику входа по телефону из start.py,
который срабатывает на любой текст, похожий на номер.
"""

import logging
from typing import List, Optional

import settings
from database import Database
from bot.directions import DIRECTIONS, directions_for_age, label, parse_ages
from bot.dispatcher import F, Router
from bot.formatting import esc, safe_call
from bot.handlers.common import manager_ids
from bot.fsm import FSMContext
from bot.states import LeadStates
from max_api.context import Callback, Msg
from max_api.keyboards import (
    directions_keyboard,
    lead_decision_keyboard,
    lead_phone_keyboard,
    start_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="lead")


WELCOME_TEXT = (
    f"Мы — <b>{settings.SCHOOL_NAME}</b>. Занимаемся с детьми 1–8 классов "
    "программированием, робототехникой и шахматами. Группы до 8 человек, "
    "минимум теории — каждое занятие заканчивается работающим результатом.\n\n"
    "<b>Наши программы:</b>\n\n"
    "🐱 <b>Scratch-программирование</b> (7–10 лет) — первые работающие игры и "
    "программы без барьера синтаксиса: аркады, платформер, калькулятор.\n"
    "🤖 <b>Робототехника</b> (8–12 лет) — конструирование и механика: модель на "
    "дистанционном управлении, робот-сортировщик, манипулятор.\n"
    "🐍 <b>Python</b> (10–14 лет) — переход к настоящему коду через мини-проекты: "
    "«Угадай число», «Крестики-нолики», текстовый квест, чат-бот.\n"
    "♟️ <b>Шахматы</b> (7–14 лет) — логика, концентрация и разбор партий с "
    "первого занятия. Есть группа для продолжающих с подготовкой к разряду.\n\n"
    "<b>Сколько лет вашему ребёнку?</b> Подскажем, какая программа подойдёт "
    "именно ему 🔥\n\n"
    "<i>Напишите возраст числом. Если детей несколько — через пробел, например: 8 11</i>"
)


def _format_ages(children: List[dict]) -> str:
    return ", ".join(str(c["age"]) for c in children)


def _format_directions(children: List[dict]) -> str:
    """
    Направление хранится вместе с возрастом ребёнка, которому выбрано:
    у родителя двоих детей строка «Шахматы, Программирование» не говорит
    менеджеру, кому что предлагать.
    """
    if len(children) == 1:
        return children[0].get("direction") or "Не выбрано"
    return "; ".join(
        f"{c['age']} лет — {c.get('direction') or 'не выбрано'}" for c in children
    )


async def _known_phone(state: FSMContext, db: Database, max_user_id: int) -> Optional[str]:
    """
    Телефон, который бот уже знает.

    Порядок источников важен: сначала номер текущего диалога (его собрал
    start.py перед входом в воронку), затем привязка к CRM, затем ранее
    оставленная заявка. MAX не отдаёт номер в обычных апдейтах — только
    во вложении-контакте, которое человек присылает сам.
    """
    try:
        data = await state.get_data()
        if data.get("lead_phone"):
            return data["lead_phone"]
    except Exception:
        pass

    try:
        user = db.get_user(max_user_id)
        if user and user.get("phone"):
            return user["phone"]
        for lead in db.get_leads(status=None):
            if lead.get("max_user_id") == max_user_id and lead.get("phone"):
                return lead["phone"]
    except Exception as e:
        logger.debug(f"Не удалось найти известный номер для {max_user_id}: {e}")
    return None


# ==================== ВОЗРАСТ ====================

@router.message(LeadStates.waiting_for_age, F.text)
async def lead_age(message: Msg, state: FSMContext, db: Database) -> None:
    ages = parse_ages(message.text)

    if not ages:
        await message.answer(
            "Не получилось распознать возраст 🙂\n"
            "Напишите его числом — например <b>9</b>. "
            "Если детей несколько, перечислите через пробел: <b>8 11</b>",
            parse_mode="HTML",
        )
        return

    # Направления выбираются ДЛЯ КАЖДОГО РЕБЁНКА ОТДЕЛЬНО. Раньше кнопки
    # были общим объединением по всем возрастам: родитель двоих детей мог
    # выбрать только одно направление, и было непонятно, кому из детей
    # оно предназначено. Теперь дети проходят по очереди.
    children = [{"age": age, "direction": None} for age in ages]
    await state.update_data(lead_children=children, lead_index=0)

    # Черновик дополняется возрастом. Номер в нём уже есть — его записал
    # start.py, когда не нашёл человека в CRM.
    db.upsert_draft_lead(
        message.from_user.id,
        ages=", ".join(str(a) for a in ages),
        full_name=message.from_user.full_name or "",
    )

    if len(children) > 1:
        listed = ", ".join(str(c["age"]) for c in children)
        await message.answer(
            f"Записал: детей {len(children)} — возраст {listed}.\n"
            "Направление подберём для каждого по очереди.",
            parse_mode="HTML",
        )

    await _ask_next_child(
        message, state, db, message.from_user.id, message.from_user.full_name or ""
    )


async def _ask_next_child(
    message: Msg, state: FSMContext, db: Database, user_id: int, user_name: str
) -> None:
    """
    Показывает кнопки направлений для очередного ребёнка. Если у ребёнка
    подходящих направлений нет (возраст вне рамок школы), проставляет
    пометку и переходит к следующему — заявка всё равно должна дойти до
    менеджера.
    """
    data = await state.get_data()
    children = data.get("lead_children") or []
    index = int(data.get("lead_index") or 0)

    while index < len(children):
        age = children[index]["age"]
        codes = directions_for_age(age)

        if codes:
            await state.update_data(lead_index=index)
            header = _child_header(children, index)
            await message.answer(
                f"{header}<b>Выберите направление</b> для ребёнка {age} лет:",
                reply_markup=directions_keyboard(codes, DIRECTIONS),
                parse_mode="HTML",
            )
            return

        children[index]["direction"] = "Уточнить у менеджера"
        await message.answer(
            f"Для возраста {age} лет готовой программы у нас пока нет — занятия "
            f"рассчитаны на {settings.MIN_AGE}–{settings.MAX_AGE} лет (1–8 классы). "
            "Менеджер посмотрит, можно ли что-то предложить.",
            parse_mode="HTML",
        )
        index += 1
        await state.update_data(lead_children=children, lead_index=index)

    await _finish(message, state, db, user_id, user_name)


def _child_header(children, index: int) -> str:
    """«Ребёнок 2 из 3» — только когда детей действительно несколько."""
    if len(children) < 2:
        return ""
    return f"👶 <b>Ребёнок {index + 1} из {len(children)}</b>\n\n"


async def _finish(
    message: Msg, state: FSMContext, db: Database, user_id: int, user_name: str
) -> None:
    """
    Завершение воронки. Телефон уже собран на входе, поэтому заявка
    оформляется сразу; отдельный шаг с номером остаётся только на случай,
    когда состояние потерялось (перезапуск бота).
    """
    data = await state.get_data()
    children = data.get("lead_children") or []

    picked = [
        c for c in children
        if c.get("direction") and c["direction"] != "Уточнить у менеджера"
    ]

    if len(children) > 1:
        chosen = "\n".join(
            f"• {c['age']} лет — {esc(c['direction'] or '—')}" for c in children
        )
        intro = ("Отличный выбор!\n\n" if picked else "Записал:\n\n") + chosen + "\n\n"
    elif picked:
        intro = f"Отличный выбор — <b>{esc(picked[0]['direction'])}</b>!\n\n"
    else:
        # Ни одного направления не выбрано (возраст вне рамок школы) —
        # «Отличный выбор» здесь звучал бы издевательски.
        intro = ""

    phone = await _known_phone(state, db, user_id)
    if phone:
        if intro:
            await message.answer(intro.rstrip(), parse_mode="HTML")
        await _save_lead(message, state, db, phone, user_id, user_name)
        return

    # Запасной путь: номер потерялся вместе с состоянием.
    await state.set_state(LeadStates.waiting_for_phone)
    await message.answer(
        f"{intro}"
        "Наш менеджер свяжется с вами, расскажет про расписание, стоимость и "
        "запишет на пробное занятие.\n\n"
        "📱 <b>Оставьте номер телефона для связи</b> — нажмите кнопку ниже "
        "или напишите номер сообщением.",
        reply_markup=lead_phone_keyboard(),
        parse_mode="HTML",
    )


# ==================== ВЫБОР НАПРАВЛЕНИЯ ====================

@router.callback_query(F.data.startswith("dir:"))
async def lead_direction(callback: Callback, state: FSMContext, db: Database) -> None:
    code = callback.data.partition(":")[2]
    if code not in DIRECTIONS:
        await callback.answer("Направление не найдено.")
        return

    data = await state.get_data()
    children = data.get("lead_children") or []
    index = int(data.get("lead_index") or 0)

    if not children or index >= len(children):
        # Состояние потерялось (перезапуск бота — FSM живёт в памяти).
        await state.set_state(LeadStates.waiting_for_age)
        await callback.message.answer(
            "Кажется, мы потеряли нить разговора 🙂 Напишите, пожалуйста, "
            "сколько лет ребёнку."
        )
        await callback.answer()
        return

    children[index]["direction"] = label(code)
    await state.update_data(lead_children=children, lead_index=index + 1)

    # Выбор сохраняется в черновик сразу же, а не копится до конца воронки.
    db.upsert_draft_lead(
        callback.from_user.id,
        ages=_format_ages(children),
        direction=_format_directions(children),
        full_name=callback.from_user.full_name or "",
    )

    if len(children) > 1:
        await callback.message.answer(
            f"✅ Ребёнку {children[index]['age']} лет — <b>{esc(label(code))}</b>",
            parse_mode="HTML",
        )

    await callback.answer()
    await _ask_next_child(
        callback.message, state, db,
        callback.from_user.id, callback.from_user.full_name or "",
    )


# ==================== ТЕЛЕФОН (ЗАПАСНОЙ ПУТЬ) ====================

@router.message(LeadStates.waiting_for_phone, F.contact)
async def lead_phone_contact(message: Msg, state: FSMContext, db: Database) -> None:
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer(
            "❌ Отправьте свой контакт, а не номер другого человека.",
            reply_markup=lead_phone_keyboard(),
        )
        return
    await _save_lead(
        message, state, db, message.contact.phone_number,
        message.from_user.id, message.from_user.full_name or "",
    )


@router.message(LeadStates.waiting_for_phone, F.text)
async def lead_phone_text(message: Msg, state: FSMContext, db: Database) -> None:
    digits = "".join(c for c in (message.text or "") if c.isdigit())
    if len(digits) < 10:
        await message.answer(
            "Похоже, в номере не хватает цифр. Напишите его целиком — "
            "например <b>+7 900 123-45-67</b> — или нажмите кнопку ниже.",
            reply_markup=lead_phone_keyboard(),
            parse_mode="HTML",
        )
        return
    await _save_lead(
        message, state, db, message.text.strip(),
        message.from_user.id, message.from_user.full_name or "",
    )


# ==================== СОЗДАНИЕ ЗАЯВКИ ====================

async def _save_lead(
    message: Msg,
    state: FSMContext,
    db: Database,
    phone: str,
    user_id: int,
    user_name: str,
) -> None:
    data = await state.get_data()
    children = data.get("lead_children") or []
    await state.clear()

    ages = _format_ages(children) if children else "—"
    direction = _format_directions(children) if children else "Не выбрано"

    # Черновик, заведённый в начале диалога, превращается в заявку —
    # новая строка не создаётся, иначе одно обращение попало бы к
    # менеджеру дважды.
    lead_id = db.promote_draft_lead(
        user_id, phone=phone, ages=ages, direction=direction
    )
    if lead_id is None:
        lead_id = db.create_lead(
            user_id,
            phone=phone, ages=ages, direction=direction,
            full_name=user_name,
        )

    await message.answer(
        "✅ <b>Спасибо! Заявка принята.</b>\n\n"
        f"📞 Телефон: {esc(phone)}\n"
        f"🎯 Направление: {esc(direction)}\n"
        f"👶 Возраст: {esc(ages)}\n\n"
        "Менеджер свяжется с вами в ближайшее рабочее время.\n"
        f"А пока можно посмотреть подробности на сайте: {settings.SCHOOL_SITE}",
        reply_markup=start_keyboard(),
        parse_mode="HTML",
    )

    await _notify_managers(message, db, lead_id, phone, ages, direction, user_name)


async def _notify_managers(
    message: Msg, db: Database, lead_id: int, phone: str, ages: str,
    direction: str, user_name: str,
) -> None:
    recipients = [uid for uid in manager_ids(db) if db.notification_enabled(uid, "updates")]
    if not recipients:
        logger.warning(
            f"⚠️ Заявка №{lead_id} создана, но менеджеров нет — уведомлять некого. "
            f"Пусть менеджер войдёт командой /manager или впишите id в ADMIN_MAX_IDS."
        )
        return

    text = (
        f"🆕 <b>Новая заявка №{lead_id}</b>\n\n"
        f"📅 <b>Получена:</b> {settings.now().strftime('%d.%m.%Y %H:%M')}\n"
        f"👤 <b>Имя в MAX:</b> {esc(user_name or '—')}\n"
        f"📞 <b>Телефон:</b> {esc(phone)}\n"
        f"🎯 <b>Направление:</b> {esc(direction)}\n"
        f"👶 <b>Возраст ребёнка:</b> {esc(ages)}"
    )

    for manager_id in recipients:
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                user_id=mid,
                text=text,
                fmt="html",
                attachments=lead_decision_keyboard(lead_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id} о заявке: {e}")


# ==================== ЗАПАСНОЙ ВЫХОД ====================

@router.message(LeadStates.waiting_for_age)
async def lead_age_fallback(message: Msg) -> None:
    """Прислали не текст (фото, файл) там, где ждём возраст."""
    await message.answer(
        "Напишите, пожалуйста, возраст ребёнка числом — например <b>9</b>.",
        parse_mode="HTML",
    )


@router.message(LeadStates.waiting_for_phone)
async def lead_phone_fallback(message: Msg) -> None:
    await message.answer(
        "Нужен номер телефона — нажмите кнопку ниже или напишите номер сообщением.",
        reply_markup=lead_phone_keyboard(),
    )
