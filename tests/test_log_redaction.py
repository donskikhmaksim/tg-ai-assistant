"""#121: секреты, которые едут в самом URL, не должны попадать в логи.

Проверяем то, что видно СНАРУЖИ: тесты поднимают НАСТОЯЩИЙ web-сервер бота
(`server_mod.build_app` под aiohttp TestServer/TestClient), делают по нему
настоящие HTTP-запросы и читают реальный текст, который logging напечатал в
поток хендлера. Ни один тест не заглядывает во внутренние переменные фильтра
и не подменяет его обёрткой — иначе он доказывал бы только сам себя.

Фильтр в этих тестах НИКТО не ставит руками: его обязан поставить сам
`build_app()`. Если он этого не делает — тесты краснеют.

Секреты здесь ВЫДУМАННЫЕ: боевые значения не должны появляться ни в тестах,
ни в отчётах.
"""
from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import app.mcp_readonly as mcpro
import app.repositories as repo
from app import log_redaction
from app.web import server as server_mod

# Выдуманные значения, похожие по форме на настоящие.
FAKE_MCP_SECRET = "fake0mcp0secret0do0not0use0abcdef12"
FAKE_CHAT_TOKEN = "fa1se2chat3token4beef"  # chat_link_token(): 24 hex-символа
FAKE_BOT_TOKEN = "123456789:FAKE-bot-token-value-0000000"
CHAT_ID = "-1001234567890"


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Инфраструктура: настоящий сервер + настоящий поток вывода логов
# ─────────────────────────────────────────────────────────────────────────────

class _LogCapture:
    """Реальный logging-хендлер на корневом логгере + его поток.

    Именно в этот поток logging печатает готовую строку — то, что в бою
    ушло бы в stdout сервиса (и в логи Railway).
    """

    def __init__(self, fmt: str = "%(message)s", logger_name: str = "") -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter(fmt))
        self.logger = logging.getLogger(logger_name)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        # aiohttp решает, писать ли access-строку, по isEnabledFor(INFO)
        # своего логгера — в момент создания обработчика соединения.
        logging.getLogger("aiohttp.access").setLevel(logging.INFO)

    @property
    def text(self) -> str:
        self.handler.flush()
        return self.stream.getvalue()

    def close(self) -> None:
        self.logger.removeHandler(self.handler)


