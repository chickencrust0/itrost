"""
bot/dispatcher.py — минимальный Router/Dispatcher вместо aiogram.

Поддерживает ровно то подмножество возможностей aiogram, которое
использовалось в исходном боте: фильтры по тексту (regexp/наличие),
по наличию contact/document/photo, по callback-данным (== / startswith),
привязку хендлера к состоянию FSM и последующему тексту, плюс
инъекцию зависимостей в хендлер по именам параметров.

Меню бота в MAX реализованы через inline-кнопки (см. max_api/keyboards.py
и докстринг там же), поэтому в отличие от исходного бота на Telegram
здесь нет фильтров вида F.text == "<подпись кнопки>" — вместо них
хендлеры меню триггерятся через F.data == "menu:...".
"""

import inspect
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Union

from max_api.client import MaxBot
from max_api.context import Callback, Msg
import settings
from bot.fsm import FSMContext
from bot.notifier import describe_update

logger = logging.getLogger(__name__)

CondFn = Callable[[Any], bool]


class Cond:
    """Обёртка над предикатом ctx -> bool с поддержкой | и &, как у aiogram F."""

    def __init__(self, fn: CondFn):
        self.fn = fn

    def __call__(self, ctx: Any) -> bool:
        try:
            return bool(self.fn(ctx))
        except Exception:
            return False

    def __or__(self, other: "Cond") -> "Cond":
        return Cond(lambda ctx: self.fn(ctx) or other.fn(ctx))

    def __and__(self, other: "Cond") -> "Cond":
        return Cond(lambda ctx: self.fn(ctx) and other.fn(ctx))


class _TextField(Cond):
    """
    F.text — и самостоятельный фильтр («есть непустой текст»), и точка
    входа для F.text.regexp(...).

    БЫЛО: обычный класс без __call__. Любой хендлер с фильтром F.text
    (ввод дат в сводке менеджера, текст рассылки, комментарий к заявке
    на перенос, текст ДЗ) падал в диспетчере с
    TypeError: '_TextField' object is not callable — исключение
    гасилось общим except, и сообщение молча пропадало. Внешне это
    выглядело как «бот не отвечает на ввод».
    """

    def __init__(self):
        super().__init__(lambda ctx: bool((getattr(ctx, "text", "") or "").strip()))

    def regexp(self, pattern: str) -> Cond:
        compiled = re.compile(pattern)
        return Cond(lambda ctx: bool(compiled.match(getattr(ctx, "text", "") or "")))


class _DataField(Cond):
    """F.data — фильтр «есть непустой payload» плюс == и startswith."""

    def __init__(self):
        super().__init__(lambda ctx: bool(getattr(ctx, "data", "") or ""))

    def __eq__(self, other) -> Cond:  # type: ignore[override]
        return Cond(lambda ctx: getattr(ctx, "data", "") == other)

    def __hash__(self):  # __eq__ переопределён — иначе объект нехешируем
        return id(self)

    def startswith(self, prefix: str) -> Cond:
        return Cond(lambda ctx: (getattr(ctx, "data", "") or "").startswith(prefix))


class _F:
    text = _TextField()
    data = _DataField()
    # F.any — совпадает всегда: нужен там, где хендлер привязан только к
    # состоянию FSM и принимает любое сообщение.
    any = Cond(lambda ctx: True)
    contact = Cond(lambda ctx: getattr(ctx, "contact", None) is not None)
    document = Cond(lambda ctx: getattr(ctx, "document", None) is not None)
    photo = Cond(lambda ctx: getattr(ctx, "photo", None) is not None)


F = _F()


def CommandStart() -> Cond:
    return Cond(
        lambda ctx: getattr(ctx, "update_type", "") == "bot_started"
        or (getattr(ctx, "text", "") or "").strip().lower() == "/start"
    )


def Command(*names: str) -> Cond:
    """
    Фильтр по команде: Command("id") ловит «/id» и «/id@botname».
    Регистр не важен — в мобильном клиенте легко получить «/ID».
    """
    wanted = {n.lower().lstrip("/") for n in names}

    def check(ctx) -> bool:
        text = (getattr(ctx, "text", "") or "").strip()
        if not text.startswith("/"):
            return False
        head = text.split()[0][1:].split("@")[0].lower()
        return head in wanted

    return Cond(check)


