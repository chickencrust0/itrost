"""
max_api/context.py — обёртки над сырым JSON апдейтов MAX.

Даёт хендлерам интерфейс, похожий на aiogram (message.answer(...),
callback.message.edit_text(...)), чтобы логика хендлеров читалась так
же, как в исходном боте на Telegram.

Важное архитектурное решение: бот общается с людьми только 1-на-1
(учитель/родитель/менеджер), групповых чатов не предполагается. Поэтому
везде в качестве адресата используется user_id отправителя, а не
chat_id диалога — это снимает неоднозначность вокруг chat_id, которая
в официальной документации MAX объясняется отдельно и только для
групповых чатов/каналов.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FromUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.username or str(self.id)


@dataclass
class Contact:
    phone_number: str
    user_id: Optional[int] = None


@dataclass
class FileRef:
    token: str
    file_name: str
    kind: str  # "image" | "file" | "video" | "audio"


def _parse_user(raw: Dict[str, Any]) -> FromUser:
    return FromUser(
        id=raw.get("user_id") or raw.get("id") or 0,
        first_name=raw.get("first_name") or raw.get("name") or "",
        last_name=raw.get("last_name") or "",
        username=raw.get("username"),
    )


class Msg:
    """Обёртка над Update типа message_created / bot_started и т.п."""

    def __init__(self, bot: "MaxBot", update: Dict[str, Any]):  # noqa: F821
        self.bot = bot
        self.raw = update
        self.update_type = update.get("update_type", "")

        message = update.get("message") or {}
        sender = message.get("sender") or update.get("user") or {}
        self.from_user = _parse_user(sender)

        body = message.get("body") or {}
        self.message_id: Optional[str] = body.get("mid")
        self.text: str = (body.get("text") or "") if self.update_type != "bot_started" else ""

        self.contact: Optional[Contact] = None
        self.document: Optional[FileRef] = None
        self.photo: Optional[FileRef] = None

        for att in body.get("attachments") or []:
            a_type = att.get("type")
            payload = att.get("payload") or {}
            if a_type == "contact":
                phone = _extract_contact_phone(payload)
                if phone:
                    self.contact = Contact(
                        phone_number=phone,
                        user_id=(payload.get("max_info") or {}).get("user_id"),
                    )
            elif a_type == "image":
                # Предполагается, что token входящего вложения так же годится
                # для повторной отправки через attachments=[{token: ...}],
                # как и token, полученный от POST /uploads (см. пример
                # ImageAttachment({token: 'existingImageToken'}) в
                # библиотеке JS) — отдельно вендором не подтверждено.
                self.photo = FileRef(
                    token=payload.get("token", ""), file_name="photo.jpg", kind="image"
                )
            elif a_type == "file":
                self.document = FileRef(
                    token=payload.get("token", ""),
                    file_name=payload.get("filename") or "file",
                    kind="file",
                )

        # Адресат ответа — сам отправитель (см. докстринг модуля).
        self._reply_user_id = self.from_user.id

    async def answer(
        self,
        text: str,
        reply_markup: Optional[List[Dict[str, Any]]] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> "Msg":
        result = await self.bot.send_message(
            user_id=self._reply_user_id,
            text=text,
            attachments=reply_markup,
            fmt=(parse_mode or "").lower() or None,
        )
        return _sent_message_stub(self.bot, self._reply_user_id, result)

    async def answer_photo(self, token: str, caption: str = "") -> None:
        await self.bot.send_message(
            user_id=self._reply_user_id,
            text=caption,
            attachments=[{"type": "image", "payload": {"token": token}}],
        )

    async def answer_document(self, token: str, caption: str = "") -> None:
        await self.bot.send_message(
            user_id=self._reply_user_id,
            text=caption,
            attachments=[{"type": "file", "payload": {"token": token}}],
        )


def _extract_contact_phone(payload: Dict[str, Any]) -> Optional[str]:
    """
    Телефон приходит внутри vcf_info (см. dev.max.ru, «Кнопка
    request_contact»), а не отдельным полем. Достаём номер простым
    разбором TEL-строки vCard.
    """
    vcf = payload.get("vcf_info") or ""
    for line in vcf.replace("\\r\\n", "\n").split("\n"):
        if line.upper().startswith("TEL"):
            return line.split(":")[-1].strip()
    return payload.get("phone") or None


def _sent_message_stub(bot, user_id: int, result: Dict[str, Any]) -> "SentMessage":
    message = (result or {}).get("message") or {}
    mid = (message.get("body") or {}).get("mid")
    return SentMessage(bot=bot, user_id=user_id, message_id=mid, raw=result)


class SentMessage:
    """Возвращается из Msg.answer(...) — позволяет дальше редактировать
    отправленное ботом сообщение (аналог возврата message.answer() в aiogram)."""

    def __init__(self, bot, user_id: int, message_id: Optional[str], raw: Dict[str, Any]):
        self.bot = bot
        self.user_id = user_id
        self.message_id = message_id
        self.raw = raw

    async def edit_text(
        self, text: str, reply_markup: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        if not self.message_id:
            return
        await self.bot.edit_message(self.message_id, text=text, attachments=reply_markup)


class Callback:
    """Обёртка над Update типа message_callback."""

    def __init__(self, bot, update: Dict[str, Any]):
        self.bot = bot
        self.raw = update
        callback = update.get("callback") or {}
        self.callback_id: str = callback.get("callback_id", "")
        self.data: str = callback.get("payload", "") or ""
        self.from_user = _parse_user(callback.get("user") or update.get("user") or {})

        message = update.get("message") or {}
        self.message = CallbackMessage(bot, message, self.from_user.id)

    async def answer(self, text: Optional[str] = None, show_alert: bool = False) -> None:
        """
        Подтверждение нажатия кнопки.

        Без текста НЕ отправляет ничего: MAX требует, чтобы в теле
        POST /answers было `notification` или `message`, и пустой вызов
        возвращает 400. Раньше он делался всегда — и ронял обработчик на
        первой же строке.

        С текстом уходит `notification` — всплывающее уведомление, как
        callback.answer(text) в Telegram. Длинный текст в уведомление не
        помещается, поэтому он досылается обычным сообщением.

        Метод НЕ поднимает исключений: подтверждение нажатия
        второстепенно, и сбой на нём не должен мешать обработчику
        показать человеку то, ради чего кнопка нажималась.
        """
        if not text:
            return

        try:
            await self.bot.answer_callback(self.callback_id, notification=text)
            if len(text) > 200:
                await self.bot.send_message(user_id=self.from_user.id, text=text)
        except Exception as e:
            logger.warning(f"Не удалось ответить на нажатие кнопки: {e}")
            try:
                await self.bot.send_message(user_id=self.from_user.id, text=text)
            except Exception:
                logger.exception("Не удалось доставить ответ на нажатие кнопки")


class CallbackMessage:
    """message внутри callback-апдейта, с .edit_text/.answer как у aiogram."""

    def __init__(self, bot, message: Dict[str, Any], user_id: int):
        self.bot = bot
        self._user_id = user_id
        body = message.get("body") or {}
        self.message_id: Optional[str] = body.get("mid")
        self.text: str = body.get("text") or ""
        self.html_text: str = self.text  # MAX не отдаёт исходный HTML отдельно

    async def edit_text(
        self, text: str, reply_markup: Optional[List[Dict[str, Any]]] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> None:
        if self.message_id:
            await self.bot.edit_message(
                self.message_id, text=text, attachments=reply_markup,
                fmt=(parse_mode or "").lower() or None,
            )
        else:
            await self.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

    async def answer(
        self, text: str, reply_markup: Optional[List[Dict[str, Any]]] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> SentMessage:
        result = await self.bot.send_message(
            user_id=self._user_id, text=text, attachments=reply_markup,
            fmt=(parse_mode or "").lower() or None,
        )
        return _sent_message_stub(self.bot, self._user_id, result)
