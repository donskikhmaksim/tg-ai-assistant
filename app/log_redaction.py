"""Маскировка секретов, которые едут В САМОМ URL, перед выводом в лог (#121).

ЗАЧЕМ. У этого бота два места, где пропуск лежит не в заголовке, а прямо
в адресе, и потому попадает в КАЖДУЮ строку access-лога aiohttp:

1. read-only MCP-сервер смонтирован на `/mcp/<MCP_READONLY_SECRET>`
   (app/mcp_readonly.py::register_routes). Секрет не истекает и не
   ротируется — доступ к логам Railway = полный доступ к чтению всей
   переписки владельца через MCP.
2. страница транскрипта открывается ссылкой `/chat?c=<chat_id>&t=<token>`,
   где `t` — вечный HMAC-токен (app/web/auth.py::chat_link_token). Он и
   есть пропуск: по нему читается вся история конкретного чата без
   Telegram-авторизации.

aiohttp по умолчанию логирует каждый запрос строкой формата
`%a %t "%r" %s %b "%{Referer}i" "%{User-Agent}i"` через логгер
`aiohttp.access` (aiohttp/web_log.py::AccessLogger.log), то есть путь с
секретом уходит в stdout сервиса и живёт там всю историю логов.

ЧТО ДЕЛАЕМ. Ставим logging-фильтр, который перед выводом подменяет
секретные куски адреса на заглушку. Снаружи не меняется НИЧЕГО: URL те же,
клиенты (Cowork, ссылки из TickTick) ничего не замечают — меняется только
текст, попадающий в лог:

    10.0.0.1 [..] "POST /mcp/<mcp-secret> HTTP/1.1" 200 123 "-" "curl"
    10.0.0.1 [..] "GET /chat?c=-100123&t=<chat-token> HTTP/1.1" 200 4096 ...

Отладка по логам сохраняется полностью: адрес клиента, метод, путь, код
ответа, размер ответа, время — всё на месте, вырезается ровно тот сегмент,
который является паролем.

ОСОБЕННОСТЬ aiohttp (проверено по aiohttp/web_log.py, а не по аналогии с
uvicorn). AccessLogger.log() делает

    self.logger.info(self._log_format % tuple(values), extra=extra)

то есть путь УЖЕ подставлен в текст сообщения (`record.msg`, `record.args`
пуст) — в отличие от uvicorn, где он приезжает отдельным аргументом. Но
тот же путь ДОПОЛНИТЕЛЬНО кладётся в `extra` и становится атрибутом
записи (`record.first_request_line`), а его печатает любой форматтер вида
`%(first_request_line)s` и любой JSON-хендлер. Поэтому фильтр чистит и
текст, и эти атрибуты — правка одного только текста оставила бы дыру для
структурированного лога.

ЧЕГО ЭТО НЕ ДЕЛАЕТ. Секреты, уже попавшие в старые логи, остаются там: их
надо считать скомпрометированными и ротировать отдельно
(`MCP_READONLY_SECRET` — сменой переменной, chat-токены — сменой
BOT_TOKEN, которым они подписаны). И это не замена переносу секрета из
пути в заголовок (ломающее изменение, отдельная задача).
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Iterable, Optional

#: Чем заменяем MCP_READONLY_SECRET в пути.
SECRET_PLACEHOLDER = "<mcp-secret>"
#: Чем заменяем подписанный токен ссылки на транскрипт (`/chat?...&t=`).
CHAT_TOKEN_PLACEHOLDER = "<chat-token>"
#: Чем заменяем токен бота внутри URL Telegram Bot API.
BOT_TOKEN_PLACEHOLDER = "bot<bot-token>"

# Ниже этой длины подстроку НЕ вырезаем по всему тексту: короткая строка
# («ok», «id») может встретиться где угодно в осмысленном сообщении, и
# слепая замена сделает логи бесполезными. Секрет короче 8 символов и так
# не секрет; позиционные правила ниже накроют его всё равно.
_MIN_INLINE_SECRET_LEN = 8

# Позиционные правила. Работают, даже если значение секрета фильтру
# неизвестно (например ссылку выдал другой процесс) и независимо от длины.
#
# 1. сегмент пути сразу после /mcp — это и есть пропуск к MCP-серверу.
_MCP_PATH_RE = re.compile(r"(?<![\w/])(/mcp/)([^/?\s\"']+)")
# 2. query-параметр `t=` — подписанный токен ссылки на транскрипт. Требуем
#    перед ним `?` или `&`, чтобы не трогать осмысленный текст, где «t=»
#    может встретиться сам по себе.
_CHAT_TOKEN_RE = re.compile(r"([?&]t=)([^&\s\"']+)")
# 3. токен бота в URL Telegram Bot API: https://api.telegram.org/bot<TOKEN>/…
#    и /file/bot<TOKEN>/… (по нему скачиваются голосовые). Формат токена —
#    «<цифры>:<буквы-цифры-дефисы-подчёркивания>».
_BOT_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")


def redact(text: str, secret: Optional[str] = None) -> str:
    """Вернуть `text` с замаскированными секретами адреса.

    Идемпотентна: повторный вызов на уже замаскированной строке ничего не
    меняет (заглушки не совпадают ни с одним правилом на второй проход).
    """
    if not text:
        return text
    if secret and len(secret) >= _MIN_INLINE_SECRET_LEN:
        text = text.replace(secret, SECRET_PLACEHOLDER)
        # Путь доезжает до лога в том виде, в каком его прислал клиент:
        # секрет с не-URL-безопасными символами будет в процентной
        # кодировке и на сырое значение уже не похож.
        quoted = urllib.parse.quote(secret, safe="")
        if quoted != secret:
            text = text.replace(quoted, SECRET_PLACEHOLDER)
    text = _MCP_PATH_RE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, text)
    text = _CHAT_TOKEN_RE.sub(lambda m: m.group(1) + CHAT_TOKEN_PLACEHOLDER, text)
    text = _BOT_TOKEN_RE.sub(BOT_TOKEN_PLACEHOLDER, text)
    return text


def _redact_any(value: Any, secret: Optional[str]) -> Any:
    """Рекурсивно почистить строку / кортеж / словарь, всё прочее — как есть."""
    if isinstance(value, str):
        return redact(value, secret)
    if isinstance(value, dict):
        return {k: _redact_any(v, secret) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_any(v, secret) for v in value)
    return value


#: Атрибуты записи, которые aiohttp кладёт через `extra=` (см.
#: AccessLogger.LOG_FORMAT_MAP). Там же лежит путь целиком, поэтому
#: структурированный форматтер напечатал бы секрет мимо текста сообщения.
_AIOHTTP_EXTRA_KEYS = (
    "first_request_line",
    "request_header",
    "response_header",
    "remote_address",
)


class SecretPathFilter(logging.Filter):
    """Фильтр, маскирующий секреты адреса в сообщении, аргументах и extra.

    Никогда не отбрасывает записи (всегда True) — его задача отредактировать
    текст, а не решать, что печатать.
    """

    def __init__(self, secret: Optional[str] = None) -> None:
        super().__init__()
        self.secret = secret or None

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, self.secret)
        args = record.args
        if isinstance(args, dict):
            record.args = {k: _redact_any(v, self.secret) for k, v in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(_redact_any(a, self.secret) for a in args)
        for key in _AIOHTTP_EXTRA_KEYS:
            value = record.__dict__.get(key)
            if value is not None:
                record.__dict__[key] = _redact_any(value, self.secret)
        return True


#: Логгеры, на которые фильтр вешается напрямую: их записи и содержат путь.
#: `aiohttp.access` — access-строки; `aiohttp.server`/`aiohttp.web` —
#: сообщения об ошибках обработки запроса, где путь тоже светится.
_AIOHTTP_LOGGERS = ("aiohttp.access", "aiohttp.server", "aiohttp.web")


def install(secret: Optional[str] = None,
            logger_names: Iterable[str] = _AIOHTTP_LOGGERS) -> SecretPathFilter:
    """Повесить фильтр на aiohttp-логгеры и на корневые хендлеры.

    Две точки крепления — это не дублирование, а разное покрытие:

    * фильтр НА ЛОГГЕРЕ `aiohttp.access` мутирует запись в самом источнике,
      поэтому работает независимо от того, какие хендлеры появятся позже и
      выставят ли логгеру `propagate=False` (aiohttp сам вешает свой
      StreamHandler на `aiohttp.access`, когда у event loop включён debug);
    * фильтр НА ХЕНДЛЕРАХ корневого логгера покрывает всё остальное, что
      печатает приложение (`logging.getLogger(__name__)` в app/*): фильтры
      РОДИТЕЛЬСКИХ логгеров к записям дочерних НЕ применяются, а фильтры
      хендлеров — применяются, поэтому вешать только на root-логгер было бы
      бесполезно.

    Идемпотентна: повторный вызов не навешивает второй фильтр, но обновляет
    известное фильтру значение секрета (важно для тестов и для случая, когда
    приложение пересобирают в одном процессе).
    """
    existing = _installed_filter()
    if existing is not None:
        if secret:
            existing.secret = secret
        _attach(existing, logger_names)
        return existing

    filt = SecretPathFilter(secret)
    _attach(filt, logger_names)
    return filt


def _attach(filt: SecretPathFilter, logger_names: Iterable[str]) -> None:
    for name in logger_names:
        logger = logging.getLogger(name)
        if filt not in logger.filters:
            logger.addFilter(filt)
    root = logging.getLogger()
    if filt not in root.filters:
        root.addFilter(filt)
    for handler in root.handlers:
        if filt not in handler.filters:
            handler.addFilter(filt)


def _installed_filter() -> Optional[SecretPathFilter]:
    for f in logging.getLogger().filters:
        if isinstance(f, SecretPathFilter):
            return f
    return None


def uninstall() -> None:
    """Снять фильтр отовсюду (нужен тестам, чтобы не тащить его между ними)."""
    targets = [logging.getLogger(n) for n in _AIOHTTP_LOGGERS]
    root = logging.getLogger()
    targets.append(root)
    for logger in targets:
        for f in list(logger.filters):
            if isinstance(f, SecretPathFilter):
                logger.removeFilter(f)
        for handler in list(logger.handlers):
            for f in list(handler.filters):
                if isinstance(f, SecretPathFilter):
                    handler.removeFilter(f)
