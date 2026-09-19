"""
alfacrm_client.py — клиент AlfaCRM v2 API для бота IT РОСТ на MAX.

Собран из двух источников:
  * alfacrm_client.py исходного бота на Telegram — авторизация, лимитер,
    постраничная выборка, работа с уроками;
    * интерфейс alfacrm_client.py из бота «Колибри» на MAX — чтобы весь
    остальной код (кеш, планировщик, хендлеры) не пришлось переписывать
    под другую CRM.

ПОЧЕМУ ЭТО ПРОЩЕ, ЧЕМ БЫЛО С AlfaCRM
---------------------------------------
В AlfaCRM сущность schedule — это ПРАВИЛО повторения («по вторникам
16:00 с 25.08 по 29.12»), и бот вынужден был разворачивать её в
конкретные даты, выдумывая синтетические id вида "116:2026-09-01".
В AlfaCRM `lesson` — это уже одно конкретное занятие с настоящим id,
датой, временем, списками teacher_ids/customer_ids и полями topic /
homework / status. Поэтому:

  * никакого разворачивания повторов и синтетических id;
  * состав учеников берётся прямо из lesson.customer_ids, а не
    вычисляется через абонементы и записи;
  * тему, ДЗ и статус МОЖНО писать обратно в CRM (см. update_lesson).

Нормализованная форма урока (её ждут cache.py и bot/formatting.py):
    id, date, time_from, time_to, teacher_ids, customer_ids, status,
    topic, homework, note, group_id, lesson_type_name, _raw
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

import settings
from ssl_utils import build_ssl_context

logger = logging.getLogger(__name__)

# MAX требует сертификаты Минцифры, а AlfaCRM — обычные. Контекст общий:
# certifi + Минцифры покрывает оба случая и ничего не ломает.
_SSL_CONTEXT = build_ssl_context(settings.ALFACRM_SSL_VERIFY)

STATUS_LABELS = {
    settings.STATUS_PLANNED: "📌 запланирован",
    settings.STATUS_CANCELLED: "❌ отменён",
    settings.STATUS_CONDUCTED: "✅ проведён",
}

# Поля, которые AlfaCRM ожидает получить обратно при lesson/update.
# Частичный апдейт (только {"status": 3}) в лучшем случае возвращает 400,
# в худшем — затирает время и состав участников урока.
LESSON_UPDATE_FIELDS = (
    "branch_id",
    "date",
    "time_from",
    "time_to",
    "lesson_type_id",
    "subject_id",
    "room_id",
    "status",
    "teacher_ids",
    "customer_ids",
    "group_ids",
    "topic",
    "homework",
    "note",
    "streaming_link",
    "is_public",
)


class AlfaCRMError(Exception):
    """Ошибка обращения к CRM, пригодная для показа человеку."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class _RateLimiter:
    """Не больше N запросов в секунду (у AlfaCRM лимит 5 rps)."""

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


class _TTLCache:
    """Справочники (клиенты, педагоги, абонементы) меняются редко."""

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._data: Dict[str, Any] = {}
        self._stamp: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get(self, key: str, loader):
        now = time.monotonic()
        if key in self._data and (now - self._stamp.get(key, 0)) < self._ttl:
            return self._data[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Пока ждали блокировку, значение мог загрузить другой вызов.
            now = time.monotonic()
            if key in self._data and (now - self._stamp.get(key, 0)) < self._ttl:
                return self._data[key]
            value = await loader()
            self._data[key] = value
            self._stamp[key] = time.monotonic()
            return value

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._data.clear()
            self._stamp.clear()
        else:
            self._data.pop(key, None)
            self._stamp.pop(key, None)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v is not None]
    return [value]


def _same_id(a: Any, b: Any) -> bool:
    """id из SQLite — int, из CRM бывает и строкой. Сравниваем как строки."""
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _date_only(value: Any) -> str:
    """'2026-09-01 16:00:00' -> '2026-09-01'."""
    text = str(value or "").strip().replace("T", " ")
    return text.split(" ")[0] if text else ""


