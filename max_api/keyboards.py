"""
max_api/keyboards.py — сборка inline-клавиатур MAX.

ВАЖНОЕ ОТЛИЧИЕ ОТ TELEGRAM: у MAX нет отдельного «reply-клавиатура» —
постоянного меню кнопок под полем ввода (см. dev.max.ru/docs-api,
раздел «Клавиатура для чат-бота»: единственный вид клавиатуры —
inline, прикреплённая к конкретному сообщению). Поэтому все прежние
ReplyKeyboardMarkup-меню (расписание/ДЗ/баланс и т.д.) в этом боте
реализованы как inline-клавиатуры, прикреплённые к сообщению меню, а
не как постоянная панель.

Ограничения MAX: до 210 кнопок, до 30 рядов, до 7 кнопок в ряду
(до 3, если это link/open_app/request_geo_location/request_contact).
"""

from typing import Any, Dict, List, Optional

Button = Dict[str, Any]
Row = List[Button]
Keyboard = List[Row]


def btn_callback(text: str, payload: str, intent: Optional[str] = None) -> Button:
    btn: Button = {"type": "callback", "text": text, "payload": payload}
    if intent:
        btn["intent"] = intent  # default | positive | negative
    return btn


def btn_link(text: str, url: str) -> Button:
    return {"type": "link", "text": text, "url": url}


def btn_request_contact(text: str) -> Button:
    return {"type": "request_contact", "text": text}


def btn_message(text: str) -> Button:
    """Кнопка, при нажатии отправляющая боту текстовое сообщение (аналог
    reply-кнопки в Telegram: пользователь фактически "печатает" text)."""
    return {"type": "message", "text": text}


def keyboard(rows: Keyboard) -> Dict[str, Any]:
    """Оборачивает ряды кнопок во вложение inline_keyboard для attachments=[...]."""
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def single_row(*buttons: Button) -> Keyboard:
    return [[b] for b in buttons] if len(buttons) > 1 else [[buttons[0]]] if buttons else []


# ==================== ГОТОВЫЕ КЛАВИАТУРЫ БОТА ====================

def request_phone_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([[btn_request_contact("📱 Поделиться контактом")]])]


def start_keyboard() -> List[Dict[str, Any]]:
    """
    Единственная кнопка в тупиковых точках диалога — вернуться в начало.

    Кнопки «Отмена» здесь сознательно нет: любое взаимодействие с ботом
    возможно только после того, как оставлен номер, поэтому «отменить»
    означало бы оставить человека в состоянии, где бот всё равно ничего
    не может сделать.
    """
    return [keyboard([[btn_callback("🔄 В начало", "menu:start")]])]


def directions_keyboard(codes, labels) -> List[Dict[str, Any]]:
    """Кнопки направлений + ссылка на сайт последней строкой."""
    import settings

    rows = [[btn_callback(labels[code], f"dir:{code}")] for code in codes]
    rows.append([btn_link("🌐 Подробнее на сайте", settings.SCHOOL_SITE)])
    return [keyboard(rows)]


def lead_phone_keyboard() -> List[Dict[str, Any]]:
    # Без «Отмены»: без номера бот всё равно ничего не сможет сделать.
    return [keyboard([[btn_request_contact("📱 Отправить мой номер")]])]


def lead_decision_keyboard(lead_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([[
        btn_callback("✅ Обработана", f"lead_ok:{lead_id}", intent="positive"),
        btn_callback("❌ Отклонить", f"lead_no:{lead_id}", intent="negative"),
    ]])]


def teacher_menu_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("📅 Моё расписание", "menu:teacher:schedule")],
        # Преподаватель может заморозить занятие так же, как родитель:
        # родители часто договариваются напрямую с педагогом, и без этого
        # пункта педагогу приходилось пересылать просьбу менеджеру.
        [btn_callback("❄️ Заморозить занятие", "menu:teacher:freeze")],
        [btn_callback("📊 Отчёт по урокам", "menu:teacher:report")],
        [btn_callback("👤 Связаться с администратором", "menu:support")],
        [btn_callback("🔔 Настройки уведомлений", "menu:notifications")],
        [btn_callback("🚪 Выйти из профиля", "menu:logout")],
    ])]


