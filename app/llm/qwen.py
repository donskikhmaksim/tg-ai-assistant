"""Tier 1 — локальный Qwen-триаж через Ollama (OpenAI-совместимый API, §8 ТЗ).

Дешёвые «ворота»: есть ли в окне разговора хоть одна задача/договорённость/обещание?
Возвращает строгий JSON {"has_task": bool}. На любой сбой — fail-open (считаем,
что задача возможна), чтобы не потерять полезное окно из-за ошибки триажа.
"""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — фильтр-триаж переписки. Тебе дают фрагмент диалога (русский и/или "
    "английский). Определи, есть ли в нём хотя бы одна ЗАДАЧА, ДОГОВОРЁННОСТЬ, "
    "ОБЕЩАНИЕ или ПРОСЬБА что-то сделать — со стороны любого из участников, "
    "включая владельца аккаунта. Болтовня, приветствия, эмоции, факты без "
    "действия — это НЕ задача.\n"
    'Ответь СТРОГО одним JSON-объектом: {"has_task": true} или '
    '{"has_task": false}. Без пояснений.'
)


class QwenTriage:
    def __init__(self, base_url: str, model: str, api_key: str = "ollama") -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        self._model = model

    async def has_task(self, window_text: str) -> bool:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": window_text},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            data = json.loads(content)
            return bool(data.get("has_task", False))
        except json.JSONDecodeError:
            log.warning("Qwen вернул не-JSON, fail-open")
            return True
        except Exception:  # noqa: BLE001
            log.exception("Ошибка Qwen-триажа, fail-open")
            return True
