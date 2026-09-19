"""
ssl_utils.py — общий SSL-контекст для HTTP-клиентов бота.

Кроме обычного набора корневых сертификатов (certifi) добавляет цепочку
Russian Trusted Root CA / Sub CA (НУЦ Минцифры России). Без неё запросы
к platform-api2.max.ru падают с CERTIFICATE_VERIFY_FAILED / unable to
get local issuer certificate — в документации MAX (dev.max.ru/docs-api,
раздел «Обзор») прямо сказано: «убедитесь, что добавили сертификат
Минцифры в список доверенных».

Сертификаты — публичные корневые сертификаты аккредитованного
удостоверяющего центра Минцифры России (действительны до 2032/2027),
взяты из зеркала https://github.com/koenrh/russian-trusted-root-ca
(оригинал: https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
и .../russian_trusted_sub_ca_pem.crt — сам домен gu-st.ru недоступен из
окружения, в котором собирался этот бот, поэтому файлы приложены прямо
в репозитории, а не скачиваются на лету). Добавление доверия к этим
двум сертификатам не снижает безопасность — это официальный
государственный удостоверяющий центр, а не проверка отключается.
"""

import logging
import ssl
from pathlib import Path

import certifi

logger = logging.getLogger(__name__)

_CERTS_DIR = Path(__file__).parent / "certs"
_EXTRA_CA_FILES = ("russian_trusted_root_ca.pem", "russian_trusted_sub_ca.pem")


def build_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """
    verify=False полностью отключает проверку сертификата — только для
    диагностики на машинах с антивирусом/корпоративным прокси,
    подменяющим TLS-сертификаты (например, Kaspersky с включённой
    проверкой защищённых соединений). Использовать в бою НЕ рекомендуется:
    соединение перестаёт быть защищено от подмены сервера.
    """
    if not verify:
        logger.warning(
            "⚠️ Проверка SSL-сертификата ОТКЛЮЧЕНА (verify=False) — "
            "используйте только для диагностики, не в бою"
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ctx = ssl.create_default_context(cafile=certifi.where())
    for name in _EXTRA_CA_FILES:
        path = _CERTS_DIR / name
        if path.exists():
            ctx.load_verify_locations(cafile=str(path))
        else:
            logger.warning(f"⚠️ Не найден сертификат {path} — доверие Минцифры не добавлено")
    return ctx
