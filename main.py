"""
main.py — точка входа бота IT РОСТ.

Порядок запуска: клиент CRM -> кеш -> БД -> счётчик заморозок ->
планировщик -> диспетчер апдейтов. Источник апдейтов — MAX (long polling
по умолчанию, вебхук — через MAX_MODE=webhook), источник данных —
AlfaCRM, филиал BRANCH_ID.

Long Polling подходит для разработки/тестирования, но не для
продакшена (см. dev.max.ru/docs-api, «Рекомендации по работе с API») —
переключитесь на MAX_MODE=webhook и настройте HTTPS с доверенным
сертификатом (в т.ч. Минцифры) перед боевым запуском.
"""

import asyncio
import logging
import signal

from aiohttp import web

import settings
from bot.notifier import ManagerNotifier
from cache import LessonCache
from database import Database
from alfacrm_client import AlfaCRMClient
from max_api.client import MaxBot
from scheduler import ReminderScheduler
from bot.dispatcher import Dispatcher
from bot.handlers import common as _common  # noqa: F401  (регистрирует реэкспорт)
from bot.handlers import lead, manager, parent, start, support, teacher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

UPDATE_TYPES = [
    "message_created",
    "message_callback",
    "bot_started",
]


def _check_config() -> None:
    problems = []
    if not settings.MAX_BOT_TOKEN:
        problems.append("MAX_BOT_TOKEN не задан")
    if not settings.ALFACRM_URL:
        problems.append("ALFACRM_URL не задан")
    if not settings.ALFACRM_EMAIL or not settings.ALFACRM_API_KEY:
        problems.append("ALFACRM_EMAIL/ALFACRM_API_KEY не заданы")
    if not settings.BRANCH_ID:
        problems.append("BRANCH_ID не задан (для IT РОСТ это 2)")
    if not settings.MANAGER_IDS:
        problems.append("ADMIN_MAX_IDS пуст — некому будет прийти сводкам и заявкам")
    if settings.TZ_ERROR:
        problems.append(f"часовой пояс: {settings.TZ_ERROR}")

    for p in problems:
        logger.warning(f"⚠️ {p}")


def build_dispatcher(
    bot: MaxBot,
    db: Database,
    crm: AlfaCRMClient,
    cache: LessonCache,
    notifier: ManagerNotifier,
) -> Dispatcher:
    dp = Dispatcher(
        bot, db=db, crm=crm, cache=cache, notifier=notifier
    )
    # Порядок важен. Диспетчер останавливается на ПЕРВОМ совпавшем
    # обработчике, а в start.py есть фильтр F.text.regexp с номером
    # телефона — он совпадает и с датой вида 2026-09-01, которую вводит
    # менеджер в сводке. Поэтому start подключается ПОСЛЕДНИМ: все
    # роутеры выше привязаны к состояниям FSM и чужого не перехватывают,
    # а /start и вход по телефону всё равно больше никто не обрабатывает.
    # support — первым: пока пользователь в диалоге с менеджером, его текст
    # должен уходить в обращение. Хендлеры привязаны к состоянию FSM,
    # поэтому чужие сценарии он не перехватывает.
    # Authentication commands run first. Phone matching now requires at least
    # ten digits, so it no longer steals date input from role workflows.
    dp.include_router(start.router)
    dp.include_router(support.router)
    dp.include_router(lead.router)
    dp.include_router(teacher.router)
    dp.include_router(parent.router)
    dp.include_router(manager.router)
    return dp


async def run_polling(bot: MaxBot, dp: Dispatcher) -> None:
    logger.info("▶️ Запуск в режиме long polling")
    marker = None
    while True:
        try:
            result = await bot.get_updates(
                marker=marker, limit=100, timeout=settings.MAX_LONGPOLL_TIMEOUT, types=UPDATE_TYPES
            )
        except Exception as e:
            logger.error(f"Ошибка long polling: {e}")
            await asyncio.sleep(2)
            continue

        updates = (result or {}).get("updates") or []
        for update in updates:
            asyncio.create_task(dp.feed_update(update))

        new_marker = (result or {}).get("marker")
        if new_marker is not None:
            marker = new_marker


async def run_webhook(bot: MaxBot, dp: Dispatcher) -> None:
    logger.info(f"▶️ Запуск в режиме webhook на {settings.MAX_WEBHOOK_HOST}:{settings.MAX_WEBHOOK_PORT}")

    async def handle(request: web.Request) -> web.Response:
        if settings.MAX_WEBHOOK_SECRET:
            if request.headers.get("X-Max-Bot-Api-Secret") != settings.MAX_WEBHOOK_SECRET:
                return web.Response(status=401)
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400)
        asyncio.create_task(dp.feed_update(update))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post(settings.MAX_WEBHOOK_PATH, handle)

    if settings.MAX_WEBHOOK_URL:
        try:
            await bot.subscribe(
                settings.MAX_WEBHOOK_URL, UPDATE_TYPES, settings.MAX_WEBHOOK_SECRET or None
            )
            logger.info(f"✅ Подписка на вебхук оформлена: {settings.MAX_WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"❌ Не удалось оформить подписку на вебхук: {e}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.MAX_WEBHOOK_HOST, settings.MAX_WEBHOOK_PORT)
    await site.start()

    stop_event = asyncio.Event()
    await stop_event.wait()


async def main() -> None:
    _check_config()

    bot = MaxBot(settings.MAX_BOT_TOKEN)
    try:
        me = await bot.get_me()
        logger.info(f"🤖 Бот подключён: {me.get('name') or me.get('username') or me}")
        logger.info(f"📦 Версия сборки: {settings.BUILD}")
        logger.info(
            f"🏢 Филиал AlfaCRM: {settings.BRANCH_ID} "
            f"({settings.ALFACRM_URL or 'URL не задан'})"
        )
    except Exception as e:
        logger.error(f"❌ Не удалось подключиться к MAX: {e}")

    crm = AlfaCRMClient(
        base_url=settings.ALFACRM_URL,
        email=settings.ALFACRM_EMAIL,
        api_key=settings.ALFACRM_API_KEY,
        branch_id=settings.BRANCH_ID,
    )
    db = Database(settings.DB_PATH)
    cache = LessonCache(crm)
    cache.start()

    reminder_scheduler = ReminderScheduler(db, crm, bot, cache)
    reminder_scheduler.start()

    notifier = ManagerNotifier(bot, db)
    dp = build_dispatcher(bot, db, crm, cache, notifier)

    stop = asyncio.Event()

    def _on_signal():
        logger.info("⏹ Получен сигнал остановки")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    runner_task = asyncio.create_task(
        run_webhook(bot, dp) if settings.MAX_MODE == "webhook" else run_polling(bot, dp)
    )

    # На Windows loop.add_signal_handler недоступен (NotImplementedError
    # выше), поэтому Ctrl+C приходит в виде KeyboardInterrupt прямо в
    # asyncio.run() и отменяет текущую задачу — раньше это пропускало
    # блок очистки ниже целиком (cache.stop()/scheduler.stop()/close()),
    # отсюда "Unclosed client session" при выходе. try/finally гарантирует
    # очистку при любом способе остановки.
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        runner_task.cancel()
        try:
            await runner_task
        except (asyncio.CancelledError, Exception):
            pass
        await notifier.shutdown()
        cache.stop()
        reminder_scheduler.stop()
        await crm.close()
        await bot.close()
        logger.info("👋 Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
