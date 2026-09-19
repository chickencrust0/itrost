"""
settings.py — единственная точка чтения окружения.

Бот IT РОСТ: мессенджер MAX + AlfaCRM.

По сравнению с версией для «Колибри» (MAX + AlfaCRM) блок настроек
CRM стал втрое короче. Причина: у AlfaCRM состав полей не
публикуется, и каждое имя поля приходилось выносить в отдельную
переменную окружения, чтобы правку схемы можно было сделать без правки
кода. У AlfaCRM схема документирована и стабильна (customer, teacher,
lesson, customer-tariff), поэтому имена полей зашиты в alfacrm_client.py
и настраивать их не нужно.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore


def _int_list(raw: str):
    result = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ==================== ДОСТУПЫ: MAX ====================

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")

# webhook | polling. Long Polling не годится для продакшена (см. докс MAX),
# но не требует публичного домена с доверенным TLS-сертификатом —
# поэтому по умолчанию включён polling.
MAX_MODE = os.getenv("MAX_MODE", "polling").strip().lower()
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL", "").strip()
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
MAX_WEBHOOK_HOST = os.getenv("MAX_WEBHOOK_HOST", "0.0.0.0").strip()
MAX_WEBHOOK_PORT = _int("MAX_WEBHOOK_PORT", 8080)
MAX_WEBHOOK_PATH = os.getenv("MAX_WEBHOOK_PATH", "/webhook").strip()

MANAGER_IDS = _int_list(os.getenv("ADMIN_MAX_IDS", ""))

# Не более 2 сообщений/сек в один диалог (документировано MAX).
MAX_SEND_DELAY = _float("MAX_SEND_DELAY", 0.55)
MAX_BROADCAST_DELAY = _float("MAX_BROADCAST_DELAY", 0.55)
MAX_RPS = _float("MAX_RPS", 1.8)
MAX_TIMEOUT = _float("MAX_TIMEOUT", 30.0)
MAX_LONGPOLL_TIMEOUT = _int("MAX_LONGPOLL_TIMEOUT", 30)

# Отключать только для диагностики (антивирус/корпоративный прокси,
# подменяющий TLS) — см. ssl_utils.py. По умолчанию проверка включена.
MAX_SSL_VERIFY = _bool("MAX_SSL_VERIFY", True)

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "bot.db"))

# ==================== ДОСТУПЫ: AlfaCRM ====================

ALFACRM_URL = os.getenv("ALFACRM_URL", "").strip().rstrip("/")
ALFACRM_EMAIL = os.getenv("ALFACRM_EMAIL", "").strip()
ALFACRM_API_KEY = os.getenv("ALFACRM_API_KEY", "").strip()

# Филиал IT РОСТ. Подставляется в КАЖДЫЙ путь: /v2api/{BRANCH_ID}/...
# Если бот вдруг начнёт показывать чужое расписание — проверьте в
# первую очередь это число.
BRANCH_ID = os.getenv("BRANCH_ID", "2").strip() or "2"

# У AlfaCRM документированный лимит — 5 запросов/сек. Держим ниже
# потолка: бот не единственный клиент API.
ALFACRM_RPS = _float("ALFACRM_RPS", 4.0)
ALFACRM_TIMEOUT = _float("ALFACRM_TIMEOUT", 30.0)
ALFACRM_MAX_PAGES = _int("ALFACRM_MAX_PAGES", 200)
ALFACRM_SSL_VERIFY = _bool("ALFACRM_SSL_VERIFY", True)
# Сколько секунд держать в памяти справочники (ученики, педагоги).
ALFACRM_LOOKUP_TTL = _int("ALFACRM_LOOKUP_TTL", 120)

# Статусы урока в AlfaCRM. В отличие от AlfaCRM это настоящее поле
# lesson.status, а не догадка.
STATUS_PLANNED = _int("STATUS_PLANNED", 1)
STATUS_CANCELLED = _int("STATUS_CANCELLED", 2)
STATUS_CONDUCTED = _int("STATUS_CONDUCTED", 3)
ALL_STATUSES = (STATUS_PLANNED, STATUS_CANCELLED, STATUS_CONDUCTED)

# Тема, ДЗ и отметка «проведён» пишутся ОБРАТНО В CRM: у сущности lesson
# в AlfaCRM эти поля есть (topic / homework / status). Это главное
# отличие от бота «Колибри», где их приходилось держать только в SQLite,
# потому что в AlfaCRM таких полей нет вовсе.
#
# Локальная таблица lesson_notes при этом сохраняется и остаётся
# источником для показа: если запись в CRM не прошла (нет прав, урок
# заблокирован), преподаватель не теряет введённый текст, а в лог уходит
# предупреждение.
ALFACRM_WRITE_BACK = _bool("ALFACRM_WRITE_BACK", True)

# Уважительный пропуск в AlfaCRM: id причины из справочника «Причины
# пропуска урока». Списывать занятие с абонемента или нет решает сама
# CRM по этому справочнику, а не бот, — поэтому заморозка передаёт
# именно id, а не флаг.
#
# Пока не задано, заморозка записывается ТОЛЬКО в базу бота: отметить
# пропуск без причины значило бы пометить его неуважительным, и
# заморозка списала бы занятие вместо того, чтобы его сохранить.
ALFACRM_REASON_EXCUSED = os.getenv("ALFACRM_REASON_EXCUSED", "").strip()

# Ставить занятию статус «проведён» при первой отметке посещаемости.
# В AlfaCRM абонемент списывается только у проведённого занятия, поэтому
# по умолчанию включено: иначе отметки стояли бы, а занятия не
# списывались, и остаток у родителя не менялся бы.
ALFACRM_CONDUCT_ON_ATTENDANCE = _bool("ALFACRM_CONDUCT_ON_ATTENDANCE", True)

# Дополнительное поле карточки ученика, где лежит остаток БЕСПРИЧИННЫХ
# заморозок. Заводится администратором в AlfaCRM (Настройки ->
# дополнительные поля), имя всегда начинается с custom_.
#
# Если такого поля нет, счётчик ведётся в базе самого бота — он обязан
# работать сразу после установки, не требуя правок в CRM. Разница только
# в том, видит ли менеджер остаток в карточке ученика или правит его
# через бота. Логика выбора хранилища — в freezes.FreezeCounter.
#
# В боте «Колибри» на impulseCRM своих полей нет вовсе, и счётчик
# приходилось прятать в поле адреса вперемешку с настоящим адресом —
# из-за чего «ул. Ленина, д. 5» могла быть прочитана как пять заморозок.
ALFACRM_FIELD_FREEZES = os.getenv("ALFACRM_FIELD_FREEZES", "custom_free_freezes").strip()

# Сколько беспричинных заморозок даётся ученику, когда счётчик ведётся
# в базе бота (для нового ученика записи ещё нет).
FREEZE_FREE_PER_CLIENT = _int("FREEZE_FREE_PER_CLIENT", 2)

# ==================== ЗАМОРОЗКИ: ОКНО ДО ЗАНЯТИЯ ====================
#
# Заморозить занятие (и уважительно, и беспричинно) можно, только пока
# до начала осталось не меньше этого числа часов. Позже занятие сгорает
# — об этом приходит уведомление (scheduler.notify_burned_lessons).
FREEZE_DEADLINE_HOURS = _float("FREEZE_DEADLINE_HOURS", 5.0)

# Через сколько минут ПОСЛЕ начала занятия считать его сгоревшим и
# уведомить родителя. Запас нужен, чтобы преподаватель успел проставить
# посещаемость: сразу после звонка отметок ещё нет.
BURN_NOTIFY_AFTER_MINUTES = _int("BURN_NOTIFY_AFTER_MINUTES", 90)

# Как часто планировщик ищет сгоревшие занятия (минуты).
BURN_CHECK_INTERVAL_MINUTES = _int("BURN_CHECK_INTERVAL_MINUTES", 15)

# На сколько дней вперёд смотреть в разделе «Заморозить занятие».
FREEZE_LOOKAHEAD_DAYS = _int("FREEZE_LOOKAHEAD_DAYS", 14)

# Кеш держит только окно вокруг сегодняшнего дня.
CACHE_DAYS_BACK = _int("CACHE_DAYS_BACK", 30)
CACHE_DAYS_FORWARD = _int("CACHE_DAYS_FORWARD", 60)
CACHE_REFRESH_MINUTES = _int("CACHE_REFRESH_MINUTES", 5)

MAX_LESSON_CARDS = _int("MAX_LESSON_CARDS", 20)
# Влияет только на строку «занятия заканчиваются» в разделе остатка.
# Ежедневной рассылки по остатку больше нет: два сообщения об одном и
# том же читаются как спам, а остаток родитель смотрит сам по кнопке.
LOW_BALANCE_THRESHOLD = _int("LOW_BALANCE_THRESHOLD", 2)

# Время вечерней сводки по неявкам менеджеру (по поясу филиала).
ABSENCE_DIGEST_HOUR = _int("ABSENCE_DIGEST_HOUR", 21)
ABSENCE_DIGEST_MINUTE = _int("ABSENCE_DIGEST_MINUTE", 0)

# ==================== ВОРОНКА НОВЫХ КЛИЕНТОВ ====================

# ==================== АКТИВНОСТЬ В БОТЕ ====================
#
# Действия посетителей НЕ присылаются менеджеру сообщениями. Раньше
# каждая серия действий уходила отдельной сводкой в чат, и при десятке
# посетителей лента менеджера становилась нечитаемой — в ней терялись
# заявки, обращения и решения по неявкам, то есть ровно то, на что
# менеджеру надо отвечать.
#
# Теперь активность пишется в таблицу activity_log и показывается по
# кнопке «👀 Активность в боте» в меню менеджера.
MANAGER_NOTIFY_ACTIVITY = _bool("MANAGER_NOTIFY_ACTIVITY", True)

# Сколько последних посетителей показывать и сколько действий раскрывать
# по каждому.
ACTIVITY_USERS_LIMIT = _int("ACTIVITY_USERS_LIMIT", 15)
ACTIVITY_EVENTS_PER_USER = _int("ACTIVITY_EVENTS_PER_USER", 12)

# Сколько дней хранить журнал (чистится ночным заданием).
ACTIVITY_KEEP_DAYS = _int("ACTIVITY_KEEP_DAYS", 14)

# ==================== ДОСТУП МЕНЕДЖЕРА ====================
#
# Помимо списка ADMIN_MAX_IDS менеджером можно стать по команде
# /manager с паролем — так новый сотрудник подключается без правки .env
# и перезапуска бота.
#
# ВАЖНО: смените пароль в .env. Значение по умолчанию известно всем, у
# кого есть исходники.
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "itrost-manager").strip()

# Метка сборки. Печатается в лог при запуске, чтобы можно было
# убедиться, какая версия кода реально работает на сервере.
BUILD = "it-rost 2026-09-11.1 (MAX + AlfaCRM, филиал 2: посещаемость, заморозки, поддержка)"

SCHOOL_NAME = os.getenv("SCHOOL_NAME", "Инженерно-техническое направление «IT РОСТ»")
SCHOOL_SITE = os.getenv("SCHOOL_SITE", "https://it-rost.ru/")
SCHOOL_PHONE = os.getenv("SCHOOL_PHONE", "+7 960 892 21-22")

# Возраст СВОБОДНОГО НАБОРА: 7–14 лет. Верхняя граница именно 14, а не
# 15, хотя в ДОП по шахматам стоит 8–15: единственная программа,
# доходящая до 15 лет, — группа для продолжающих, куда зачисляют по
# итогам начального курса или при наличии разряда, а не с улицы. Если
# поставить сюда 15, ребёнку 15 лет бот покажет пустой список программ
# и всё равно скажет «готовой программы нет» — только двумя экранами
# позже и уже после выбора.
#
# Заявка от ребёнка вне диапазона всё равно создаётся, просто менеджеру
# уходит пометка: отказывать на входе бот не должен.
MIN_AGE = _int("MIN_AGE", 7)
MAX_AGE = _int("MAX_AGE", 14)

# ==================== ВРЕМЯ ====================

# Волгоград. С 2020 года это снова MSK, но пояс указан явно: если
# решение отменят, править надо будет одну строку, а не искать причину
# съехавших на час напоминаний.
TIMEZONE = os.getenv("TIMEZONE", "Europe/Volgograd")
TZ_ERROR = None

if ZoneInfo is None:  # pragma: no cover
    TZ = timezone.utc
    TZ_ERROR = "zoneinfo недоступен (нужен Python 3.9+)"
else:
    try:
        TZ = ZoneInfo(TIMEZONE)
    except Exception as e:
        TZ = timezone.utc
        TZ_ERROR = (
            f"часовой пояс '{TIMEZONE}' не найден ({e.__class__.__name__}). "
            f"Установите пакет tzdata: pip install tzdata"
        )


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()