Filter = Union[str, Cond]


class _Entry:
    __slots__ = ("filters", "func")

    def __init__(self, filters: List[Filter], func: Callable):
        self.filters = filters
        self.func = func


class Router:
    def __init__(self, name: str = ""):
        self.name = name
        self._message_entries: List[_Entry] = []
        self._callback_entries: List[_Entry] = []

    def message(self, *filters: Filter):
        def deco(func: Callable) -> Callable:
            self._message_entries.append(_Entry(list(filters), func))
            return func
        return deco

    def callback_query(self, *filters: Filter):
        def deco(func: Callable) -> Callable:
            self._callback_entries.append(_Entry(list(filters), func))
            return func
        return deco


def _matches(entry: _Entry, ctx: Any, current_state: Optional[str]) -> bool:
    for f in entry.filters:
        if isinstance(f, str):
            if current_state != f:
                return False
        else:
            if not f(ctx):
                return False
    return True


class Dispatcher:
    def __init__(self, bot: MaxBot, **workflow_data: Any):
        self.bot = bot
        self.workflow_data = workflow_data
        self._routers: List[Router] = []

    def include_router(self, router: Router) -> None:
        self._routers.append(router)

    async def _require_phone(self, ctx: Any, state: FSMContext) -> bool:
        """Block every workflow until this visitor has supplied a phone number."""
        db = self.workflow_data.get("db")
        user = db.get_user(ctx.from_user.id) if db is not None else None
        if user and (user.get("phone") or "").strip():
            return True
        data = await state.get_data()
        if (data.get("lead_phone") or "").strip():
            return True
        if db is not None:
            try:
                if any(r.get("max_user_id") == ctx.from_user.id and r.get("phone") for r in db.get_leads(None)):
                    return True
            except Exception:
                logger.exception("Не удалось проверить ранее сохранённый телефон")
        # Contact and a manually typed number are the only pre-auth inputs.
        if getattr(ctx, "contact", None) is not None:
            return True
        text = (getattr(ctx, "text", "") or "").strip()
        if re.fullmatch(r"\+?\d[\d\-()\s]{8,}", text) and len(re.sub(r"\D", "", text)) >= 10:
            return True
        await state.clear()
        await state.set_state("login:waiting_for_phone")
        from max_api.keyboards import request_phone_keyboard
        target = getattr(ctx, "message", None) or ctx
        await target.answer(
            "📱 <b>Сначала отправьте номер телефона.</b>\\n\\n"
            "Без номера остальные функции бота недоступны. Нажмите кнопку ниже "
            "или напишите номер сообщением.",
            reply_markup=request_phone_keyboard(), parse_mode="HTML",
        )
        try:
            answer = getattr(ctx, "answer", None)
            if getattr(ctx, "data", None) is not None and answer:
                await answer("Сначала нужен номер телефона")
        except Exception:
            pass
        return False

    async def _call(self, func: Callable, ctx: Any, ctx_kind: str, user_id: int) -> None:
        sig = inspect.signature(func)
        available: Dict[str, Any] = dict(self.workflow_data)
        available["state"] = FSMContext(user_id)
        available[ctx_kind] = ctx  # "message" или "callback"
        kwargs = {name: available[name] for name in sig.parameters if name in available}
        try:
            await func(**kwargs)
        except Exception:
            # Раньше ошибка только писалась в лог, и человек не получал
            # НИЧЕГО: нажатая кнопка выглядела как неработающая, а причина
            # была видна лишь тому, кто смотрит логи сервера. Теперь
            # пользователю приходит внятный ответ.
            logger.exception(f"Ошибка в хендлере {func.__module__}.{func.__name__}")
            await self._report_failure(
                ctx, f"{func.__module__}.{func.__name__}"
            )

    @staticmethod
    async def _report_failure(ctx: Any, where: str) -> None:
        """Сообщить человеку о сбое, не раскрывая внутренностей."""
        try:
            target = getattr(ctx, "message", None) or ctx
            answer = getattr(target, "answer", None)
            if answer is None:
                return
            await answer(
                "⚠️ Не удалось выполнить действие — произошла ошибка.\n"
                "Попробуйте ещё раз или напишите менеджеру."
            )
        except Exception:
            logger.exception(f"Не удалось сообщить об ошибке в {where}")

    @staticmethod
    async def _report_unknown_button(ctx: Any) -> None:
        try:
            await ctx.answer("Кнопка устарела")
            await ctx.message.answer(
                "🔄 Эта кнопка из старого сообщения и больше не действует.\n"
                "Отправьте /start, чтобы открыть актуальное меню."
            )
        except Exception:
            logger.exception("Не удалось ответить на неизвестную кнопку")

    @staticmethod
    def _log_identity(ctx) -> None:
        """
        Пишет в лог MAX id отправителя. Нужно на первом запуске: чтобы
        заполнить ADMIN_MAX_IDS, свой id надо откуда-то взять, а до
        заполнения ADMIN_MAX_IDS меню менеджера недоступно.

        Пока список менеджеров пуст, пишем на уровне INFO с подсказкой —
        человек ставит бота и сразу видит нужное число. Как только
        ADMIN_MAX_IDS заполнен, это уходит в DEBUG и не засоряет лог.
        """
        user = ctx.from_user
        if settings.MANAGER_IDS:
            logger.debug(f"Апдейт от {user.full_name} (MAX id {user.id})")
        else:
            logger.info(
                f"🆔 Сообщение от «{user.full_name}» — MAX id: {user.id}. "
                "Впишите его в ADMIN_MAX_IDS в .env, чтобы получить права менеджера."
            )

    async def _track(self, ctx, kind: str) -> None:
        """
        Уведомление менеджеру об обращении. Вынесено сюда, а не в
        отдельные хендлеры, чтобы попадало ЛЮБОЕ взаимодействие, включая
        те, на которые нет обработчика.

        Ошибки уведомления не должны мешать основной работе: менеджер
        может быть недоступен, а ответить человеку бот обязан.
        """
        notifier = self.workflow_data.get("notifier")
        if notifier is None:
            return
        try:
            await notifier.track(
                ctx.from_user.id,
                ctx.from_user.full_name or "",
                describe_update(ctx, kind),
            )
        except Exception:
            logger.exception("Не удалось передать уведомление менеджеру")

    async def feed_update(self, update: Dict[str, Any]) -> None:
        update_type = update.get("update_type", "")
        try:
            if update_type in ("message_created", "bot_started"):
                ctx = Msg(self.bot, update)
                if not ctx.from_user.id:
                    return
                self._log_identity(ctx)
                await self._track(ctx, "message")
                state = FSMContext(ctx.from_user.id)
                if not await self._require_phone(ctx, state):
                    return
                current_state = await state.get_state()
                for router in self._routers:
                    for entry in router._message_entries:
                        if _matches(entry, ctx, current_state):
                            await self._call(entry.func, ctx, "message", ctx.from_user.id)
                            return
                # Ни один обработчик не подошёл. Раньше это проходило
                # совершенно бесшумно, и понять, почему бот молчит, было
                # невозможно даже по логам.
                logger.warning(
                    f"🤷 Сообщение без обработчика: текст={ctx.text[:80]!r}, "
                    f"состояние={current_state}, от {ctx.from_user.id}"
                )
            elif update_type == "message_callback":
                ctx = Callback(self.bot, update)
                if not ctx.from_user.id:
                    return
                self._log_identity(ctx)
                await self._track(ctx, "callback")
                state = FSMContext(ctx.from_user.id)
                if not await self._require_phone(ctx, state):
                    return
                current_state = await state.get_state()
                for router in self._routers:
                    for entry in router._callback_entries:
                        if _matches(entry, ctx, current_state):
                            await self._call(entry.func, ctx, "callback", ctx.from_user.id)
                            return
                # Нажата кнопка, для которой нет обработчика: чаще всего
                # это старое сообщение из истории чата, оставшееся от
                # предыдущей версии бота. Молчание здесь выглядит как
                # «кнопка не работает».
                logger.warning(
                    f"🤷 Кнопка без обработчика: payload={ctx.data!r}, "
                    f"состояние={current_state}, от {ctx.from_user.id}"
                )
                await self._report_unknown_button(ctx)
            # Прочие типы апдейтов (bot_added, chat_title_changed и т.п.) этому
            # боту не нужны — он работает только в личных диалогах 1-на-1.
        except Exception:
            logger.exception(f"Необработанная ошибка при обработке апдейта {update_type}")