def _settings(**overrides):
    base = dict(
        bot_token=FAKE_BOT_TOKEN,
        policy_pull_token="",
        mcp_readonly_secret=FAKE_MCP_SECRET,
        default_timezone="UTC",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build_app(monkeypatch, settings=None):
    """Настоящее приложение бота.

    Никакого log_redaction.install() здесь нет намеренно: фильтр должен
    поставить сам build_app().
    """
    st = settings or _settings()
    monkeypatch.setattr(server_mod, "get_settings", lambda: st)
    monkeypatch.setattr(mcpro, "get_settings", lambda: st)

    async def fake_get_bot_state(key):
        return None

    monkeypatch.setattr(repo, "get_bot_state", fake_get_bot_state)
    return server_mod.build_app(bot=SimpleNamespace())


def _client(monkeypatch, settings=None) -> TestClient:
    """То же приложение на настоящем сокете (127.0.0.1, свободный порт)."""
    return TestClient(TestServer(_build_app(monkeypatch, settings)))


def _restore_logging():
    log_redaction.uninstall()
    for name in ("aiohttp.access", "aiohttp.server", "aiohttp.web"):
        logger = logging.getLogger(name)
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
        logger.handlers[:] = []


@pytest.fixture(autouse=True)
def _clean_logging():
    """Не тащить фильтр и настройки логгеров между тестами."""
    yield
    _restore_logging()


# ─────────────────────────────────────────────────────────────────────────────
# Основное: access-лог настоящего сервера
# ─────────────────────────────────────────────────────────────────────────────

def test_access_log_hides_mcp_secret_but_keeps_everything_debuggable(monkeypatch):
    """`/mcp/<MCP_READONLY_SECRET>` — пропуск ко всей переписке владельца."""
    cap = _LogCapture()
    status = {}

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.post(f"/mcp/{FAKE_MCP_SECRET}", json={"ping": 1})
            status["code"] = resp.status

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert printed.strip(), "access-лог вообще ничего не напечатал — тест бесполезен"
    assert FAKE_MCP_SECRET not in printed
    assert "/mcp/<mcp-secret>" in printed
    # Отладка по логам обязана остаться возможной.
    assert "127.0.0.1" in printed          # адрес клиента
    assert '"POST /mcp/' in printed         # метод и путь
    assert "HTTP/1.1" in printed            # версия протокола
    assert str(status["code"]) in printed   # код ответа


def test_access_log_hides_any_value_in_the_mcp_path_even_an_unknown_one(monkeypatch):
    """Маскировка не должна зависеть от того, знает ли фильтр значение.

    Реальный сценарий: секрет ротировали в переменных окружения, а старый
    клиент ещё какое-то время долбится СТАРЫМ значением — оно тоже секрет и
    тоже не должно лечь в лог (запрос при этом честно получает 404).
    """
    stale = "stale0rotated0secret0abcdef987654"
    cap = _LogCapture()

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.post(f"/mcp/{stale}", json={"ping": 1})
            assert resp.status == 404

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert stale not in printed
    assert '"POST /mcp/<mcp-secret> HTTP/1.1" 404' in printed


def test_access_log_hides_chat_link_token_but_keeps_the_chat_id(monkeypatch):
    """`/chat?c=…&t=…`: `t` — вечный HMAC-пропуск к истории чата, `c` —
    обычный id чата, он для отладки нужен и остаётся."""
    cap = _LogCapture()

    async def go():
        async with _client(monkeypatch) as client:
            await client.get(f"/chat?c={CHAT_ID}&t={FAKE_CHAT_TOKEN}")

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_CHAT_TOKEN not in printed
    assert "t=<chat-token>" in printed
    assert f"c={CHAT_ID}" in printed
    assert "GET /chat" in printed


def test_chat_token_is_redacted_even_when_the_mcp_server_is_off(monkeypatch):
    """У большинства деплоев MCP_READONLY_SECRET не задан — read-only MCP
    выключен целиком. Ссылка на транскрипт при этом работает, и её токен
    обязан маскироваться всё равно: фильтр ставится самим build_app(), а не
    только по дороге монтирования MCP.
    """
    cap = _LogCapture()

    async def go():
        async with _client(monkeypatch, settings=_settings(mcp_readonly_secret="")) as client:
            await client.get(f"/chat?c={CHAT_ID}&t={FAKE_CHAT_TOKEN}")

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_CHAT_TOKEN not in printed
    assert "t=<chat-token>" in printed


def test_referer_header_carrying_the_chat_token_is_redacted(monkeypatch):
    """Второй путь той же утечки: формат access-лога у aiohttp по умолчанию
    печатает `%{Referer}i`, а браузер шлёт в Referer полный адрес страницы,
    с которой ушёл запрос, — то есть ссылку на транскрипт вместе с токеном.
    """
    cap = _LogCapture()
    referer = f"https://example.up.railway.app/chat?c={CHAT_ID}&t={FAKE_CHAT_TOKEN}"

    async def go():
        async with _client(monkeypatch) as client:
            await client.get("/health", headers={"Referer": referer})

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_CHAT_TOKEN not in printed
    assert "t=<chat-token>" in printed


def test_ordinary_request_is_logged_in_full(monkeypatch):
    """Фильтр не должен глотать или калечить полезное."""
    cap = _LogCapture()

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/health")
            assert resp.status == 200

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert '"GET /health HTTP/1.1" 200' in printed
    assert "Python" in printed  # User-Agent клиента на месте


def test_structured_field_first_request_line_is_redacted_too(monkeypatch):
    """Путь приезжает не только в тексте сообщения.

    aiohttp кладёт его ещё и в `extra` (AccessLogger.log → extra=…), откуда
    его печатает любой форматтер вида %(first_request_line)s и любой
    JSON-хендлер. Правка одного лишь текста оставила бы здесь дыру.
    """
    cap = _LogCapture(fmt="line=%(first_request_line)s")

    async def go():
        async with _client(monkeypatch) as client:
            await client.post(f"/mcp/{FAKE_MCP_SECRET}", json={"ping": 1})

    try:
        _run(go())
    finally:
        cap.close()

    printed = cap.text
    assert "line=POST /mcp/" in printed, printed
    assert FAKE_MCP_SECRET not in printed
    assert "<mcp-secret>" in printed


def test_redaction_works_when_access_logger_has_its_own_handler(monkeypatch):
    """Хендлер повешен ПРЯМО на `aiohttp.access`, propagate=False.

    Так делает сам aiohttp, когда у event loop включён debug. Корневые
    хендлеры такие записи не видят вовсе — маскировка обязана происходить
    в самом логгере-источнике.
    """
    access = logging.getLogger("aiohttp.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.addHandler(handler)
    access.propagate = False
    access.setLevel(logging.INFO)

    async def go():
        async with _client(monkeypatch) as client:
            await client.post(f"/mcp/{FAKE_MCP_SECRET}", json={"ping": 1})

    try:
        _run(go())
    finally:
        access.removeHandler(handler)

    printed = stream.getvalue()
    assert printed.strip(), "собственный хендлер логгера ничего не напечатал"
    assert FAKE_MCP_SECRET not in printed
    assert "/mcp/<mcp-secret>" in printed


def test_application_log_line_with_secret_url_is_redacted(monkeypatch):
    """Сообщения самого приложения (app.*) идут через КОРНЕВЫЕ хендлеры.

    Фильтры родительских логгеров к записям дочерних не применяются, поэтому
    фильтр обязан висеть именно на хендлерах корневого логгера — иначе такая
    строка уедет в лог с секретом.
    """
    cap = _LogCapture()
    try:
        _build_app(monkeypatch)  # build_app() ставит фильтр
        logging.getLogger("app.web.server").info(
            "read-only MCP mounted at https://example.up.railway.app/mcp/%s",
            FAKE_MCP_SECRET,
        )
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_MCP_SECRET not in printed
    assert "read-only MCP mounted at https://example.up.railway.app/mcp/<mcp-secret>" in printed


def test_server_tells_the_filter_the_actual_secret_value(monkeypatch):
    """Позиционных правил хватает только там, где секрет стоит в ожидаемом
    месте пути. Поднятый сервер обязан ещё и СООБЩИТЬ фильтру само значение —
    тогда оно вырезается, где бы ни всплыло (query, диагностика, traceback).
    Проверяем поведением: печатаем строку, которую позиционные правила не
    ловят, и смотрим на напечатанное.
    """
    cap = _LogCapture()
    try:
        _build_app(monkeypatch)  # ставит фильтр И сообщает ему секрет
        logging.getLogger("app.mcp_readonly").warning(
            "probe failed: https://example.up.railway.app/probe?key=%s", FAKE_MCP_SECRET
        )
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_MCP_SECRET not in printed
    assert "/probe?key=<mcp-secret>" in printed


def test_secret_arriving_as_a_log_argument_is_redacted(monkeypatch):
    """Так пишет watchdog: `logger.error("Watchdog: %s", detail)`, где detail
    собран из текста ошибки и несёт полный адрес с секретом. Секрет здесь —
    в аргументе, а не в шаблоне; подстановка обязана остаться рабочей."""
    foreign_secret = "someone0elses0mcp0secret0777"
    cap = _LogCapture()
    try:
        _build_app(monkeypatch)
        logging.getLogger("app.pipeline.watchdog").error(
            "Watchdog: %s", f"ticktick: 404 for https://tt.up.railway.app/mcp/{foreign_secret}"
        )
    finally:
        cap.close()

    printed = cap.text
    assert foreign_secret not in printed
    assert "Watchdog: ticktick: 404 for https://tt.up.railway.app/mcp/<mcp-secret>" in printed


def test_exception_text_with_ticktick_mcp_url_is_redacted(monkeypatch):
    """Главный канал утечки СОСЕДНЕГО секрета: `logger.exception(...)`.

    У `TICKTICK_MCP_URL` секрет тоже лежит в пути, а httpx кладёт ПОЛНЫЙ
    адрес в текст ошибки — и `logger.exception()` печатает его вместе с
    трейсбеком (audit-поллер каждые 5 минут, watchdog, /connect, Mini App).
    Фильтру это значение неизвестно, спасает только позиционное правило.
    """
    foreign_secret = "someone0elses0mcp0secret0999"
    cap = _LogCapture(fmt="%(levelname)s %(message)s")
    try:
        _build_app(monkeypatch)
        try:
            raise RuntimeError(
                "Client error '404 Not Found' for url "
                f"'https://ticktick-mcp.up.railway.app/mcp/{foreign_secret}'"
            )
        except RuntimeError:
            logging.getLogger("app.audit.poller").exception("ticktick audit poll failed")
    finally:
        cap.close()

    printed = cap.text
    assert foreign_secret not in printed
    assert "/mcp/<mcp-secret>" in printed
    # Диагностика на месте: и своё сообщение, и тип ошибки, и трейсбек.
    assert "ticktick audit poll failed" in printed
    assert "RuntimeError" in printed
    assert "404 Not Found" in printed
    assert "Traceback (most recent call last)" in printed


def test_exception_text_with_telegram_file_url_hides_the_bot_token(monkeypatch):
    """Второй такой же канал: скачивание голосового идёт по
    `https://api.telegram.org/file/bot<BOT_TOKEN>/…`, а aiohttp кладёт полный
    URL в текст ClientResponseError — и handlers_messages ловит это через
    `logger.exception("Failed to download media…")`.
    """
    cap = _LogCapture()
    try:
        _build_app(monkeypatch)
        try:
            raise RuntimeError(
                "404, message='Not Found', url='https://api.telegram.org/file/"
                f"bot{FAKE_BOT_TOKEN}/voice/file_42.oga'"
            )
        except RuntimeError:
            logging.getLogger("app.telegram.handlers_messages").exception(
                "Failed to download media for transcription"
            )
    finally:
        cap.close()

    printed = cap.text
    assert FAKE_BOT_TOKEN not in printed
    assert "bot<bot-token>" in printed
    assert "voice/file_42.oga" in printed
    assert "Failed to download media for transcription" in printed


# ─────────────────────────────────────────────────────────────────────────────
# Юнит-уровень: сама функция redact()
# ─────────────────────────────────────────────────────────────────────────────

def test_plain_mcp_path_without_secret_is_untouched():
    assert log_redaction.redact('"POST /mcp HTTP/1.1" 200') == '"POST /mcp HTTP/1.1" 200'


def test_redact_is_idempotent():
    once = log_redaction.redact(f"/mcp/{FAKE_MCP_SECRET}", FAKE_MCP_SECRET)
    assert log_redaction.redact(once, FAKE_MCP_SECRET) == once


def test_short_secret_is_not_blindly_stripped_from_text():
    """Короткая строка не вырезается по всему тексту (иначе фильтр испортил
    бы осмысленные сообщения), но позиционно в пути — маскируется."""
    assert log_redaction.redact("task ok, project ok", "ok") == "task ok, project ok"
    assert log_redaction.redact("/mcp/ok", "ok") == "/mcp/<mcp-secret>"


def test_known_secret_is_redacted_even_outside_the_mcp_path():
    """Позиционное правило ловит только `/mcp/<…>`. Само значение секрета
    может засветиться и в другом виде — в query, в диагностике, в traceback;
    известное фильтру значение вырезается везде."""
    printed = log_redaction.redact(
        f"probe failed: https://example.up.railway.app/probe?key={FAKE_MCP_SECRET}",
        FAKE_MCP_SECRET,
    )
    assert FAKE_MCP_SECRET not in printed
    assert "probe failed: https://example.up.railway.app/probe?key=<mcp-secret>" == printed


def test_percent_encoded_secret_is_redacted():
    tricky = "fake secret/with+chars"
    printed = log_redaction.redact("/mcp/fake%20secret%2Fwith%2Bchars", tricky)
    assert "fake%20secret" not in printed
    assert "<mcp-secret>" in printed


def test_telegram_bot_token_in_url_is_redacted():
    """Файлы (голосовые) качаются по URL вида /file/bot<BOT_TOKEN>/… — если
    такой URL попадёт в лог или в traceback, там будет токен бота."""
    printed = log_redaction.redact(
        f"download failed: https://api.telegram.org/file/bot{FAKE_BOT_TOKEN}/voice/file_1.oga"
    )
    assert FAKE_BOT_TOKEN not in printed
    assert "bot<bot-token>" in printed
    assert "voice/file_1.oga" in printed  # что качали — видно


def test_ordinary_text_is_not_mangled():
    text = "Batch finished: 12 tasks, 3 skipped (t=0.42s), /health ok"
    assert log_redaction.redact(text, FAKE_MCP_SECRET) == text
