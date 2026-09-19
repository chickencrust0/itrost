"""
bot/states.py — имена состояний FSM (простые строки вместо aiogram
StatesGroup/State — этого достаточно для нашего плоского хранилища).

Разделение TeacherTransferStates/ParentTransferStates сохранено таким
же, каким его сделали в исходном боте: раньше оба сценария использовали
одно состояние и обработчики пересекались (см. память проекта).
"""


class DateRangeStates:
    waiting_for_date_from = "daterange:waiting_for_date_from"
    waiting_for_date_to = "daterange:waiting_for_date_to"


class ParentDateRangeStates:
    """
    Свой период у родителя.

    Состояния отдельные от DateRangeStates: у преподавателя обработчик
    ввода дат ведёт в его расписание, и общий набор состояний означал бы,
    что родитель, введя дату, получит расписание преподавателя.
    """
    waiting_for_date_from = "parent_daterange:waiting_for_date_from"
    waiting_for_date_to = "parent_daterange:waiting_for_date_to"


class HomeworkStates:
    waiting_for_text_or_file = "homework:waiting_for_text_or_file"


class TeacherTransferStates:
    waiting_for_comment = "teacher_transfer:waiting_for_comment"


class ParentTransferStates:
    waiting_for_comment = "parent_transfer:waiting_for_comment"


class BroadcastStates:
    waiting_for_content = "broadcast:waiting_for_content"
    waiting_for_recipient = "broadcast:waiting_for_recipient"


class ManagerSummaryStates:
    waiting_for_date_from = "manager_summary:waiting_for_date_from"
    waiting_for_date_to = "manager_summary:waiting_for_date_to"


class LeadStates:
    """Воронка нового посетителя: возраст -> направление -> телефон."""
    waiting_for_age = "lead:waiting_for_age"
    waiting_for_phone = "lead:waiting_for_phone"


class LoginStates:
    """
    Ожидание телефона от того, кто уже занимается.

    Отдельное состояние нужно, чтобы отличать «прислал телефон для входа»
    от «прислал телефон в заявке»: раньше любой присланный номер
    трактовался как попытка входа.
    """
    waiting_for_phone = "login:waiting_for_phone"


class SupportStates:
    """Пользователь в диалоге с менеджером: пока состояние активно, весь
    текст уходит в обращение, а не в другие хендлеры."""
    chatting = "support:chatting"


class ManagerReplyStates:
    waiting_for_reply = "manager_support:waiting_for_reply"


class ManagerLoginStates:
    """Вход менеджера по паролю (/manager) — без правки .env."""
    waiting_for_password = "manager_login:waiting_for_password"


class FreezeStates:
    """Прикрепление справки к заморозке по уважительной причине."""
    waiting_for_certificate = "freeze:waiting_for_certificate"
