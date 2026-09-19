"""
freezes.py — счётчик оставшихся БЕСПРИЧИННЫХ заморозок.

Зачем отдельный модуль. Счётчик нужен в четырёх местах (карточка
остатка у родителя, заморозка родителем, заморозка преподавателем,
решение менеджера по неявке), а храниться он может в двух местах —
в дополнительном поле карточки ученика AlfaCRM или в базе самого бота.
Без общей точки правило «сначала CRM, потом база» пришлось бы повторить
четыре раза, и оно бы разъехалось — ровно так в боте «Колибри»
сломалось снятие отметки посещения: одно и то же правило было записано
дважды, и вторая копия отфильтровала по несуществующему полю.

Где хранится:

  * ALFACRM_FIELD_FREEZES задано И поле есть в карточке ученика ->
    источник CRM. Так менеджер видит и правит остаток прямо в CRM.
  * иначе -> таблица client_freezes в базе бота, а начальное значение
    берётся из FREEZE_FREE_PER_CLIENT.

Второй режим — рабочий по умолчанию: бот должен заработать сразу после
установки, не требуя заводить поля в CRM.
"""

import logging
from typing import Optional

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database

logger = logging.getLogger(__name__)


class FreezeCounter:
    def __init__(self, crm: AlfaCRMClient, db: Database):
        self.crm = crm
        self.db = db
        # None — ещё не выяснили; True/False — выяснили и больше не
        # дёргаем CRM лишний раз на каждое обращение.
        self._use_crm: Optional[bool] = None if settings.ALFACRM_FIELD_FREEZES else False

    async def get(self, client_id) -> int:
        """
        Остаток заморозок. Ошибку CRM НЕ проглатывает: родителю нельзя
        показать выдуманный ноль вместо «не смогли прочитать» — по нулю
        он решит, что заморозки кончились, и не станет их использовать.
        """
        if self._use_crm is not False:
            value = await self.crm.get_crm_freezes(client_id)
            if value is not None:
                self._use_crm = True
                return value
            self._use_crm = False
        return self.db.get_free_freezes(client_id, settings.FREEZE_FREE_PER_CLIENT)

    async def set(self, client_id, value: int) -> int:
        value = max(0, int(value))
        if self._use_crm:
            try:
                if await self.crm.set_crm_freezes(client_id, value):
                    return value
            except AlfaCRMError as e:
                # Запись не прошла — но списание уже показано человеку.
                # Пишем в базу, чтобы остаток не «отрос» обратно, и
                # оставляем след в логе.
                logger.warning(
                    f"⚠️ Не удалось записать остаток заморозок клиента {client_id} "
                    f"в CRM ({e}) — сохранено в базе бота"
                )
        self.db.set_free_freezes(client_id, value)
        return value

    async def spend(self, client_id) -> int:
        """
        Расходует одну заморозку, возвращает остаток ПОСЛЕ списания.
        Если заморозок нет — ошибка: увести счётчик в минус хуже, чем
        сказать родителю, что бесплатные заморозки кончились.
        """
        left = await self.get(client_id)
        if left <= 0:
            raise AlfaCRMError("Беспричинных заморозок не осталось")
        return await self.set(client_id, left - 1)

    async def refund(self, client_id) -> int:
        """
        Возвращает заморозку — например, когда менеджер признал причину
        уважительной уже после списания.
        """
        return await self.set(client_id, await self.get(client_id) + 1)