def parent_menu_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("📅 Расписание", "menu:parent:schedule")],
        [btn_callback("📚 Домашнее задание", "menu:parent:homework")],
        # «Баланс» переименован в «Остаток занятий»: денег бот не
        # показывает вовсе, а слово «баланс» заставляло родителей искать
        # там оплату.
        [btn_callback("🎟 Остаток занятий", "menu:parent:balance")],
        # Заморозка вынесена в отдельный пункт меню. Раньше кнопки
        # заморозки висели под каждой карточкой расписания — родителю
        # было неочевидно, что заморозка живёт именно там.
        [btn_callback("❄️ Заморозить занятие", "menu:parent:freeze")],
        [btn_callback("📋 Мои заморозки", "menu:parent:freezes")],
        [btn_callback("👤 Связаться с администратором", "menu:support")],
        [btn_callback("🔔 Настройки уведомлений", "menu:notifications")],
        [btn_callback("🚪 Выйти из профиля", "menu:logout")],
    ])]


def manager_menu_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("📢 Рассылка", "menu:manager:broadcast")],
        [btn_callback("📋 Заявки с сайта и бота", "menu:manager:leads")],
        [btn_callback("🔁 Заявки на перенос", "menu:manager:transfers")],
        [btn_callback("📊 Сводка за период", "menu:manager:summary")],
        [btn_callback("❌ Неявки за сегодня", "menu:manager:absences")],
        [btn_callback("❄️ Заморозки и справки", "menu:manager:freezes")],
        [btn_callback("👤 Обращения", "menu:manager:support")],
        [btn_callback("👀 Активность в боте", "menu:manager:activity")],
        [btn_callback("🔐 Кто вошёл в бота", "menu:manager:logins")],
        [btn_callback("🔔 Настройки уведомлений", "menu:notifications")],
        [btn_callback("🚪 Выйти из профиля", "menu:logout")],
    ])]


def notification_settings_keyboard(preferences: Dict[str, bool], role: str) -> List[Dict[str, Any]]:
    labels = {
        "lessons": "Расписание и напоминания об уроках",
        "summaries": "Ежедневные сводки",
        "updates": "Заявки и события занятий",
        "broadcasts": "Массовые рассылки",
    }
    categories = ("summaries", "updates", "broadcasts") if role == "manager" else (
        "lessons", "updates", "broadcasts"
    )
    rows = [
        [btn_callback(f"{'✅' if preferences[key] else '🔕'} {labels[key]}", f"notifications:{key}")]
        for key in categories
    ]
    rows.append([btn_callback("⬅️ В меню", "menu:start")])
    return [keyboard(rows)]


def lesson_action_keyboard(lesson_id: Any) -> List[Dict[str, Any]]:
    """
    Действия по занятию, когда отмечать некого (у занятия нет учеников).

    Кнопки «✅ Отметить как проведённый» здесь БОЛЬШЕ НЕТ: школе нужна
    только посещаемость конкретных детей, а отметка «проведено»
    дублировала её и добавляла преподавателю лишний шаг. Payload
    `close:` остался обработанным в bot/handlers/teacher.py — сообщения
    со старыми кнопками живут в истории чата сколько угодно долго.
    """
    return [keyboard([
        [btn_callback("📝 Прикрепить ДЗ", f"hw:{lesson_id}")],
        [btn_callback("🔁 Заявка на перенос", f"transfer:{lesson_id}")],
    ])]


