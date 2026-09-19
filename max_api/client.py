"""
max_api/client.py — тонкий клиент REST API MAX (platform-api2.max.ru).

Официальной библиотеки для Python нет (только JS/TS и Golang, см.
dev.max.ru/docs/chatbots/bots-coding), поэтому это самостоятельная
реализация поверх aiohttp — по аналогии с alfacrm_client.py из
исходного проекта.

Подтверждено официальной документацией (dev.max.ru/docs-api):
  * авторизация — заголовок `Authorization: <token>` (без Bearer)
  * POST /messages, PUT /messages, DELETE /messages, GET /messages
  * POST /answers — ответ на нажатие inline-кнопки
  * POST /uploads — получение URL для загрузки медиафайла
  * POST/GET/DELETE /subscriptions — вебхук
  * GET /updates — long polling
  * GET /me — информация о боте
  * лимит: не более 2 сообщений/сек в один диалог/чат/канал
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

import settings
from ssl_utils import build_ssl_context

logger = logging.getLogger(__name__)

_SSL_CONTEXT = build_ssl_context(settings.MAX_SSL_VERIFY)


class MaxAPIError(Exception):
    pass


class _RateLimiter:
    """Не больше N запросов в секунду — как в alfacrm_client.py."""

    def __init__(self, rps: float):
        self._min_interval = 1.0 / max(rps, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class MaxBot:
    def __init__(self, token: str, base_url: Optional[str] = None):
        self.token = token
        self.base_url = (base_url or settings.MAX_API_URL).rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._limiter = _RateLimiter(settings.MAX_RPS)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
                    self.session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=settings.MAX_TIMEOUT),
                        connector=connector,
                    )
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    # ==================== ИНФРАСТРУКТУРА ====================

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        attempts: int = 3,
        long_poll: bool = False,
    ) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": self.token, "Content-Type": "application/json"}

        # Долгий опрос сам себе таймаут — не гоняем через общий rate-limiter,
        # чтобы не задерживать следующий запрос сна лимитера поверх timeout.
        request_timeout = None
        if long_poll:
            # timeout параметра /updates может доходить до 90с — берём с запасом,
            # иначе клиентский таймаут сессии срубит запрос раньше ответа сервера.
            wait = int((params or {}).get("timeout") or settings.MAX_LONGPOLL_TIMEOUT)
            request_timeout = aiohttp.ClientTimeout(total=wait + 10)

        last_error: Optional[str] = None
        for attempt in range(attempts):
            if not long_poll:
                await self._limiter.acquire()
            try:
                async with session.request(
                    method, url, headers=headers, params=params, json=json_body,
                    timeout=request_timeout,
                ) as response:
                    if response.status == 401:
                        raise MaxAPIError(
                            f"401 Unauthorized на {path}: токен MAX_BOT_TOKEN "
                            f"указан некорректно или недействителен"
                        )
                    if response.status == 429 or response.status >= 500:
                        last_error = f"HTTP {response.status}"
                        backoff = 0.5 * (2 ** attempt)
                        logger.warning(f"⚠️ {last_error} на {path}, повтор через {backoff:.1f}с")
                        await asyncio.sleep(backoff)
                        continue
                    if response.status >= 400:
                        body = await response.text()
                        raise MaxAPIError(f"HTTP {response.status} на {path}: {body[:300]}")
                    return await response.json(content_type=None) or {}
            except asyncio.TimeoutError:
                if long_poll:
                    # Пустой таймаут long-polling — это нормально, не ошибка.
                    return {}
                last_error = "таймаут"
                await asyncio.sleep(0.5 * (2 ** attempt))
            except aiohttp.ClientError as e:
                last_error = f"сеть: {e}"
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise MaxAPIError(f"Запрос {method} {path} не удался ({last_error})")

    # ==================== БОТ ====================

    async def get_me(self) -> Dict[str, Any]:
        return await self._request("GET", "/me")

    async def set_commands(self, commands: List[Dict[str, str]]) -> Dict[str, Any]:
        return await self._request("PATCH", "/me/commands", json_body={"commands": commands})

    # ==================== СООБЩЕНИЯ ====================

    async def send_message(
        self,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        text: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        fmt: Optional[str] = "html",
        notify: bool = True,
        link: Optional[Dict[str, Any]] = None,
        disable_link_preview: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if not user_id and not chat_id:
            raise MaxAPIError("send_message: нужен user_id или chat_id")
        params: Dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        if chat_id:
            params["chat_id"] = chat_id
        if disable_link_preview is not None:
            params["disable_link_preview"] = str(disable_link_preview).lower()

        body: Dict[str, Any] = {"notify": notify}
        if text is not None:
            body["text"] = text[:4000]
        if attachments:
            body["attachments"] = attachments
        if fmt:
            body["format"] = fmt
        if link:
            body["link"] = link

        return await self._request("POST", "/messages", params=params, json_body=body)

    async def edit_message(
        self,
        message_id: str,
        *,
        text: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        fmt: Optional[str] = "html",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if text is not None:
            body["text"] = text[:4000]
        # Пустой список = снять все вложения; None = не трогать вложения.
        if attachments is not None:
            body["attachments"] = attachments
        if fmt:
            body["format"] = fmt
        return await self._request(
            "PUT", "/messages", params={"message_id": message_id}, json_body=body
        )

    async def delete_message(self, message_id: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/messages", params={"message_id": message_id})

    async def answer_callback(
        self,
        callback_id: str,
        *,
        notification: Optional[str] = None,
        text: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        fmt: Optional[str] = "html",
    ) -> Optional[Dict[str, Any]]:
        """
        Ответ на нажатие inline-кнопки.

        Тело запроса ОБЯЗАНО содержать `notification` или `message` —
        подтверждено ответом сервера:
            HTTP 400 {"code":"proto.payload",
                      "message":"Invalid request. `message` or `notification` required"}

        Здесь была ошибка: раньше нажатие «квитировалось» пустым
        POST /answers в расчёте на то, что MAX иначе сочтёт кнопку
        необработанной. MAX такой запрос отвергает, исключение поднималось
        из первой же строки обработчика, и разделы «Расписание», «ДЗ» и
        «Баланс» падали ещё до обращения к CRM.

        Поля:
          notification — всплывающее уведомление (аналог
                         callback.answer(text) в Telegram);
          message      — заменить текст исходного сообщения.

        Если не передано ничего, запрос НЕ отправляется вовсе: квитировать
        нечем, а пустой вызов гарантированно вернёт 400.
        """
        body: Dict[str, Any] = {}

        if notification is not None:
            # Всплывающее уведомление — короткое, длинный текст сюда не
            # влезет, и его следует слать обычным сообщением.
            body["notification"] = notification[:200]

        if text is not None or attachments is not None:
            msg: Dict[str, Any] = {}
            if text is not None:
                msg["text"] = text[:4000]
            if attachments is not None:
                msg["attachments"] = attachments
            if fmt:
                msg["format"] = fmt
            body["message"] = msg

        if not body:
            return None

        return await self._request(
            "POST", "/answers", params={"callback_id": callback_id}, json_body=body
        )

    # ==================== ЗАГРУЗКА ФАЙЛОВ ====================

    async def get_upload_url(self, upload_type: str) -> Dict[str, Any]:
        """upload_type: image | video | audio | file."""
        return await self._request("POST", "/uploads", params={"type": upload_type})

    # ==================== ПОДПИСКИ / LONG POLLING ====================

    async def subscribe(
        self, url: str, update_types: Optional[List[str]] = None, secret: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"url": url}
        if update_types:
            body["update_types"] = update_types
        if secret:
            body["secret"] = secret
        return await self._request("POST", "/subscriptions", json_body=body)

    async def list_subscriptions(self) -> Dict[str, Any]:
        return await self._request("GET", "/subscriptions")

    async def unsubscribe(self, url: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/subscriptions", params={"url": url})

    async def get_updates(
        self,
        marker: Optional[int] = None,
        limit: int = 100,
        timeout: int = 30,
        types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        # timeout окна long-polling должен быть меньше клиентского таймаута сессии.
        return await self._request("GET", "/updates", params=params, long_poll=True)
