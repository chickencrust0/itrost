"""
cache.py — кеш уроков с периодическим обновлением.

Портировано из alfacrm-bot без изменений логики: кеш держит только окно
вокруг сегодняшнего дня (по умолчанию −30/+60 дней) вместо всей истории
целиком — иначе объём растёт без ограничений и упирается в лимит
запросов/сек AlfaCRM (см. settings.ALFACRM_RPS).

Ожидает от alfacrm_client.get_lessons(...) уроки в НОРМАЛИЗОВАННОЙ форме
(id, date, time_from, time_to, teacher_ids, customer_ids, status, topic,
homework) — см. _normalize_lesson() в alfacrm_client.py.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import settings
from alfacrm_client import AlfaCRMClient
from bot.formatting import in_date_range, lesson_date_iso

logger = logging.getLogger(__name__)


class LessonCache:
    def __init__(self, crm: AlfaCRMClient):
        self.crm = crm
        self._lessons_by_id: Dict[Any, Dict[str, Any]] = {}
        self._lessons_by_teacher: Dict[Any, List[Dict[str, Any]]] = {}
        self._lessons_by_customer: Dict[Any, List[Dict[str, Any]]] = {}
        self._all_lessons: List[Dict[str, Any]] = []
        self._window_from: Optional[str] = None
        self._window_to: Optional[str] = None
        self._last_update: Optional[datetime] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self.scheduler = AsyncIOScheduler(timezone=settings.TZ)

    # ==================== ЖИЗНЕННЫЙ ЦИКЛ ====================

    def start(self) -> None:
        self._task = asyncio.create_task(self.refresh())
        self.scheduler.add_job(
            self.refresh,
            IntervalTrigger(minutes=settings.CACHE_REFRESH_MINUTES),
            id="lesson_cache_refresh",
            name="Обновление кеша уроков",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        logger.info(
            f"Кеш уроков запущен (окно −{settings.CACHE_DAYS_BACK}/+{settings.CACHE_DAYS_FORWARD} дней, "
            f"обновление каждые {settings.CACHE_REFRESH_MINUTES} мин)"
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("Кеш уроков остановлен")

    def _window(self):
        today = settings.today()
        return (
            (today - timedelta(days=settings.CACHE_DAYS_BACK)).isoformat(),
            (today + timedelta(days=settings.CACHE_DAYS_FORWARD)).isoformat(),
        )

    async def refresh(self) -> None:
        date_from, date_to = self._window()
        async with self._lock:
            try:
                lessons = await self.crm.get_lessons(date_from=date_from, date_to=date_to)
                lessons = [l for l in lessons if l.get("id") is not None]

                by_teacher: Dict[Any, List[Dict[str, Any]]] = {}
                by_customer: Dict[Any, List[Dict[str, Any]]] = {}
                for lesson in lessons:
                    for t_id in lesson.get("teacher_ids") or []:
                        by_teacher.setdefault(t_id, []).append(lesson)
                    for c_id in lesson.get("customer_ids") or []:
                        by_customer.setdefault(c_id, []).append(lesson)

                self._all_lessons = lessons
                self._lessons_by_id = {l["id"]: l for l in lessons}
                self._lessons_by_teacher = by_teacher
                self._lessons_by_customer = by_customer
                self._window_from, self._window_to = date_from, date_to
                self._last_update = settings.now()
                self._initialized = True

                logger.info(
                    f"✅ Кеш обновлён: {len(lessons)} уроков, "
                    f"педагогов {len(by_teacher)}, учеников {len(by_customer)}"
                )
                if not lessons:
                    logger.warning(
                        "⚠️ Кеш пуст. Проверьте ALFACRM_URL, BRANCH_ID, права "
                        "API-пользователя и наличие занятий в окне "
                        "(диагностика: python alfa_probe.py)."
                    )
                elif not by_customer:
                    # Занятия есть, а учеников на них нет — самый частый и
                    # самый непонятный со стороны случай: у преподавателя
                    # расписание видно, у родителя пусто. Логируем явно,
                    # чтобы это не выглядело как «бот сломался».
                    logger.warning(
                        f"⚠️ Занятий в кеше {len(lessons)}, но НИ НА ОДНОМ нет учеников. "
                        "Расписание и ДЗ у родителей будут пустыми: в карточках уроков "
                        "AlfaCRM пуст customer_ids — добавьте учеников в занятие или "
                        "в группу. Диагностика: python alfa_probe.py --phone +7XXXXXXXXXX"
                    )
            except Exception as e:
                # Старые данные не сбрасываем — лучше немного устаревшее
                # расписание, чем пустое.
                logger.error(f"❌ Ошибка обновления кеша уроков: {e}", exc_info=True)

    # ==================== ЧТЕНИЕ ====================

    def is_ready(self) -> bool:
        return self._initialized

    def covers(self, date_from: Optional[str], date_to: Optional[str]) -> bool:
        """Покрывает ли кеш запрошенный период целиком."""
        if not self._initialized or not self._window_from or not self._window_to:
            return False
        if date_from is None or date_to is None:
            return False
        return self._window_from <= date_from and date_to <= self._window_to

    def get_lessons(
        self,
        teacher_id: Optional[Any] = None,
        customer_id: Optional[Any] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        lesson_id: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        if not self._initialized:
            logger.warning("⚠️ Кеш ещё не инициализирован, возвращаем пустой список")
            return []

        if lesson_id is not None:
            lesson = self._lessons_by_id.get(lesson_id)
            return [lesson] if lesson else []

        if teacher_id is not None:
            lessons = self._lessons_by_teacher.get(teacher_id, [])
        elif customer_id is not None:
            lessons = self._lessons_by_customer.get(customer_id, [])
        else:
            lessons = self._all_lessons

        if status is not None:
            lessons = [l for l in lessons if l.get("status") == status]

        if date_from or date_to:
            # Фильтр обязан смотреть на нормализованную дату (см.
            # bot.formatting.in_date_range), а не только на поле `date`,
            # иначе уроки с датой только внутри time_from пропадают молча.
            lessons = [l for l in lessons if in_date_range(l, date_from, date_to)]

        return sorted(lessons, key=lambda l: (lesson_date_iso(l), str(l.get("time_from") or "")))

    def get_lesson(self, lesson_id: Any) -> Optional[Dict[str, Any]]:
        return self._lessons_by_id.get(lesson_id)

    async def patch_lesson(self, lesson_id: Any, updates: Dict[str, Any]) -> None:
        """
        Обновляет урок в кеше in-place.

        Списки по педагогам/ученикам держат ссылки на те же объекты,
        поэтому отдельно их обновлять не нужно.
        """
        async with self._lock:
            lesson = self._lessons_by_id.get(lesson_id)
            if lesson is not None:
                lesson.update(updates)
                logger.debug(f"✅ Кеш урока {lesson_id} обновлён: {list(updates.keys())}")
            else:
                logger.warning(f"⚠️ Урок {lesson_id} не найден в кеше, patch не применён")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_lessons": len(self._all_lessons),
            "teachers_count": len(self._lessons_by_teacher),
            "customers_count": len(self._lessons_by_customer),
            "window": [self._window_from, self._window_to],
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "initialized": self._initialized,
        }
