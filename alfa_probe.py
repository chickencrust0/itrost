#!/usr/bin/env python3
"""
alfa_probe.py — диагностика связки «бот ↔ AlfaCRM» без запуска бота.

Зачем: когда расписание пустое или родитель не входит по телефону,
причин обычно три — неверный BRANCH_ID, нет прав у API-пользователя,
или в CRM действительно нет данных в запрошенном окне. Различить их по
логам бота тяжело, а этот скрипт отвечает на вопрос за полминуты.

Запуск:
    python alfa_probe.py                      # общая проверка
    python alfa_probe.py --phone +79001234567 # найти человека по номеру
    python alfa_probe.py --days 7             # окно занятий, по умолчанию 14

Скрипт ТОЛЬКО читает: ни одной записи в CRM он не делает.
"""

import argparse
import asyncio
import sys
from datetime import timedelta

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError


def _head(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


async def probe(phone: str = "", days: int = 14) -> int:
    _head("КОНФИГУРАЦИЯ")
    print(f"ALFACRM_URL   : {settings.ALFACRM_URL or '❌ НЕ ЗАДАН'}")
    print(f"ALFACRM_EMAIL : {settings.ALFACRM_EMAIL or '❌ НЕ ЗАДАН'}")
    print(f"ALFACRM_API_KEY: {'задан' if settings.ALFACRM_API_KEY else '❌ НЕ ЗАДАН'}")
    print(f"BRANCH_ID     : {settings.BRANCH_ID}")
    print(f"Часовой пояс  : {settings.TIMEZONE}" + (f"  ⚠️ {settings.TZ_ERROR}" if settings.TZ_ERROR else ""))
    print(f"Сборка        : {settings.BUILD}")

    if not (settings.ALFACRM_URL and settings.ALFACRM_EMAIL and settings.ALFACRM_API_KEY):
        print("\n❌ Заполните .env и запустите снова.")
        return 1

    crm = AlfaCRMClient(
        base_url=settings.ALFACRM_URL,
        email=settings.ALFACRM_EMAIL,
        api_key=settings.ALFACRM_API_KEY,
        branch_id=settings.BRANCH_ID,
    )

    try:
        _head("АВТОРИЗАЦИЯ")
        await crm._ensure_token()
        print("✅ Токен получен")

        _head("СПРАВОЧНИКИ")
        try:
            teachers = await crm.load_all_teachers()
            print(f"Педагоги : {len(teachers)}")
            for t in teachers[:5]:
                print(f"   · id={t.get('id')} {crm.extract_user_name(t)} "
                      f"тел. {crm.extract_user_phone(t)}")
        except AlfaCRMError as e:
            print(f"❌ teacher/index: {e}")

        try:
            clients = await crm.load_all_clients()
            print(f"Ученики (is_study=1): {len(clients)}")
            for c in clients[:5]:
                print(f"   · id={c.get('id')} {crm.extract_user_name(c)} "
                      f"тел. {crm.extract_user_phone(c)}")
            if not clients:
                print("   ⚠️ Ни одного действующего ученика. Проверьте BRANCH_ID — "
                      "возможно, данные лежат в другом филиале.")
        except AlfaCRMError as e:
            print(f"❌ customer/index: {e}")

        _head(f"ЗАНЯТИЯ (окно ±{days} дн.)")
        today = settings.today()
        date_from = (today - timedelta(days=days)).isoformat()
        date_to = (today + timedelta(days=days)).isoformat()
        try:
            lessons = await crm.get_lessons(date_from=date_from, date_to=date_to)
            print(f"Период {date_from} – {date_to}: {len(lessons)} занятий")
            without_customers = [l for l in lessons if not l.get("customer_ids")]
            without_teachers = [l for l in lessons if not l.get("teacher_ids")]
            print(f"   без учеников  : {len(without_customers)}")
            print(f"   без педагогов : {len(without_teachers)}")
            for l in lessons[:5]:
                print(f"   · id={l['id']} {l['date']} {l.get('time_from')} "
                      f"педагоги={l.get('teacher_ids')} ученики={len(l.get('customer_ids') or [])} "
                      f"статус={l.get('status')}")
            if lessons and not any(l.get("customer_ids") for l in lessons):
                print("   ⚠️ НИ НА ОДНОМ занятии нет учеников — расписание и ДЗ "
                      "у родителей будут пустыми. Добавьте учеников в занятия.")
            if not lessons:
                print("   ⚠️ Занятий нет вовсе. Либо не тот BRANCH_ID, либо "
                      "расписание в CRM ещё не заполнено.")
        except AlfaCRMError as e:
            print(f"❌ lesson/index: {e}")

        _head("ПОСЕЩАЕМОСТЬ И ЗАМОРОЗКИ")
        # Проверяем ровно то, что чинилось в «Колибри» восемь итераций:
        # доступна ли отметка посещения и есть ли где хранить счётчик
        # заморозок. В AlfaCRM это обычные поля, так что ответ короткий.
        try:
            sample = next((l for l in lessons if l.get("customer_ids")), None)
        except NameError:
            sample = None
        if sample:
            attendance = await crm.get_attendance(sample["id"])
            print(f"Занятие {sample['id']} ({sample['date']}): "
                  f"учеников {len(sample.get('customer_ids') or [])}, "
                  f"отметок посещаемости {len(attendance)}")
            details = (sample.get("_raw") or {}).get("details")
            if details is None:
                print("   ⚠️ CRM не отдала поле details — отметка посещаемости "
                      "работать не будет. Проверьте права API-пользователя.")
            else:
                print("   ✅ поле details отдаётся, отметка посещаемости доступна")
        else:
            print("   ⚠️ нет занятия с учениками — проверить отметку не на чем")

        field = settings.ALFACRM_FIELD_FREEZES
        if not field:
            print("Счётчик заморозок: ALFACRM_FIELD_FREEZES не задан — "
                  "ведётся в базе бота")
        elif clients:
            has = field in clients[0]
            print(f"Счётчик заморозок: поле {field!r} "
                  + ("✅ есть в карточке ученика" if has else
                     "⚠️ в карточке отсутствует — счётчик будет вестись в базе бота"))
        if not settings.ALFACRM_REASON_EXCUSED:
            print("⚠️ ALFACRM_REASON_EXCUSED не задан: заморозки не будут "
                  "отмечаться в CRM (только в боте). Возьмите id уважительной "
                  "причины из справочника причин пропуска AlfaCRM.")

        if phone:
            _head(f"ПОИСК ПО НОМЕРУ {phone}")
            teacher = await crm.find_teacher_by_phone(phone)
            if teacher:
                print(f"✅ Педагог: id={teacher.get('id')} {crm.extract_user_name(teacher)}")
            customer = await crm.find_customer_by_phone(phone)
            if customer:
                cid = customer.get("id")
                print(f"✅ Ученик: id={cid} {crm.extract_user_name(customer)}")
                info = await crm.get_customer_info(cid)
                if info:
                    print(f"   баланс: {info.get('balance')} руб., "
                          f"остаток занятий: {info.get('lessons_left')}")
                    for s in info.get("subscriptions") or []:
                        print(f"   абонемент «{s['name']}»: {s['left']} из {s['total']}")
                lessons = await crm.get_lessons(
                    customer_id=cid, date_from=date_from, date_to=date_to
                )
                print(f"   занятий в окне: {len(lessons)}")
            if not teacher and not customer:
                print("❌ Номер не найден ни среди педагогов, ни среди учеников. "
                      "Бот на этом номере предложит оставить заявку.")

        _head("ИТОГ")
        print("Проверка завершена. Строки со знаком ⚠️ или ❌ выше — то, что "
              "помешает боту работать.")
        return 0
    finally:
        await crm.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика AlfaCRM для бота IT РОСТ")
    parser.add_argument("--phone", default="", help="проверить конкретный номер телефона")
    parser.add_argument("--days", type=int, default=14, help="полуширина окна занятий в днях")
    args = parser.parse_args()
    return asyncio.run(probe(args.phone, args.days))


if __name__ == "__main__":
    sys.exit(main())