class AlfaCRMClient:
    """
    Интерфейс намеренно повторяет AlfaCRMClient из бота «Колибри»:
    load_all_teachers / load_all_clients / find_*_by_phone /
    get_customer_info / get_subscriptions / get_balances_map /
    get_lessons / get_lesson / update_lesson / extract_user_name.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_key: str,
        branch_id: Optional[str] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.email = email
        self.api_key = api_key
        self.branch_id = str(branch_id or settings.BRANCH_ID)
        self.token: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._auth_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._limiter = _RateLimiter(settings.ALFACRM_RPS)
        # Предупреждение об отсутствующем поле заморозок печатается один
        # раз, а не на каждое открытие карточки родителем.
        self._freezes_warned = False
        self._lookups = _TTLCache(settings.ALFACRM_LOOKUP_TTL)

    # ==================== ИНФРАСТРУКТУРА ====================

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=settings.ALFACRM_TIMEOUT),
                        connector=aiohttp.TCPConnector(ssl=_SSL_CONTEXT),
                    )
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _ensure_token(self) -> None:
        if self.token:
            return
        # Без блокировки десяток параллельных запросов делал десяток
        # логинов одновременно и упирался в лимит 5 rps.
        async with self._auth_lock:
            if not self.token:
                await self._auth()

    async def _auth(self) -> None:
        if not self.base_url or not self.email or not self.api_key:
            raise AlfaCRMError(
                "Не заданы ALFACRM_URL / ALFACRM_EMAIL / ALFACRM_API_KEY — "
                "заполните .env и перезапустите бота"
            )
        session = await self._get_session()
        await self._limiter.acquire()
        try:
            async with session.post(
                f"{self.base_url}/v2api/auth/login",
                json={"email": self.email, "api_key": self.api_key},
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise AlfaCRMError(
                        f"Ошибка авторизации в AlfaCRM ({response.status}): {data}",
                        status=response.status,
                    )
                self.token = (data or {}).get("token")
                if not self.token:
                    raise AlfaCRMError("AlfaCRM не вернула токен авторизации")
                logger.info("✅ Токен AlfaCRM получен")
        except aiohttp.ClientError as e:
            raise AlfaCRMError(f"Сеть недоступна при авторизации в AlfaCRM: {e}")

    async def _request(
        self, method: str, endpoint: str, *, attempts: int = 3, **kwargs
    ) -> Dict[str, Any]:
        await self._ensure_token()
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        last_error: Optional[str] = None

        for attempt in range(attempts):
            headers = {"X-ALFACRM-TOKEN": self.token, "Content-Type": "application/json"}
            headers.update(kwargs.get("headers") or {})
            request_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}

            await self._limiter.acquire()
            try:
                async with session.request(
                    method, url, headers=headers, **request_kwargs
                ) as response:
                    if response.status == 401:
                        logger.warning("🔄 Токен AlfaCRM истёк, обновляем…")
                        self.token = None
                        await self._ensure_token()
                        last_error = "401 Unauthorized"
                        continue

                    if response.status == 429 or response.status >= 500:
                        last_error = f"HTTP {response.status}"
                        backoff = 0.5 * (2 ** attempt)
                        logger.warning(
                            f"⚠️ {last_error} на {endpoint}, повтор через {backoff:.1f}с"
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if response.status >= 400:
                        body = await response.text()
                        raise AlfaCRMError(
                            f"AlfaCRM ответила {response.status} на {endpoint}: {body[:300]}",
                            status=response.status,
                        )

                    return await response.json(content_type=None) or {}

            except asyncio.TimeoutError:
                last_error = "таймаут"
                await asyncio.sleep(0.5 * (2 ** attempt))
            except aiohttp.ClientError as e:
                last_error = f"сеть: {e}"
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise AlfaCRMError(f"Запрос {method} {endpoint} не удался ({last_error})")

    def _endpoint(self, entity: str, action: str = "index") -> str:
        return f"/v2api/{self.branch_id}/{entity}/{action}"

    async def _load_all_pages(self, endpoint: str, payload: dict) -> List[Dict]:
        """Забирает ВСЕ страницы, а не только нулевую."""
        all_items: List[Dict] = []
        page = 0
        payload = dict(payload)
        while page < settings.ALFACRM_MAX_PAGES:
            payload["page"] = page
            result = await self._request("POST", endpoint, json=payload)
            items = result.get("items") or []
            if not items:
                break
            all_items.extend(items)
            total = int(result.get("total") or 0)
            if total and len(all_items) >= total:
                break
            page += 1
        else:
            logger.warning(f"⚠️ Достигнут лимит страниц на {endpoint}")
        return all_items

    # ==================== ТЕЛЕФОНЫ ====================

    @staticmethod
    def _normalize_phone(phone: Any) -> str:
        return "".join(filter(str.isdigit, str(phone or "")))

    def _phone_matches(self, phone1: Any, phone2: Any) -> bool:
        clean1 = self._normalize_phone(phone1)
        clean2 = self._normalize_phone(phone2)
        if len(clean1) < 10 or len(clean2) < 10:
            return False
        # Сравниваем по последним 10 цифрам: +7/8/без кода — одно и то же.
        return clean1[-10:] == clean2[-10:]

    @staticmethod
    def _phones_of(record: Dict[str, Any]) -> List[str]:
        raw = record.get("phone")
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw]
        if raw:
            return [str(raw)]
        return []

    # ==================== СПРАВОЧНИКИ ====================

    async def load_all_teachers(self) -> List[Dict[str, Any]]:
        async def loader():
            return await self._load_all_pages(self._endpoint("teacher"), {})

        return await self._lookups.get("teachers", loader)

    async def load_all_customers(self, is_study: Optional[int] = None) -> List[Dict]:
        """
        is_study=1 — только действующие ученики, None — все, включая лидов
        и архив. Кешируется отдельно по значению фильтра.
        """
        key = f"customers:{is_study}"

        async def loader():
            payload: Dict[str, Any] = {}
            if is_study is not None:
                payload["is_study"] = is_study
            return await self._load_all_pages(self._endpoint("customer"), payload)

        return await self._lookups.get(key, loader)

    async def load_all_clients(self) -> List[Dict[str, Any]]:
        """
        Имя из интерфейса alfacrm_client — его зовут cache/scheduler/
        handlers. Здесь берём ТОЛЬКО действующих учеников (is_study=1):
        иначе в утренние рассылки, сводки и проверку баланса попали бы
        лиды и отчисленные.
        """
        return await self.load_all_customers(is_study=1)

    def invalidate_lookups(self) -> None:
        self._lookups.invalidate()

    async def find_teacher_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        for teacher in await self.load_all_teachers():
            if any(self._phone_matches(phone, p) for p in self._phones_of(teacher)):
                return teacher
        return None

    async def find_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        # Сначала среди действующих учеников, потом среди всех: у
        # отчисленного родителя вход в бота тоже должен работать.
        for is_study in (1, None):
            for customer in await self.load_all_customers(is_study=is_study):
                if any(self._phone_matches(phone, p) for p in self._phones_of(customer)):
                    return customer
        return None

    async def get_teacher_info(self, teacher_id: Any) -> Optional[Dict[str, Any]]:
        for teacher in await self.load_all_teachers():
            if _same_id(teacher.get("id"), teacher_id):
                return teacher
        return None

    # ==================== АБОНЕМЕНТЫ И БАЛАНС ====================
    #
    # В AlfaCRM остаток занятий лежит прямо на карточке ученика:
    #   paid_lesson_count — сколько занятий оплачено,
    #   paid_count        — сколько из них уже проведено,
    #   balance           — деньги на счету.
    # Этого достаточно и для родителя, и для проверки низкого баланса,
    # поэтому отдельный запрос абонементов (customer-tariff) —
    # НЕОБЯЗАТЕЛЬНАЯ детализация: он даёт разбивку по абонементам, но
    # если сущность недоступна (нет прав, другой тариф CRM), бот молча
    # показывает одну сводную строку вместо списка.

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _lessons_left(self, customer: Dict[str, Any]) -> int:
        paid = self._int(customer.get("paid_lesson_count"))
        used = self._int(customer.get("paid_count"))
        return max(0, paid - used)

    @staticmethod
    def _first(record: Dict[str, Any], *names: str) -> Any:
        """
        Первое непустое из нескольких вариантов имени поля.

        Состав полей customer-tariff в открытой документации AlfaCRM не
        расписан (в справочнике показан только пример запроса), поэтому
        имена перебираются, а не угадываются одним вариантом.
        """
        for name in names:
            value = record.get(name)
            if value not in (None, ""):
                return value
        return None

    async def _load_customer_tariffs(self, customer_id: Any) -> List[Dict[str, Any]]:
        try:
            return await self._load_all_pages(
                self._endpoint("customer-tariff"), {"customer_id": customer_id}
            )
        except AlfaCRMError as e:
            logger.debug(f"customer-tariff недоступна ({e}) — покажем сводный остаток")
            return []

    async def get_subscriptions(self, client_id: Any) -> List[Dict[str, Any]]:
        """
        Абонементы клиента в нормализованной форме:
        {id, name, left, total, used, frozen, end_date, days_left}.

        Если детализация недоступна — одна сводная строка по данным
        карточки ученика, чтобы раздел «Абонемент» у родителя не был пуст.
        """
        result: List[Dict[str, Any]] = []

        for t in await self._load_customer_tariffs(client_id):
            if t.get("is_deleted"):
                continue
            total = self._int(self._first(t, "lesson_count", "lessons_count", "count"))
            left = self._int(self._first(t, "lesson_left", "lessons_left", "balance"))
            used = max(0, total - left) if total else self._int(t.get("lesson_used"))
            result.append({
                "id": t.get("id"),
                "entity": "customer-tariff",
                "name": self._first(t, "tariff_name", "name", "note") or "Абонемент",
                "left": left,
                "total": total,
                "used": used,
                "frozen": bool(self._first(t, "is_frozen", "frozen")),
                "end_date": _date_only(self._first(t, "e_date", "end_date", "date_to")),
                "days_left": self._first(t, "days_left"),
            })

        if result:
            result.sort(key=lambda x: (x["end_date"] or "9999-99-99", x["name"]))
            return result

        customer = await self._find_customer(client_id)
        if not customer:
            return []
        total = self._int(customer.get("paid_lesson_count"))
        used = self._int(customer.get("paid_count"))
        if not total and not used:
            return []
        return [{
            "id": customer.get("id"),
            "entity": "customer",
            "name": "Оплаченные занятия",
            "left": max(0, total - used),
            "total": total,
            "used": used,
            "frozen": False,
            "end_date": _date_only(customer.get("paid_till")),
            "days_left": None,
        }]

    async def get_balances_map(self) -> Dict[str, Dict[str, Any]]:
        """
        {customer_id (строкой): {"left": int, "paid": int, "used": int}}.

        Ключи ПРИВЕДЕНЫ К СТРОКЕ: crm_id в SQLite — int, id из CRM бывает
        и int, и строкой; при расхождении типов lookup молча промахивался
        и баланс показывался нулевым.
        """
        result: Dict[str, Dict[str, Any]] = {}
        for c in await self.load_all_clients():
            cid = c.get("id")
            if cid is None:
                continue
            paid = self._int(c.get("paid_lesson_count"))
            used = self._int(c.get("paid_count"))
            if not paid and not used:
                # Оплат не было вовсе — это не «баланс заканчивается», а
                # «ученик ещё не занимается». Такие в проверку не идут,
                # иначе уведомление уйдёт на всю базу разом.
                continue
            result[str(cid)] = {"left": max(0, paid - used), "paid": paid, "used": used}
        return result

    async def _find_customer(self, customer_id: Any) -> Optional[Dict[str, Any]]:
        try:
            result = await self._request(
                "POST", self._endpoint("customer"), json={"id": customer_id, "page": 0}
            )
            for customer in result.get("items") or []:
                if _same_id(customer.get("id"), customer_id):
                    return customer
        except AlfaCRMError as e:
            logger.debug(f"Поиск клиента по id не сработал: {e}")

        for is_study in (1, None):
            for customer in await self.load_all_customers(is_study=is_study):
                if _same_id(customer.get("id"), customer_id):
                    return customer
        return None

    async def get_customer_info(self, customer_id: Any) -> Optional[Dict[str, Any]]:
        """
        Клиент + остаток занятий. Поля lessons_left / paid_lesson_count /
        paid_count / subscriptions читает bot/handlers/parent.py.
        """
        customer = await self._find_customer(customer_id)
        if not customer:
            return None

        result = dict(customer)
        result["balance"] = customer.get("balance") or 0
        result["lessons_left"] = self._lessons_left(customer)
        result["paid_lesson_count"] = self._int(customer.get("paid_lesson_count"))
        result["paid_count"] = self._int(customer.get("paid_count"))
        result["subscriptions"] = []

        try:
            result["subscriptions"] = await self.get_subscriptions(customer_id)
        except AlfaCRMError as e:
            logger.warning(f"⚠️ Не удалось получить абонементы клиента {customer_id}: {e}")

        return result

    # ==================== УРОКИ ====================

    @staticmethod
    def _normalize_lesson(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Приводит урок AlfaCRM к форме, которую ждут кеш и форматирование.

        Правка минимальная — в отличие от AlfaCRM, здесь почти всё
        уже лежит под нужными именами. Приводятся только типы: списки
        teacher_ids/customer_ids у AlfaCRM бывают и списком, и скаляром.
        """
        date = _date_only(raw.get("date")) or _date_only(raw.get("time_from"))
        return {
            "id": raw.get("id"),
            "date": date,
            "time_from": raw.get("time_from") or raw.get("date") or "",
            "time_to": raw.get("time_to") or "",
            "teacher_ids": _as_list(raw.get("teacher_ids")),
            "customer_ids": _as_list(raw.get("customer_ids")),
            "group_id": (_as_list(raw.get("group_ids")) or [None])[0],
            "status": raw.get("status"),
            "topic": (raw.get("topic") or "").strip(),
            "homework": (raw.get("homework") or "").strip(),
            "note": (raw.get("note") or "").strip(),
            "lesson_type_name": raw.get("subject_name") or raw.get("lesson_type_name") or "",
            "_raw": raw,
        }

    async def get_lessons(
        self,
        teacher_id: Optional[Any] = None,
        customer_id: Optional[Any] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Уроки за период. Если status не задан — перебираем все статусы:
        AlfaCRM по умолчанию отдаёт только проведённые, и без перебора
        расписание на завтра было бы пустым.
        """
        statuses = [status] if status is not None else list(settings.ALL_STATUSES)

        base_payload: Dict[str, Any] = {}
        if teacher_id:
            base_payload["teacher_id"] = teacher_id
        if customer_id:
            base_payload["customer_id"] = customer_id
        if date_from:
            base_payload["date_from"] = date_from
        if date_to:
            base_payload["date_to"] = date_to

        by_id: Dict[Any, Dict[str, Any]] = {}

        for st in statuses:
            payload = {**base_payload, "status": st}
            page = 0
            fetched = 0
            while page < settings.ALFACRM_MAX_PAGES:
                payload["page"] = page
                result = await self._request(
                    "POST", self._endpoint("lesson"), json=payload
                )
                items = result.get("items") or []
                if not items:
                    break
                for lesson in items:
                    key = lesson.get("id") or id(lesson)
                    by_id[key] = lesson
                fetched += len(items)
                total = int(result.get("total") or 0)
                if total and fetched >= total:
                    break
                page += 1

        lessons = [self._normalize_lesson(l) for l in by_id.values()]
        logger.info(f"📊 Получено уроков из AlfaCRM: {len(lessons)} ({date_from} – {date_to})")
        return lessons

    async def get_lesson(self, lesson_id: Any) -> Optional[Dict[str, Any]]:
        """Один урок. Перебор всей истории как fallback не делается — он стоил сотни запросов."""
        for st in [None] + list(settings.ALL_STATUSES):
            payload: Dict[str, Any] = {"id": lesson_id, "page": 0}
            if st is not None:
                payload["status"] = st
            try:
                result = await self._request(
                    "POST", self._endpoint("lesson"), json=payload
                )
            except AlfaCRMError as e:
                logger.debug(f"lesson/index id={lesson_id} status={st}: {e}")
                continue
            for lesson in result.get("items") or []:
                if _same_id(lesson.get("id"), lesson_id):
                    return self._normalize_lesson(lesson)
        return None

    async def update_lesson(
        self,
        lesson_id: Any,
        updates: Dict[str, Any],
        current: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Обновляет урок, передавая ПОЛНУЮ модель.

        `current` можно передать из кеша, чтобы не ходить в CRM лишний
        раз — это может быть как нормализованный урок (тогда берётся
        `_raw`), так и сырой ответ CRM.
        """
        raw = None
        if current is not None:
            raw = current.get("_raw") or current
        if not raw:
            fetched = await self.get_lesson(lesson_id)
            raw = (fetched or {}).get("_raw") if fetched else None
        if not raw:
            raise AlfaCRMError(f"Урок {lesson_id} не найден в CRM")

        payload: Dict[str, Any] = {}
        for field in LESSON_UPDATE_FIELDS:
            value = raw.get(field)
            if value is not None:
                payload[field] = value
        payload.update(updates)
        payload.setdefault("branch_id", int(self.branch_id))

        await self._request(
            "POST",
            self._endpoint("lesson", "update"),
            params={"id": lesson_id},
            json=payload,
        )
        logger.info(f"✅ Урок {lesson_id} обновлён в CRM: {list(updates.keys())}")
        return payload

    async def mark_lesson_conducted(
        self, lesson_id: Any, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(
            lesson_id, {"status": settings.STATUS_CONDUCTED}, current=current
        )

    async def set_homework(
        self, lesson_id: Any, homework_text: str, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(
            lesson_id, {"homework": homework_text}, current=current
        )

    async def set_topic(
        self, lesson_id: Any, topic: str, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(lesson_id, {"topic": topic}, current=current)

    # ==================== ПОСЕЩАЕМОСТЬ ====================
    #
    # В AlfaCRM посещаемость — ДОКУМЕНТИРОВАННОЕ поле `details` сущности
    # lesson: массив записей по одной на ученика, с `is_attend`
    # (1 присутствовал / 0 отсутствовал) и `reason_id` (причина пропуска).
    #
    # Это принципиально проще, чем было в боте «Колибри». Там отметка
    # посещения делалась через внутренний API веб-интерфейса
    # (check_visits/check), найденный разбором фронтенда: он не
    # документирован, не принимает API-ключ и требует подставлять cookie
    # сессии браузера, которую надо периодически обновлять вручную. Плюс
    # приходилось передавать абонемент и «цель» целыми объектами и
    # угадывать minutesBegin. Здесь всё это не нужно: обычный
    # lesson/update по документированной схеме, с обычным токеном.
    #
    # Требование схемы: служебные поля записи (`id`, `branch_id`,
    # `customer_id`, `lesson_id`, `ctt_id`) должны СОВПАДАТЬ с тем, что
    # CRM отдала в списке уроков. Поэтому существующие details
    # переносятся как есть, и меняются только is_attend/reason_id.

    ATTEND_FIELDS = (
        "id", "branch_id", "customer_id", "lesson_id", "ctt_id",
        "is_attend", "reason_id", "commission", "grade",
        "homework_grade_id", "bonus", "note",
    )

    @staticmethod
    def _details_of(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        value = raw.get("details")
        return [d for d in value if isinstance(d, dict)] if isinstance(value, list) else []

    async def get_attendance(self, lesson_id: Any) -> Dict[str, int]:
        """
        {customer_id (строкой): 1|0} по данным CRM.

        В словарь попадают только ученики, у которых is_attend реально
        проставлен. «Ключа нет» и «is_attend = 0» — разные вещи: первое
        означает «преподаватель ещё не отмечал», второе — «отмечен как
        отсутствующий», и путать их нельзя, иначе карточка занятия
        показывала бы всю группу как пропустившую ещё до начала урока.
        """
        lesson = await self.get_lesson(lesson_id)
        if not lesson:
            return {}
        result: Dict[str, int] = {}
        for d in self._details_of(lesson.get("_raw") or {}):
            cid = d.get("customer_id")
            if cid is None or d.get("is_attend") is None:
                continue
            result[str(cid)] = 1 if self._int(d.get("is_attend")) else 0
        return result

    async def set_attendance(
        self,
        lesson_id: Any,
        customer_id: Any,
        is_attend: Optional[int],
        reason_id: Optional[Any] = None,
        current: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Проставляет посещаемость ОДНОМУ ученику.

        is_attend: 1 — был, 0 — не был, None — снять отметку (вернуть
        ученика в состояние «не отмечен»).

        Возвращает True, если что-то изменилось. False означает, что
        отметка уже была такой — вызывающий по этому отличает «снял» от
        «снимать было нечего» и не пишет преподавателю, что снял, когда
        ничего не менялось.
        """
        raw = None
        if current is not None:
            raw = current.get("_raw") or current
        if not raw:
            fetched = await self.get_lesson(lesson_id)
            raw = (fetched or {}).get("_raw") if fetched else None
        if not raw:
            raise AlfaCRMError(f"Урок {lesson_id} не найден в CRM")

        details = [
            {k: d[k] for k in self.ATTEND_FIELDS if k in d}
            for d in self._details_of(raw)
        ]

        target = None
        for d in details:
            if _same_id(d.get("customer_id"), customer_id):
                target = d
                break

        if target is None:
            if not any(_same_id(c, customer_id) for c in _as_list(raw.get("customer_ids"))):
                raise AlfaCRMError(
                    f"Ученик {customer_id} не записан на занятие {lesson_id}"
                )
            # Запись детали ещё не создана — CRM создаёт их не всегда.
            # Собираем минимальную: ctt_id (абонемент) не указываем, его
            # CRM подберёт сама, как делает и веб-интерфейс.
            target = {
                "customer_id": customer_id,
                "lesson_id": raw.get("id") or lesson_id,
                "branch_id": raw.get("branch_id") or int(self.branch_id),
            }
            details.append(target)
            logger.debug(
                f"Деталь урока {lesson_id} для ученика {customer_id} создаётся заново"
            )

        was_attend = target.get("is_attend")
        was_reason = target.get("reason_id")
        was = (None if was_attend is None else self._int(was_attend), was_reason)

        if is_attend is None:
            target.pop("is_attend", None)
            target.pop("reason_id", None)
            now = (None, None)
        else:
            target["is_attend"] = int(bool(is_attend))
            # Причина осмысленна только у пропуска; на присутствии она
            # осталась бы висеть от прошлой отметки.
            if is_attend:
                target.pop("reason_id", None)
                now = (1, None)
            else:
                if reason_id is not None:
                    target["reason_id"] = reason_id
                now = (0, target.get("reason_id"))

        if was == now:
            return False

        updates: Dict[str, Any] = {"details": details}
        # Абонемент списывается только у проведённого занятия — пока
        # статус «запланирован», отметки в CRM ни на что не влияют.
        if is_attend is not None and settings.ALFACRM_CONDUCT_ON_ATTENDANCE:
            updates["status"] = settings.STATUS_CONDUCTED

        await self.update_lesson(lesson_id, updates, current={"_raw": raw})
        return True

    async def mark_present(
        self, lesson_id: Any, customer_id: Any, current: Optional[Dict] = None
    ) -> bool:
        return await self.set_attendance(lesson_id, customer_id, 1, current=current)

    async def mark_absent(
        self,
        lesson_id: Any,
        customer_id: Any,
        reason_id: Optional[Any] = None,
        current: Optional[Dict] = None,
    ) -> bool:
        """
        Отметить пропуск. reason_id решает, спишется ли занятие с
        абонемента: это настраивается в самой CRM (справочник причин
        пропуска), а не в боте — поэтому уважительная заморозка передаёт
        ALFACRM_REASON_EXCUSED, а неявка не передаёт ничего.
        """
        return await self.set_attendance(
            lesson_id, customer_id, 0, reason_id=reason_id, current=current
        )

    async def clear_attendance(
        self, lesson_id: Any, customer_id: Any, current: Optional[Dict] = None
    ) -> bool:
        return await self.set_attendance(lesson_id, customer_id, None, current=current)

    # ==================== БЕСПРИЧИННЫЕ ЗАМОРОЗКИ ====================
    #
    # Счётчик оставшихся беспричинных заморозок хранится в
    # ДОПОЛНИТЕЛЬНОМ ПОЛЕ карточки ученика (`custom_*`) — они у AlfaCRM
    # документированы и заводятся администратором в интерфейсе.
    #
    # В боте «Колибри» этот счётчик пришлось прятать в поле адреса
    # клиента: в AlfaCRM своих полей нет, и число лежало вперемешку с
    # настоящим адресом, из-за чего «ул. Ленина, д. 5» могла превратиться
    # в пять заморозок. Здесь такого риска нет — поле отдельное и
    # числовое.
    #
    # Если поле в CRM не заведено, счётчик ведётся в базе самого бота
    # (см. freezes.FreezeCounter): бот обязан работать сразу после
    # установки, не требуя правок в CRM.

    async def get_crm_freezes(self, customer_id: Any) -> Optional[int]:
        """
        Остаток заморозок из CRM или None, если поле не настроено либо
        отсутствует в карточке. None — это «читать неоткуда», а не ноль:
        показать родителю выдуманный ноль хуже, чем взять число из базы.
        """
        field = settings.ALFACRM_FIELD_FREEZES
        if not field:
            return None
        customer = await self._find_customer(customer_id)
        if not customer:
            raise AlfaCRMError(f"Карточка ученика {customer_id} не найдена в CRM")
        if field not in customer:
            if not self._freezes_warned:
                logger.warning(
                    f"⚠️ В карточке ученика нет поля {field!r} — счётчик заморозок "
                    f"ведётся в базе бота. Заведите это дополнительное поле в "
                    f"AlfaCRM или уберите ALFACRM_FIELD_FREEZES из .env, чтобы "
                    f"убрать это предупреждение. Поля карточки: "
                    f"{', '.join(sorted(customer.keys()))[:300]}"
                )
                self._freezes_warned = True
            return None
        return max(0, self._int(customer.get(field)))

    async def set_crm_freezes(self, customer_id: Any, value: int) -> bool:
        """Записывает остаток обратно в CRM. False — поле не настроено."""
        field = settings.ALFACRM_FIELD_FREEZES
        if not field:
            return False
        customer = await self._find_customer(customer_id)
        if not customer or field not in customer:
            return False
        await self._request(
            "POST",
            self._endpoint("customer", "update"),
            params={"id": customer_id},
            json={**customer, field: max(0, int(value))},
        )
        self._lookups.invalidate()
        return True

    # ==================== УТИЛИТЫ ====================

    @staticmethod
    def extract_user_name(user: Dict[str, Any]) -> str:
        return (
            (user.get("name") or "").strip()
            or (user.get("legal_name") or "").strip()
            or "Без имени"
        )

    def extract_user_phone(self, user: Dict[str, Any]) -> str:
        phones = self._phones_of(user)
        return phones[0] if phones else "Нет телефона"

    @staticmethod
    def get_lesson_status_label(status: int) -> str:
        return STATUS_LABELS.get(status, f"статус {status}")
