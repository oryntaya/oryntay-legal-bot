"""Защита бота: очистка ввода, скрытие персональных данных, антиинъекции, лимиты."""
import os
import re
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple

import prompts

MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "2000"))

# ---------- очистка ввода ----------

# Невидимые символы: нулевой ширины, управление направлением текста, «мягкий» перенос,
# а также Unicode-теги U+E0000..E007F (ими прячут команды от глаз человека).
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­\U000e0000-\U000e007f]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Служебные теги, которыми код размечает сообщение клиента и напоминание правил. Клиент подделать их не должен.
_USER_TAG = re.compile(r"<\s*/?\s*(?:user_message|напоминание_правил)\s*>", re.I)


def clean(text: str, limit: int = MAX_INPUT_CHARS) -> str:
    """Убирает невидимые и управляющие символы и тег-обёртку user_message, режет по длине."""
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = _USER_TAG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


# ---------- персональные данные ----------

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IIN = re.compile(r"(?<!\d)\d{12}(?!\d)")
_SECRET = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _looks_like_iin(s: str) -> bool:
    """ИИН: ГГММДД + цифра века/пола (1-6) + 5 цифр. Отсекаем случайные 12-значные числа."""
    month, day, marker = int(s[2:4]), int(s[4:6]), int(s[6])
    return 1 <= month <= 12 and 1 <= day <= 31 and 1 <= marker <= 6


def redact(text: str) -> Tuple[str, List[str]]:
    """Заменяет номера карт, ИИН и длинные ключи заглушками. Возвращает (текст, что нашли)."""
    found = []  # type: List[str]

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            if "номер карты" not in found:
                found.append("номер карты")
            return "[номер карты]"
        return m.group(0)

    def iin(m):
        if _looks_like_iin(m.group(0)):
            if "ИИН" not in found:
                found.append("ИИН")
            return "[ИИН]"
        return m.group(0)

    def secret(m):
        if "ключ или пароль" not in found:
            found.append("ключ или пароль")
        return "[секрет]"

    text = _CARD.sub(card, text)
    text = _IIN.sub(iin, text)
    text = _SECRET.sub(secret, text)
    return text, found


# ---------- попытки взлома промпта ----------

# Только точные шаблоны, чтобы не ловить обычные юридические вопросы
# (например, «дай инструкцию, как подать иск»). Остальное ловит сам промпт модели.
_INJECTION = [re.compile(p, re.I) for p in [
    r"игнорир\w*\s+(все\s+)?(предыдущ|прошл|прежн|эти|свои|ваши|любые|все)\w*\s+(инструкц|правил|указани|команд|ограничени)",
    r"забуд\w*\s+(все\s+)?(предыдущ|прошл|прежн|свои|эти|ваши)?\s*(инструкц|правил|указани)",
    r"(покажи|выведи|напиши|повтори|раскрой|скинь|пришли|дай|перескажи|переведи|скопируй)\s+(мне\s+)?(весь\s+|полностью\s+|дословно\s+)?"
    r"(свой|свои|твой|твои|ваш|ваши|системн\w+|исходн\w+|скрыт\w+|начальн\w+)\s+(промпт|промт|инструкци|правил|настройк)",
    r"(системн\w+|скрыт\w+|исходн\w+)\s+(промпт|промт|инструкци)",
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions?|rules?|prompts?)",
    r"(reveal|show|print|repeat|output|leak)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions?)",
    r"system\s*prompt",
    r"developer\s+mode|debug\s+mode|режим\s+(разработчика|отладки|бога)",
    r"\bDAN\b|jailbreak|джейлбрейк",
]]


def looks_like_injection(text: str) -> bool:
    norm = text.lower().replace("ё", "е")
    return any(p.search(norm) for p in _INJECTION)


# ---------- утечка системного промпта в ответе ----------

def _words(s: str) -> List[str]:
    return re.findall(r"\w+", s.lower().replace("ё", "е"))


# Фраза отказа сама входит в промпт и часто встречается в ответах, в сравнении её не учитываем
_PROMPT_WORDS = _words(prompts.RULES_PROMPT.replace("Это внутренняя информация, её я раскрыть не могу", ""))
_SHINGLES = {" ".join(_PROMPT_WORDS[i:i + 6]) for i in range(len(_PROMPT_WORDS) - 5)}
LEAK_THRESHOLD = 8  # столько совпавших 6-словных фрагментов считаем утечкой


def prompt_overlap(answer: str) -> int:
    w = _words(answer)
    return sum(1 for i in range(len(w) - 5) if " ".join(w[i:i + 6]) in _SHINGLES)


def leaks_prompt(answer: str) -> bool:
    if prompts.CANARY.lower() in answer.lower():
        return True
    return prompt_overlap(answer) >= LEAK_THRESHOLD


# ---------- чистка ответа модели ----------

_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
# Скобки, в которых названа статья: «(ст. 106, 113 УК РК)» — вырезаем целиком
_ARTICLE_PAREN = re.compile(r"\s*\([^()]*?\b(?:ст\.|статья|статьи|статей|пункт|п\.)\s*\d[^()]*\)", re.I)


def postprocess(answer: str) -> str:
    """Telegram показывает markdown как есть, а номера статей модель путает: убираем и то и другое."""
    answer = _BOLD.sub(r"\2", answer)
    answer = _ITALIC.sub(r"\1", answer)
    answer = _HEADING.sub("", answer)
    answer = answer.replace("`", "")
    answer = _ARTICLE_PAREN.sub("", answer)
    return answer.strip()


# ---------- лимиты ----------

class RateLimiter:
    """Скользящее окно: не больше per_min запросов в минуту и per_day в сутки на пользователя."""

    def __init__(self, per_min: int, per_day: int):
        self.per_min, self.per_day = per_min, per_day
        self._hits = defaultdict(deque)  # type: Dict[int, Deque[float]]

    def check(self, user_id: int) -> Tuple[bool, int]:
        """Возвращает (можно ли, через сколько секунд повторить). Учитывает запрос, если можно."""
        now = time.time()
        q = self._hits[user_id]
        while q and now - q[0] > 86400:
            q.popleft()
        if len(q) >= self.per_day:
            return False, int(q[0] + 86400 - now) + 1
        recent = [t for t in q if now - t <= 60]
        if len(recent) >= self.per_min:
            return False, int(recent[0] + 60 - now) + 1
        q.append(now)
        return True, 0


class Strikes:
    """Считает попытки взлома; после limit попыток за window секунд молча глушит на mute секунд."""

    def __init__(self, limit: int = 3, window: int = 600, mute: int = 1800):
        self.limit, self.window, self.mute = limit, window, mute
        self._events = defaultdict(list)  # type: Dict[int, List[float]]
        self._until = {}  # type: Dict[int, float]

    def add(self, user_id: int) -> bool:
        """Фиксирует попытку. True, если пользователь только что заглушён."""
        now = time.time()
        ev = [t for t in self._events[user_id] if now - t <= self.window] + [now]
        self._events[user_id] = ev
        if len(ev) >= self.limit:
            self._until[user_id] = now + self.mute
            self._events[user_id] = []
            return True
        return False

    def muted_for(self, user_id: int) -> int:
        left = int(self._until.get(user_id, 0) - time.time())
        return left if left > 0 else 0
