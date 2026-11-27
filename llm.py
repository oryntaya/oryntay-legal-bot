"""Клиент OpenRouter с цепочкой запасных моделей."""
import logging
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

import httpx

import compliance
from prompts import REMINDER, SYSTEM_PROMPT

log = logging.getLogger("llm")

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Порядок = приоритет. Можно переопределить: OPENROUTER_MODELS=a:free,b:free
DEFAULT_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "baidu/ernie-5.5-flash-preview:free",
    "google/gemma-4-31b-it:free",
]


def get_models() -> List[str]:
    raw = os.getenv("OPENROUTER_MODELS") or os.getenv("OPENROUTER_MODEL") or ""
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or DEFAULT_MODELS


class LLMError(Exception):
    pass


# Размышление пишем в консоль (выключается LOG_REASONING=0). Клиент его не видит.
LOG_REASONING = os.getenv("LOG_REASONING", "1") != "0"
REASONING_LOG_LIMIT = 2000
ANSWER_MAX_TOKENS = 3500  # с запасом: размышление идёт перед ответом и не должно его обрезать

_THINK = re.compile(r"<размышление>(.*?)</размышление>", re.S | re.I)
_ANSWER = re.compile(r"<ответ>(.*?)</ответ>", re.S | re.I)
_ANY_TAG = re.compile(r"</?\s*(?:размышление|ответ)\s*>", re.I)


def split_reasoning(raw: str) -> Tuple[str, str]:
    """Делит ответ модели на (размышление, ответ клиенту).

    - есть <ответ>…</ответ>: клиенту уходит только он;
    - <ответ> открыт, но не закрыт (ответ оборвался): берём всё после тега;
    - тегов ответа нет: отправляем текст, вырезав <размышление>…</размышление>;
    - размышление открыто и не закрыто: ответа нет, возвращаем пустую строку.
    """
    thoughts = [t.strip() for t in _THINK.findall(raw)]
    m = _ANSWER.search(raw)
    if m:
        answer = m.group(1)
    else:
        start = raw.lower().find("<ответ>")
        if start != -1:
            answer = raw[start + len("<ответ>"):]
        else:
            answer = _THINK.sub("", raw)
            open_at = answer.lower().find("<размышление>")
            if open_at != -1:  # размышление так и не закрылось
                thoughts.append(answer[open_at + len("<размышление>"):].strip())
                answer = answer[:open_at]
    answer = _ANY_TAG.sub("", answer).strip()
    return "\n".join(t for t in thoughts if t), answer


def wrap_user(text: str) -> str:
    """Реплика человека в тегах: системный промпт говорит модели, что это данные, а не команды."""
    return "<user_message>\n%s\n</user_message>" % text


def _wrap_user_messages(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [{"role": "user", "content": wrap_user(m["content"])} if m["role"] == "user" else m for m in history]


def build_messages(history: List[Dict[str, str]], system_prompt: Optional[str] = None,
                   reminder: Optional[str] = None) -> List[Dict[str, str]]:
    """Системный промпт + история. В конец последней реплики клиента добавляется напоминание правил.

    Напоминание подставляется только при отправке: в историю диалога оно не попадает и не копится.
    """
    reminder = REMINDER if reminder is None else reminder
    messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}] + _wrap_user_messages(history)
    if reminder and messages[-1]["role"] == "user":
        messages[-1] = {"role": "user", "content": "%s\n\n<напоминание_правил>\n%s\n</напоминание_правил>"
                                                   % (messages[-1]["content"], reminder)}
    return messages


