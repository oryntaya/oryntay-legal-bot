"""Системный промпт бота. Сам текст лежит в system_prompt.md, здесь только загрузка.

Файл можно заменить своим через SYSTEM_PROMPT_FILE в .env (например, приватный промпт вне репозитория).
Репозиторий публичный, поэтому текст промпта секретом не считается. От утечки защищают проверки
в guard.py: случайная метка CANARY (создаётся при каждом запуске и нигде не хранится) и поиск
дословных фрагментов промпта в ответе модели.
"""
import os
import re
import secrets

PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                              "system_prompt.md")
EXAMPLES_MARKER = "\n# ПРИМЕРЫ"  # с этого заголовка в файле начинаются few-shot примеры

CANARY = "ORY-" + secrets.token_hex(6)

# Ответ на попытки выманить инструкции или сменить роль бота (это реплика бота, а не часть промпта)
REFUSAL = (
    "Это внутренняя информация, её я раскрыть не могу. "
    "Давайте вернёмся к вашему вопросу: опишите ситуацию, и я постараюсь помочь."
)


def _load(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # HTML-комментарии в файле — заметки для людей
    return text.lstrip("\n")


_TEXT = _load(PROMPT_FILE)
if "{CANARY}" not in _TEXT:
    raise SystemExit("В %s нет плейсхолдера {CANARY}: без него бот не заметит утечку промпта" % PROMPT_FILE)

_cut = _TEXT.find(EXAMPLES_MARKER)
_RULES = _TEXT if _cut == -1 else _TEXT[:_cut]
EXAMPLES = "" if _cut == -1 else _TEXT[_cut:]

# Правила без примеров. По ним guard.py ищет утечку: примеры модель вправе повторять почти дословно,
# и их совпадения не должны считаться утечкой.
RULES_PROMPT = _RULES.replace("{CANARY}", CANARY)


def build(with_examples: bool = True) -> str:
    """Полный системный промпт. Без примеров нужен только для сравнения «до и после» в test_cases.md."""
    return RULES_PROMPT + (EXAMPLES if with_examples else "")


SYSTEM_PROMPT = build(True)

# Напоминание правил: короткий список, который llm.py добавляет в конец каждого запроса.
# STRICT_REMINDER=0 отключает его.
REMINDER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminder.md")
REMINDER = ""
if os.getenv("STRICT_REMINDER", "1") != "0" and os.path.exists(REMINDER_FILE):
    REMINDER = _load(REMINDER_FILE).strip()
