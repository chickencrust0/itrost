"""
scheduler.py — фоновые задания: утренняя рассылка, напоминания о скором
уроке, ежедневная сводка менеджеру, проверка баланса, чистка лога.

Портировано из alfacrm-bot без изменений в логике задач — только вызовы
alfacrm -> crm и bot.send_message(chat_id, text, ...) -> MaxBot API
(user_id=, text=, attachments=).
"""

import asyncio
import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.formatting import (
    STATUS_PLANNED,
    build_schedule,
    esc,
    fmt_date_long,
    format_reminder,
    freeze_deadline_hint,
    parse_lesson_datetime,
    safe_call,
    send_blocks,
)
from bot.handlers.common import (
    fetch_lessons,
    get_lesson_snapshot,
    get_lesson_summary,
    load_customer_map,
    manager_ids,
)
from max_api.keyboards import absence_decision_keyboard, lesson_action_keyboard

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, db: Database, crm: AlfaCRMClient, bot, cache=None):
        self.db = db
        self.crm = crm
        self.bot = bot
        self.cache = cache
        # Все задания живут в часовом поясе филиала, а не сервера.
        self.scheduler = AsyncIOScheduler(timezone=settings.TZ)
        self.db_managers = db  # список получателей берётся динамически

    def start(self):
        common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
        self.scheduler.add_job(
            self.send_daily_schedule, CronTrigger(hour=8, minute=0, timezone=settings.TZ),
            id="daily", **common,
        )
        self.scheduler.add_job(
            self.check_upcoming_lessons, IntervalTrigger(minutes=5), id="upcoming", **common,
        )
        self.scheduler.add_job(
            self.send_daily_summary, CronTrigger(hour=0, minute=1, timezone=settings.TZ),
            id="daily_summary", **common,
        )
        self.scheduler.add_job(
            self.send_absence_digest,
            CronTrigger(
                hour=settings.ABSENCE_DIGEST_HOUR,
                minute=settings.ABSENCE_DIGEST_MINUTE,
                timezone=settings.TZ,
            ),
            id="absence_digest", **common,
        )
        self.scheduler.add_job(
            self.notify_burned_lessons,
            IntervalTrigger(minutes=settings.BURN_CHECK_INTERVAL_MINUTES),
            id="burned", **common,
        )
        self.scheduler.add_job(
            self.cleanup, CronTrigger(hour=3, minute=30, timezone=settings.TZ),
            id="cleanup", **common,
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен (TZ={settings.TIMEZONE})")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("⏹ Планировщик остановлен")

    # ==================== УТРЕННЯЯ РАССЫЛКА ====================

    async def send_daily_schedule(self):
        today = settings.today()
        today_iso = today.isoformat()

        try:
            customers = await load_customer_map(self.crm)
        except Exception as e:
            logger.error(f"Не удалось загрузить карту учеников: {e}")
            customers = {}

        for role in ("teacher", "parent"):
            for user in self.db.get_all_users_by_role(role):
                if not self.db.notification_enabled(user["max_user_id"], "lessons"):
                    continue
                try:
                    kwargs = (
                        {"teacher_id": user["crm_id"]} if role == "teacher"
                        else {"customer_id": user["crm_id"]}
                    )
                    lessons = await fetch_lessons(
                        self.crm, self.cache, db=self.db,
                        status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
                        **kwargs,
                    )
                    if not lessons:
                        continue

                    blocks = build_schedule(
                        lessons,
                        role=role,
                        title=f"Расписание на сегодня, {fmt_date_long(today)}",
                        customers=customers,
                        today=today,
                    )
                    await send_blocks(self.bot, user["max_user_id"], blocks)
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['max_user_id']}: {e}")

    # ==================== НАПОМИНАНИЯ ====================

    async def check_upcoming_lessons(self):
        now = settings.now()
        today_iso = now.date().isoformat()

        try:
            lessons = await fetch_lessons(
                self.crm, self.cache, db=self.db,
                status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
            )
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки уроков: {e}")
            return

        for lesson in lessons:
            lesson_time = parse_lesson_datetime(lesson)
            if not lesson_time:
                continue

            # ОДНО напоминание на занятие — за час до начала.
            # Напоминание за 15 минут убрано: два сообщения об одном и
            # том же занятии воспринимаются как спам, а за 15 минут
            # что-то менять уже поздно.
            #
            # Окно 55–65 минут шире, чем интервал планировщика (5 минут),
            # поэтому занятие не может «проскочить» между тиками. От
            # повторов внутри окна защищает mark_reminder_sent.
            minutes_left = (lesson_time - now).total_seconds() / 60
            if 55 <= minutes_left <= 65:
                await self._notify_lesson(lesson, "1 час")

    async def _notify_lesson(self, lesson, when: str):
        lesson_id = lesson.get("id")
        if not lesson_id:
            return

        # Тип напоминания один и тот же при записи и при проверке —
        # если эти строки разойдутся, дедупликация перестанет работать
        # и напоминания задублируются на каждом тике планировщика.
        reminder_type = f"upcoming_{when}"

        try:
            customers = await load_customer_map(self.crm)
        except Exception:
            customers = {}

        for teacher_id in lesson.get("teacher_ids") or []:
            teacher = self.db.get_user_by_crm_id(teacher_id, "teacher")
            if not teacher:
                continue
            if not self.db.notification_enabled(teacher["max_user_id"], "lessons"):
                continue
            if self.db.was_reminder_sent(lesson_id, reminder_type, teacher["max_user_id"], hours=6):
                continue
            try:
                await safe_call(lambda t=teacher: self.bot.send_message(
                    user_id=t["max_user_id"],
                    text=format_reminder(lesson, when=when, role="teacher", customers=customers),
                    fmt="html",
                    attachments=lesson_action_keyboard(lesson_id),
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, teacher["max_user_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить преподавателю {teacher['max_user_id']}: {e}")

        for customer_id in lesson.get("customer_ids") or []:
            parent = self.db.get_user_by_crm_id(customer_id, "parent")
            if not parent:
                continue
            if not self.db.notification_enabled(parent["max_user_id"], "lessons"):
                continue
            if self.db.was_reminder_sent(lesson_id, reminder_type, parent["max_user_id"], hours=6):
                continue
            try:
                await safe_call(lambda p=parent: self.bot.send_message(
                    user_id=p["max_user_id"],
                    text=format_reminder(lesson, when=when, role="parent"),
                    fmt="html",
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, parent["max_user_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить родителю {parent['max_user_id']}: {e}")

    # ==================== ЕЖЕДНЕВНАЯ СВОДКА ====================

    async def send_daily_summary(self):
        yesterday = settings.today() - timedelta(days=1)
        yesterday_iso = yesterday.isoformat()
        period_label = f"за {fmt_date_long(yesterday)}"

        try:
            lessons = await fetch_lessons(
                self.crm, self.cache, db=self.db,
                date_from=yesterday_iso, date_to=yesterday_iso,
            )
        except Exception as e:
            logger.error(f"❌ Ошибка получения уроков для сводки: {e}", exc_info=True)
            return

        try:
            blocks = await get_lesson_summary(lessons, self.db, self.crm, period_label)
        except Exception as e:
            logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
            return

        for manager_id in (
            uid for uid in manager_ids(self.db)
            if self.db.notification_enabled(uid, "summaries")
        ):
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")

    # ==================== ОСТАТОК ЗАНЯТИЙ ====================
    #
    # Ежедневная проверка остатка (check_low_balance) УБРАНА вместе с
    # уведомлениями «занятия заканчиваются» — и родителям, и менеджеру.
    # Остаток теперь показывается только тогда, когда родитель сам
    # нажмёт «🎟 Остаток занятий»: рассылка про чужой абонемент читается
    # как навязывание продления, а менеджеру список из десятков учеников
    # каждое утро всё равно не нужен.

    # ==================== СВОДКА ПО НЕЯВКАМ ====================

    async def send_absence_digest(self):
        """
        Вечерняя сводка менеджеру за сегодня: кто был отмечен, а кто нет.

        «Не отмечен» — это не то же самое, что «не пришёл»: преподаватель
        мог просто не успеть отметить. Поэтому такие ученики показываются
        отдельным списком, без кнопок решения: списывать занятие у того,
        кого никто не отмечал, нельзя.

        Кнопки решения даются только по явно отмеченным неявкам, и их
        три — сгорание, беспричинная заморозка, уважительная причина.
        """
        today = settings.today()
        today_iso = today.isoformat()
        recipients = [
            uid for uid in manager_ids(self.db)
            if self.db.notification_enabled(uid, "summaries")
        ]
        if not recipients:
            logger.warning(
                "⚠️ Сводка по неявкам не отправлена: менеджеров нет. "
                "Пусть менеджер войдёт командой /manager."
            )
            return

        absences = self.db.get_absences_for_date(today_iso, status="pending")

        # Кто сегодня отмечен присутствующим — берём из самой CRM.
        present_ids, unmarked = set(), []
        try:
            lessons = await fetch_lessons(
                self.crm, self.cache, db=self.db,
                date_from=today_iso, date_to=today_iso,
            )
            # Кто отмечен присутствующим — из поля details занятий за
            # сегодня. В боте «Колибри» здесь приходилось тянуть все
            # визиты дня и сопоставлять их с учениками через абонементы;
            # в AlfaCRM customer_id лежит прямо в детали урока.
            for lesson in lessons:
                for cid, is_attend in (
                    await self.crm.get_attendance(lesson.get("id"))
                ).items():
                    if is_attend:
                        present_ids.add(
                            int(cid) if str(cid).lstrip("-").isdigit() else cid
                        )

            absent_ids = {a["client_id"] for a in absences}
            customers = await load_customer_map(self.crm)
            seen = set()
            for lesson in lessons:
                for cid in lesson.get("customer_ids") or []:
                    if cid in present_ids or cid in absent_ids or cid in seen:
                        continue
                    seen.add(cid)
                    unmarked.append((cid, customers.get(cid, f"ID:{cid}")))
        except Exception as e:
            logger.error(f"Не удалось собрать сводку посещаемости: {e}", exc_info=True)

        if not absences and not unmarked and not present_ids:
            logger.info(f"Сводка за {today_iso}: отмечать нечего")
            return

        head = (
            f"📊 <b>Посещаемость за {esc(fmt_date_long(today))}</b>\n\n"
            f"✅ Отмечены присутствующими: {len(present_ids)}\n"
            f"❌ Отмечены как не пришедшие: {len(absences)}\n"
            f"❔ Никак не отмечены: {len(unmarked)}"
        )

        for manager_id in recipients:
            try:
                await safe_call(lambda mid=manager_id: self.bot.send_message(
                    user_id=mid, text=head, fmt="html",
                ))

                for i, a in enumerate(absences):
                    if i:
                        await asyncio.sleep(settings.MAX_SEND_DELAY)
                    await safe_call(lambda mid=manager_id, r=a: self.bot.send_message(
                        user_id=mid,
                        text=(
                            f"❌ <b>Не пришёл</b>\n"
                            f"👤 {esc(r.get('client_name') or r['client_id'])}\n"
                            f"📅 {esc(r['lesson_date'])}\n"
                            f"🆔 Занятие: {esc(r['lesson_id'])}"
                        ),
                        fmt="html",
                        attachments=absence_decision_keyboard(r["id"]),
                    ))

                if unmarked:
                    listed = "\n".join(f"• {esc(name)}" for _, name in unmarked[:30])
                    more = f"\n… и ещё {len(unmarked) - 30}" if len(unmarked) > 30 else ""
                    await asyncio.sleep(settings.MAX_SEND_DELAY)
                    await safe_call(lambda mid=manager_id, l=listed, m=more: self.bot.send_message(
                        user_id=mid,
                        text=(
                            f"❔ <b>Никак не отмечены</b>\n"
                            f"<i>Преподаватель не поставил ни присутствие, ни неявку.</i>\n\n"
                            f"{l}{m}"
                        ),
                        fmt="html",
                    ))
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")

    # ==================== СГОРЕВШИЕ ЗАНЯТИЯ ====================

    async def notify_burned_lessons(self):
        """
        Лояльное уведомление родителю о сгоревшем занятии.

        Занятие считается сгоревшим, когда сошлись три условия:
          * преподаватель отметил ученика как не пришедшего;
          * с начала занятия прошло не меньше BURN_NOTIFY_AFTER_MINUTES
            минут (запас на то, чтобы отметку успели поставить);
          * родитель НЕ заморозил это занятие — а заморозить он мог
            только за FREEZE_DEADLINE_HOURS часов до начала.

        Уведомление именно уведомление, а не приговор: решение списывать
        занятие остаётся за менеджером (см. manager.absence_burn), поэтому
        текст мягкий и с прямым предложением связаться, если это ошибка.
        Дедупликация — через reminder_log, чтобы одно и то же занятие не
        напоминало о себе каждые 15 минут.
        """
        now = settings.now()
        today = now.date()
        candidates = []
        for day in (today, today - timedelta(days=1)):
            try:
                # status=None: уведомляем и по нерешённым неявкам, и по тем,
                # что менеджер уже списал. Не уведомляем только по
                # 'excused' — там занятие как раз не сгорело.
                candidates.extend(
                    a for a in self.db.get_absences_for_date(day.isoformat(), status=None)
                    if a.get("status") != "excused"
                )
            except Exception as e:
                logger.error(f"Не удалось прочитать неявки за {day}: {e}")

        if not candidates:
            return

        for absence in candidates:
            lesson_id = absence.get("lesson_id")
            client_id = absence.get("client_id")
            if not lesson_id or client_id is None:
                continue

            parent = self.db.get_user_by_crm_id(client_id, "parent")
            if not parent:
                continue  # родителя нет в боте — уведомлять некого
            if not self.db.notification_enabled(parent["max_user_id"], "updates"):
                continue

            reminder_type = f"burned:{client_id}"
            if self.db.was_reminder_sent(
                lesson_id, reminder_type, parent["max_user_id"], hours=24 * 30
            ):
                continue

            # Заморожено — значит не сгорело.
            try:
                if self.db.get_freeze_for_lesson(client_id, lesson_id):
                    continue
            except Exception as e:
                logger.warning(f"Не удалось проверить заморозку {lesson_id}: {e}")

            lesson = await get_lesson_snapshot(
                lesson_id, self.crm, self.cache, db=self.db
            )
            start = parse_lesson_datetime(lesson) if lesson else None
            if start is None:
                # Время неизвестно — берём дату неявки и считаем, что
                # занятие точно прошло, если наступил следующий день.
                if absence.get("lesson_date") == today.isoformat():
                    continue
            elif (now - start).total_seconds() / 60 < settings.BURN_NOTIFY_AFTER_MINUTES:
                continue

            when = (
                start.strftime("%d.%m в %H:%M")
                if start
                else str(absence.get("lesson_date") or "")
            )
            text = (
                "😔 <b>Занятие сгорело</b>\n\n"
                f"Ребёнок не был на занятии {esc(when)}, и заморозить его "
                f"уже не получилось — это возможно не позднее чем за "
                f"{freeze_deadline_hint()} до начала.\n\n"
                "Если это ошибка или была уважительная причина — просто "
                "напишите менеджеру, он разберётся и вернёт занятие. "
                "Ничего страшного не произошло 🙂"
            )
            try:
                await safe_call(lambda p=parent, t=text: self.bot.send_message(
                    user_id=p["max_user_id"], text=t, fmt="html",
                ))
                self.db.mark_reminder_sent(
                    lesson_id, reminder_type, parent["max_user_id"]
                )
                logger.info(
                    f"🔥 Уведомление о сгоревшем занятии: клиент {client_id}, "
                    f"занятие {lesson_id}"
                )
            except Exception as e:
                logger.error(
                    f"Не удалось уведомить родителя {parent['max_user_id']} "
                    f"о сгоревшем занятии: {e}"
                )

    # ==================== ОБСЛУЖИВАНИЕ ====================

    async def cleanup(self):
        try:
            removed = self.db.cleanup_reminder_log(days=30)
            logger.info(f"🧹 Лог напоминаний: удалено {removed} записей")
        except Exception as e:
            logger.error(f"Не удалось почистить лог напоминаний: {e}")

        # Журнал активности растёт быстрее всего остального (несколько
        # строк на каждого посетителя), а смысл имеет только свежий —
        # менеджер смотрит «кто приходил», а не историю за полгода.
        try:
            removed = self.db.cleanup_activity(days=settings.ACTIVITY_KEEP_DAYS)
            logger.info(f"🧹 Журнал активности: удалено {removed} записей")
        except Exception as e:
            logger.error(f"Не удалось почистить журнал активности: {e}")