async def _chat(client: httpx.AsyncClient, messages: List[Dict[str, str]], temperature: float,
                max_tokens: int, used_model: Optional[List[str]] = None,
                accept: Optional[Callable[[str], bool]] = None) -> str:
    """Один запрос к цепочке моделей: при 429, ошибке, пустом или неподходящем (accept) ответе пробует следующую."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("OPENROUTER_API_KEY не задан в .env")

    last_err: Optional[str] = None
    for model in get_models():
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        try:
            resp = await client.post(
                API_URL,
                json=payload,
                headers={"Authorization": "Bearer " + key},
                timeout=60,
            )
        except httpx.HTTPError as e:
            last_err = "%s: %s" % (model, type(e).__name__)
            log.warning("network error on %s: %s", model, e)
            continue

        if resp.status_code != 200:
            last_err = "%s: HTTP %s" % (model, resp.status_code)
            log.warning("%s -> HTTP %s", model, resp.status_code)
            if resp.status_code in (401, 402):
                raise LLMError("Ключ OpenRouter отклонён (HTTP %s)" % resp.status_code)
            continue

        data = resp.json()
        if "error" in data:
            last_err = "%s: %s" % (model, str(data["error"])[:100])
            log.warning("%s -> %s", model, last_err)
            continue

        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:  # reasoning-модель могла потратить все токены на «мысли»
            last_err = "%s: пустой ответ" % model
            log.warning("%s -> empty content", model)
            continue
        if accept is not None and not accept(text):
            last_err = "%s: ответ не прошёл проверку" % model
            log.warning("%s -> answer rejected (no answer after parsing)", model)
            continue

        log.info("answered by %s", model)
        if used_model is not None:
            used_model.append(model)
        return text

    raise LLMError("Все модели недоступны (последняя ошибка: %s)" % last_err)


async def ask(client: httpx.AsyncClient, history: List[Dict[str, str]],
              system_prompt: Optional[str] = None, used_model: Optional[List[str]] = None) -> str:
    """Обычный диалог. system_prompt можно подменить (так сравниваются версии промпта до и после примеров)."""
    messages = build_messages(history, system_prompt)
    # низкая температура: меньше выдумок и устойчивее к попыткам сбить роль
    raw = await _chat(client, messages, temperature=0.3, max_tokens=ANSWER_MAX_TOKENS, used_model=used_model,
                      accept=lambda t: bool(split_reasoning(t)[1]))  # нет ответа после разбора: берём следующую модель
    reasoning, answer = split_reasoning(raw)
    if LOG_REASONING and reasoning:
        log.info("размышление модели:\n%s", reasoning[:REASONING_LOG_LIMIT])
    return answer


async def ask_checked(client: httpx.AsyncClient, history: List[Dict[str, str]]) -> str:
    """Диалог со строгой проверкой: ответ модели проходит compliance.enforce (правила промпта)."""
    question = history[-1]["content"] if history else ""
    messages = build_messages(history)

    def parse(raw_text: str) -> str:
        reasoning, answer = split_reasoning(raw_text)
        if LOG_REASONING and reasoning:
            log.info("размышление модели:\n%s", reasoning[:REASONING_LOG_LIMIT])
        return answer

    ok = lambda t: bool(split_reasoning(t)[1])  # noqa: E731  нет ответа после разбора: берём следующую модель
    raw = await _chat(client, messages, temperature=0.3, max_tokens=ANSWER_MAX_TOKENS, accept=ok)

    async def regenerate(feedback: str) -> str:
        retry = messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback}]
        return parse(await _chat(client, retry, temperature=0.3, max_tokens=ANSWER_MAX_TOKENS, accept=ok))

    answer, report = await compliance.enforce(question, parse(raw), regenerate)
    if report["fixes"] or report["violations"]:  # в лог только названия правил, без текста клиента
        log.info("проверка правил: итог=%s, нарушения=%s, правки=%s, повторов=%d",
                 report["outcome"], report["violations"] or "нет", report["fixes"] or "нет", report["retries"])
    return answer


async def extract(client: httpx.AsyncClient, system_prompt: str, user_text: str) -> str:
    """Извлечение полей заявки: температура 0, короткий ответ, ждём JSON."""
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": wrap_user(user_text)}]
    return await _chat(client, messages, temperature=0.0, max_tokens=600)