def lesson_attendance_keyboard(
    lesson_id: Any,
    students: List[Any],
    marked: Optional[set] = None,
    absent: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Карточка занятия у преподавателя: на каждого ученика ряд из двух
    кнопок — «пришёл» и «не пришёл» — плюс общие действия по занятию.

    students — список (client_id, имя). marked — уже отмеченные
    присутствующими (пишется в CRM). absent — отмеченные неявкой
    (хранится в БД бота; в CRM уходит только решением менеджера).
    """
    marked = marked or set()
    absent = absent or set()
    rows: Keyboard = []
    for client_id, name in students:
        short = name if len(name) <= 18 else name[:17] + "…"
        if client_id in marked:
            rows.append([
                btn_callback(f"✅ {short}", f"unatt:{lesson_id}:{client_id}", intent="positive"),
                btn_callback("❌", f"abs:{lesson_id}:{client_id}"),
            ])
        elif client_id in absent:
            rows.append([
                btn_callback("✅", f"att:{lesson_id}:{client_id}"),
                btn_callback(f"❌ {short}", f"unabs:{lesson_id}:{client_id}", intent="negative"),
            ])
        else:
            rows.append([
                btn_callback(f"✅ {short}", f"att:{lesson_id}:{client_id}"),
                btn_callback("❌", f"abs:{lesson_id}:{client_id}"),
            ])

    rows.append([btn_callback("📝 Прикрепить ДЗ", f"hw:{lesson_id}")])
    rows.append([btn_callback("🔁 Заявка на перенос", f"transfer:{lesson_id}")])
    return [keyboard(rows)]


def absence_decision_keyboard(absence_id: Any) -> List[Dict[str, Any]]:
    """
    Решение менеджера по неявке. Три исхода, они по-разному влияют на
    абонемент:
      * сгорело — занятие списывается (burn_one), посещений становится меньше;
      * беспричинная заморозка — занятие остаётся, но расходуется одна
        беспричинная заморозка клиента (счётчик в поле адреса проживания);
      * уважительная — не трогаем ни занятия, ни счётчик заморозок.
    """
    return [keyboard([
        [btn_callback("🔥 Занятие сгорело", f"burn:{absence_id}", intent="negative")],
        [btn_callback("❄️ Заморозка без причины", f"frz_no:{absence_id}")],
        [btn_callback("🙏 Уважительная причина", f"frz_ok:{absence_id}", intent="positive")],
    ])]


def lesson_freeze_keyboard(lesson_id: Any) -> List[Dict[str, Any]]:
    """
    Действия родителя по конкретному занятию в расписании.

    Прикрепляется только к занятиям, до начала которых осталось не меньше
    settings.FREEZE_DEADLINE_HOURS часов (см. bot/handlers/parent.py):
    позже заморозка невозможна, и кнопка вводила бы в заблуждение.
    """
    return [keyboard([
        [btn_callback("❄️ Заморозить без причины", f"pfrz_no:{lesson_id}")],
        [btn_callback("🙏 Заморозить по уважительной", f"pfrz_ok:{lesson_id}")],
    ])]


def freeze_confirm_keyboard(lesson_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("✅ Да, заморозить", f"pfrz_yes:{lesson_id}", intent="negative")],
        [btn_callback("❌ Отмена", f"pfrz_cancel:{lesson_id}")],
    ])]


def freeze_reason_keyboard(lesson_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("🏥 По состоянию здоровья", f"pfrz_health:{lesson_id}")],
        [btn_callback("📝 Другая причина", f"pfrz_other:{lesson_id}")],
    ])]


def certificate_upload_keyboard(freeze_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([[btn_callback("📎 Прикрепить справку", f"cert:{freeze_id}")]])]


def contact_admin_keyboard() -> List[Dict[str, Any]]:
    """Та же кнопка связи, что и в стартовом меню."""
    return [keyboard([[btn_callback("👤 Связаться с администратором", "menu:support")]])]


def support_user_keyboard(ticket_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([[btn_callback("🔒 Завершить обращение", f"sup_close:{ticket_id}")]])]


def support_manager_keyboard(ticket_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("✍️ Ответить", f"sup_reply:{ticket_id}")],
        [btn_callback("🔒 Закрыть обращение", f"sup_close:{ticket_id}")],
    ])]


def transfer_decision_keyboard(request_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [
            btn_callback("✅ Подтвердить", f"transfer_ok:{request_id}", intent="positive"),
            btn_callback("❌ Отклонить", f"transfer_no:{request_id}", intent="negative"),
        ]
    ])]


def confirm_logout_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([[
        btn_callback("✅ Да, выйти", "logout:confirm", intent="positive"),
        btn_callback("❌ Отмена", "logout:cancel", intent="negative"),
    ]])]


def schedule_period_keyboard() -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("📅 Сегодня", "schedule:today")],
        [btn_callback("📅 Завтра", "schedule:tomorrow")],
        [btn_callback("📅 Неделя", "schedule:week")],
        [btn_callback("📅 Месяц", "schedule:month")],
        [btn_callback("📅 Свой период", "schedule:custom")],
    ])]


def parent_schedule_period_keyboard() -> List[Dict[str, Any]]:
    """
    То же, что schedule_period_keyboard, но с префиксом pschedule:.

    Префикс разный намеренно: payload schedule:* обрабатывает роутер
    преподавателя, и родитель, нажав такую кнопку, получил бы отказ
    «только для преподавателей».
    """
    return [keyboard([
        [btn_callback("📅 Сегодня", "pschedule:today")],
        [btn_callback("📅 Завтра", "pschedule:tomorrow")],
        [btn_callback("📅 Неделя", "pschedule:week")],
        [btn_callback("📅 Месяц", "pschedule:month")],
        [btn_callback("📅 Свой период", "pschedule:custom")],
    ])]


def recipients_keyboard() -> List[Dict[str, Any]]:
    """
    Кому отправить рассылку.

    База разделена на две принципиально разные аудитории:
      * ЗАРЕГИСТРИРОВАННЫЕ — те, кто вошёл в бота и найден в CRM
        (родители и преподаватели). Им пишут про занятия и расписание.
      * НОВЫЕ — те, кто оставил заявку, но в CRM ещё не заведён. Им
        пишут про пробные занятия и набор — текст для действующих
        клиентов им не подходит и выглядит как ошибка.
    """
    return [keyboard([
        [btn_callback("🆕 Новым клиентам (заявки)", "broadcast:lead")],
        [btn_callback("✅ Зарегистрированным (все)", "broadcast:registered")],
        [btn_callback("👤 Родителям", "broadcast:parent")],
        [btn_callback("👨‍🏫 Преподавателям", "broadcast:teacher")],
        [btn_callback("📢 Вообще всем", "broadcast:all")],
        [btn_callback("Отмена", "broadcast:cancel")],
    ])]


# ==================== ЗАМОРОЗКА ПРЕПОДАВАТЕЛЕМ ====================
#
# Преподавателю нужен лишний шаг по сравнению с родителем: родитель
# морозит занятие своего ребёнка, а у преподавателя в занятии несколько
# детей, и сперва надо выбрать, чьё занятие морозим.

def teacher_freeze_students_keyboard(
    lesson_id: Any, students: List[Any]
) -> List[Dict[str, Any]]:
    """
    Ученики занятия — по кнопке на каждого, как в отметке посещаемости.

    Заморозка всегда касается КОНКРЕТНОГО ребёнка: у него расходуется
    счётчик и ему возвращается занятие. Общей кнопки «заморозить
    занятие» здесь нет намеренно — она создавала впечатление, что
    морозится вся группа сразу.
    """
    rows: Keyboard = [
        [btn_callback(_short(name), f"tfrz_pick:{lesson_id}:{client_id}")]
        for client_id, name in students
    ]
    rows.append([btn_callback("❌ Отмена", "menu:teacher:cancel")])
    return [keyboard(rows)]


def teacher_freeze_reason_keyboard(lesson_id: Any, client_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("❄️ Без причины", f"tfrz_no:{lesson_id}:{client_id}")],
        [btn_callback("🙏 По уважительной причине", f"tfrz_ok:{lesson_id}:{client_id}")],
        [btn_callback("❌ Отмена", "menu:teacher:cancel")],
    ])]


def teacher_freeze_confirm_keyboard(lesson_id: Any, client_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("✅ Да, заморозить", f"tfrz_yes:{lesson_id}:{client_id}", intent="negative")],
        [btn_callback("❌ Отмена", "menu:teacher:cancel")],
    ])]


def teacher_freeze_valid_keyboard(lesson_id: Any, client_id: Any) -> List[Dict[str, Any]]:
    return [keyboard([
        [btn_callback("🏥 По состоянию здоровья", f"tfrz_health:{lesson_id}:{client_id}")],
        [btn_callback("📝 Другая причина", f"tfrz_other:{lesson_id}:{client_id}")],
    ])]


# ==================== ВЫБОР УЧЕНИКА ДЛЯ ДЗ ====================

def homework_targets_keyboard(
    lesson_id: Any, students: List[Any]
) -> List[Dict[str, Any]]:
    """
    Кому прикрепить домашнее задание.

    Первой строкой — «всем сразу»: одинаковое ДЗ на группу это самый
    частый случай, и заставлять педагога проходить его по одному ребёнку
    означало бы гарантированно получить пропуски.
    """
    rows: Keyboard = [[btn_callback("👥 Всем в группе", f"hw_all:{lesson_id}")]]
    rows += [
        [btn_callback(_short(name), f"hw_one:{lesson_id}:{client_id}")]
        for client_id, name in students
    ]
    rows.append([btn_callback("❌ Отмена", "menu:teacher:cancel")])
    return [keyboard(rows)]


def _short(name: Any, limit: int = 24) -> str:
    text = str(name or "—")
    return text if len(text) <= limit else text[: limit - 1] + "…"
