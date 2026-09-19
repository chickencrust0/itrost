"""
bot/notifier.py — журнал активности в боте.

Что изменилось и почему. Раньше этот модуль ПЕРЕСЫЛАЛ менеджеру каждое
обращение к боту: события одного человека копились несколько секунд и
уходили сводкой в чат. Даже в таком виде это оказалось неработающим —
при десятке посетителей лента менеджера состояла почти целиком из
активности, и в ней тонуло то, на что менеджеру действительно надо
отвечать: заявки, обращения в поддержку, заявки на перенос, решения по
неявкам.

Теперь активность никуда не отправляется. Она пишется в таблицу
activity_log и показывается только тогда, когда менеджер сам нажмёт
«👀 Активность в боте» в своём меню (см. bot/handlers/manager.py).

Из этого следует, что здесь больше нет ни буферов, ни таймеров, ни
задачи отложенной отправки: группировать события в момент записи не
нужно — они группируются по человеку при показе
(database.get_recent_activity).

Интерфейс класса сохранён (track / flush_now / shutdown), чтобы
диспетчер и main.py не пришлось менять.
"""

import logging
from typing import Any

import settings
from database import Database

logger = logging.getLogger(__name__)


class ManagerNotifier:
    def __init__(self, bot, db: Database):
        # bot больше не используется для отправки, но остаётся в
        # сигнатуре: main.py собирает нотификатор до диспетчера, и
        # выкидывать аргумент ради одного поля не стоит.
        self.bot = bot
        self.db = db

    # ==================== ПУБЛИЧНОЕ API ====================

    async def track(self, user_id: int, name: str, event: str) -> None:
        """Записать действие пользователя в журнал."""
        if not settings.MANAGER_NOTIFY_ACTIVITY:
            return
        if not user_id or not event:
            return
        # Действия самих менеджеров не пишем: журнал нужен, чтобы видеть
        # посетителей, а не собственные нажатия. Учитываются оба способа
        # стать менеджером — .env и вход по паролю.
        if self._is_manager(user_id):
            return

        try:
            self.db.log_activity(user_id, name or "", event)
        except Exception as e:
            # Журнал второстепенен: сбой записи не должен мешать боту
            # ответить человеку.
            logger.warning(f"Не удалось записать активность {user_id}: {e}")

    async def flush_now(self, user_id: int) -> None:
        """
        Оставлено для совместимости: раньше досылало накопленную сводку
        немедленно (в момент, когда человек оставил номер). Сейчас запись
        и так происходит сразу, досылать нечего.
        """
        return None

    async def shutdown(self) -> None:
        """Тоже ничего не откладывается — досылать при остановке нечего."""
        return None

    # ==================== ВНУТРЕННЕЕ ====================

    def _is_manager(self, user_id: int) -> bool:
        if user_id in settings.MANAGER_IDS:
            return True
        try:
            return user_id in self.db.get_manager_ids()
        except Exception:
            return False


def describe_update(ctx: Any, kind: str) -> str:
    """
    Человекочитаемое описание действия. Менеджеру нужен смысл
    («выбрал направление»), а не служебный payload («dir:chess»).
    """
    if kind == "callback":
        data = getattr(ctx, "data", "") or ""
        known = {
            "auth:new": "нажал «Я впервые»",
            "auth:existing": "нажал «Я уже занимаюсь»",
            "lead:cancel": "отменил заявку",
            "menu:start": "вернулся в начало",
            "menu:support": "начал обращение к администратору",
            "menu:parent:schedule": "открыл расписание",
            "menu:parent:homework": "открыл домашнее задание",
            "menu:parent:balance": "открыл баланс",
            "menu:parent:freezes": "открыл свои заморозки",
            "menu:teacher:schedule": "открыл расписание (преподаватель)",
            "menu:teacher:report": "открыл отчёт (преподаватель)",
        }
        if data in known:
            return known[data]
        if data.startswith("dir:"):
            from bot.directions import label
            return f"выбрал направление: {label(data.partition(':')[2])}"
        if data.startswith("transfer:"):
            return "запросил перенос занятия"
        if data.startswith(("pfrz_no:", "pfrz_yes:")):
            return "замораживает занятие без причины"
        if data.startswith(("pfrz_ok:", "pfrz_health:", "pfrz_other:")):
            return "замораживает занятие по уважительной причине"
        if data.startswith(("att:", "unatt:")):
            return "отметил присутствие ученика"
        if data.startswith(("abs:", "unabs:")):
            return "отметил неявку ученика"
        if data.startswith("cert:"):
            return "прикрепляет справку"
        return f"нажал кнопку: {data}"

    if getattr(ctx, "contact", None):
        return "отправил свой номер телефона"

    text = (getattr(ctx, "text", "") or "").strip()
    if getattr(ctx, "update_type", "") == "bot_started" or text == "/start":
        return "открыл бота"
    if getattr(ctx, "photo", None) or getattr(ctx, "document", None):
        return "прислал файл"
    if not text:
        return "прислал сообщение"
    if len(text) > 200:
        text = text[:200] + "…"
    return f"написал: {text}"
