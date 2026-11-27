"""Строгая проверка ответов модели на соблюдение правил системного промпта.

Модель правила нарушает: пишет «ты», называет статьи, выдумывает цены, обещает исход дела.
Поэтому после ответа запускается цепочка, где каждый следующий шаг включается, только если
предыдущий не помог (так мы экономим дневную квоту запросов):

1. Автоисправления без модели: markdown, статьи в скобках, строка про 112, оговорка в конце.
2. Проверка по списку правил RULES.
3. Один повторный запрос к модели со списком нарушений (единственный шаг, который тратит запрос).
4. Вырезание предложений, которые всё ещё нарушают правила.
5. Безопасный запасной ответ, если после вырезания ничего не осталось.
"""
import logging
import os
import re
from typing import Awaitable, Callable, Dict, List, NamedTuple, Optional, Tuple

import guard

log = logging.getLogger("compliance")

MAX_RETRIES = int(os.getenv("COMPLIANCE_RETRIES", "1"))  # 0 = не перезапрашивать модель
MAX_ANSWER_CHARS = 2000

DISCLAIMER = "Это общая информация, а не индивидуальная консультация."
EMERGENCY_LINE = "Если вам или близким угрожает опасность, немедленно звоните 112 (в полицию можно и по 102)."
FALLBACK = ("Подробный ответ на этот вопрос лучше получить у юриста компании. "
            "Нажмите «📅 Записаться к юристу» и выберите удобное время.")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


# ---------- что спросил клиент ----------

_COMPANY_Q = re.compile(
    r"сколько\s+(?:у\s+вас\s+)?сто|стоимост|\bцен[аыуе]\b|прайс|тариф|расценк|адрес|"
    r"где\s+(?:вы\s+)?(?:наход|располож)|\bофис|телефон|как\s+(?:вас\s+|тебя\s+)?зовут|"
    r"имя\s+юриста|кто\s+(?:у\s+вас\s+)?(?:юрист|адвокат)|график|часы\s+работы|режим\s+работы|"
    r"во\s+сколько|когда\s+(?:вы\s+)?работа")
_DANGER = re.compile(r"убь|убий|бьет|избива|избил|угрожа|угроза\s+жизни|насили|душит|зарежу|изнасил")


def asks_company_facts(question: str) -> bool:
    """Клиент спрашивает то, чего у бота нет в данных: цены, адрес, график, имена юристов."""
    return bool(_COMPANY_Q.search(_norm(question)))


def in_danger(question: str) -> bool:
    return bool(_DANGER.search(_norm(question)))


# ---------- правила ----------

class Rule(NamedTuple):
    name: str
    label: str      # что нарушено, по-человечески (для лога и для запроса к модели)
    hint: str       # как исправить (для запроса к модели)
    pattern: "re.Pattern"
    only_if: Optional[Callable[[str], bool]] = None  # правило действует только при таком вопросе


def _r(p: str, flags: int = re.I) -> "re.Pattern":
    return re.compile(p, flags)


RULES = [
    Rule("articles", "номера статей и пунктов", "назови закон по названию, без номеров статей",
         _r(r"\b(?:ст\.|статья|статьи|статье|статью|статей|п\.|пункт\w*)\s*\d")),
    Rule("ty_form", "обращение на «ты»", "обращайся к клиенту на «вы»",
         _r(r"\b(?:ты|тебе|тебя|тобой|твой|твоя|твоё|твое|твои|твоих|твоей|твоего|твоему)\b"
            r"|\b(?:хочешь|можешь|знаешь|скажи|уточни|напиши|нажми|проверь|сохрани|обратись|сделай|приложи|"
            r"позвони|подай|подготовь|собери|запиши|отправь|посмотри|укажи|расскажи|опиши|помни|учти|"
            r"подумай|смотри|попробуй|найди|возьми|потребуй|обрати)\b")),
    Rule("outcome", "обещание исхода дела", "не обещай результат и не утверждай, что суды обычно решают в чью-то пользу",
         _r(r"(?<!не\s)гарантирую|гарантируем\b|гарантированно|точно\s+(?:выиграете|получите|вернут|вернёт|выиграем)"
            r"|обязательно\s+(?:выиграете|получите|вернут)"
            r"|судебная\s+практика[^.!?]{0,100}(?:в\s+пользу|на\s+сторон)"
            r"|суды?\s+(?:обычно|как\s+правило|часто)\s+[^.!?]{0,60}(?:в\s+пользу|на\s+сторон)")),
    Rule("promise_contact", "обещание, что с клиентом свяжутся", "не обещай звонков и связи от компании",
         _r(r"с\s+вами\s+(?:свяжутся|свяжется|свяжемся)|свяжемся\s+с\s+вами"
            r"|\b(?:мы|юрист|сотрудник|менеджер|администратор)\s+(?:вам\s+|вас\s+)?(?:перезвон|позвон|свяж)\w+")),
    Rule("claims_human", "выдаёт себя за юриста", "ты ИИ-помощник, а не юрист",
         _r(r"\bя\s*[—–-]?\s*(?:практикующий\s+|ваш\s+|опытный\s+)?(?:юрист|адвокат)\b")),
    Rule("claims_booked", "говорит, что сам оформил запись", "запись оформляет только кнопка, не говори, что записал клиента",
         _r(r"\bя\s+(?:вас\s+|вам\s+)?запис(?:ал|ала|ываю)\b|\bзапись\s+оформлена|\bвы\s+(?:успешно\s+)?записаны"
            r"|\bзаписал(?:а)?\s+вас")),
    Rule("asks_secrets", "просит ИИН, паспорт или карту", "не проси ИИН, паспортные данные, номера карт и пароли",
         _r(r"\b(?:пришлите|отправьте|сообщите|укажите|назовите|напишите|введите|продиктуйте)\b[^.?!\n]{0,25}"
            r"(?:мне|нам|сюда|в\s+чат|боту)[^.?!\n]{0,30}\b(?:иин|паспорт\w*|номер\w*\s+карт\w*|пароль|cvv|cvc|пин-?код)\b")),
    Rule("service_tags", "упоминает служебные теги", "не упоминай служебные теги и структуру ответа",
         _r(r"user_message|напоминание[\s_]правил|</?размышление|</?ответ>")),
    Rule("too_long", "слишком длинный ответ", "сократи ответ до 6-10 строк",
         _r(r"[\s\S]{%d}" % MAX_ANSWER_CHARS)),
    # правила ниже действуют, только когда клиент спросил то, чего у бота нет в данных
    Rule("fake_price", "цена услуг компании", "цен компании ты не знаешь, скажи, что их уточнит сотрудник",
         _r(r"\d[\d\s.,]*\s*(?:тг\b|тенге|₸|руб\w*|\$|usd|долл\w*|евро|€)|[₸$€]\s*\d"), asks_company_facts),
    Rule("fake_address", "адрес офиса", "адреса ты не знаешь, скажи, что его уточнит сотрудник",
         _r(r"\b(?:ул\.|улиц\w+|проспект\w*|пр-т|мкр\b|микрорайон\w*|бизнес-центр\w*|офис\s*№?\s*\d+|по\s+адресу"
            r"|г\.\s*[А-ЯЁ]\w+)"), asks_company_facts),
    Rule("fake_hours", "график работы", "график ты не знаешь, скажи, что его уточнит сотрудник",
         _r(r"\b\d{1,2}[:.]\d{2}\s*(?:до|-|–|—)\s*\d{1,2}[:.]\d{2}|\bпн\s*[-–—]\s*пт|\bпо\s+будням|\bкруглосуточно"
            r"|\bс\s+\d{1,2}(?:[:.]\d{2})?\s+до\s+\d{1,2}(?:[:.]\d{2})?"), asks_company_facts),
    Rule("fake_phone", "телефон компании", "телефонов ты не знаешь, скажи, что их уточнит сотрудник",
         _r(r"(?<!\d)(?:\+?7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"), asks_company_facts),
    Rule("fake_people", "имена юристов", "имён юристов ты не знаешь, скажи, что их уточнит сотрудник",
         _r(r"\b(?i:юрист|адвокат|партн[её]р|специалист)\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+", 0), asks_company_facts),
]

_DISCLAIMER = re.compile(r"общая\s+информация|не\s+заменя\w+|не\s+является\s+индивидуальн|не\s+индивидуальн", re.I)


def check(question: str, answer: str) -> List[Rule]:
    """Список нарушенных правил. Пустой список: ответ соблюдает правила."""
    return [r for r in RULES
            if (r.only_if is None or r.only_if(question)) and r.pattern.search(answer)]


# ---------- исправления без модели ----------

def autofix(question: str, answer: str) -> Tuple[str, List[str]]:
    """Детерминированные правки: markdown, статьи в скобках, строка про 112, оговорка в конце."""
    fixes = []  # type: List[str]
    cleaned = guard.postprocess(answer)
    if cleaned != answer.strip():
        fixes.append("markdown_и_статьи_в_скобках")
    answer = cleaned
    if in_danger(question) and "112" not in answer:
        answer = EMERGENCY_LINE + "\n\n" + answer
        fixes.append("добавлен_номер_112")
    if len(answer) >= 450 and not _DISCLAIMER.search(answer):
        answer = answer + "\n\n" + DISCLAIMER
        fixes.append("добавлена_оговорка")
    return answer, fixes


def drop_sentences(answer: str, rules: List[Rule]) -> str:
    """Вырезает предложения, нарушающие правила. Структура строк и списков сохраняется."""
    lines = []
    for line in answer.split("\n"):
        parts = re.split(r"(?<=[.!?…])\s+", line)
        kept = [p for p in parts if not any(r.pattern.search(p) for r in rules)]
        if kept:
            lines.append(" ".join(kept))
        elif not line.strip():
            lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _truncate(answer: str, limit: int = MAX_ANSWER_CHARS - 200) -> str:
    if len(answer) <= limit:
        return answer
    cut = answer[:limit]
    end = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit // 2 else cut).strip()


def _feedback(violated: List[Rule]) -> str:
    items = "; ".join("%s (%s)" % (r.label, r.hint) for r in violated)
    return ("Проверка правил: твой предыдущий ответ нарушил правила: %s. Перепиши ответ целиком, исправив это. "
            "Формат прежний: сначала <размышление>, затем <ответ>." % items)


# ---------- главный алгоритм ----------

async def enforce(question: str, answer: str,
                  regenerate: Optional[Callable[[str], Awaitable[str]]] = None,
                  max_retries: Optional[int] = None) -> Tuple[str, Dict]:
    """Доводит ответ до соблюдения правил. regenerate(feedback) просит модель переписать ответ.

    Возвращает (ответ, отчёт). outcome в отчёте: clean, fixed, retry_ok, cut, fallback.
    """
    retries = MAX_RETRIES if max_retries is None else max_retries
    report = {"fixes": [], "violations": [], "retries": 0, "outcome": "clean"}  # type: Dict

    answer, fixes = autofix(question, answer)
    report["fixes"] = list(fixes)
    violated = check(question, answer)
    report["violations"] = [r.name for r in violated]
    if fixes:
        report["outcome"] = "fixed"

    while violated and regenerate is not None and report["retries"] < retries:
        report["retries"] += 1
        try:
            new_answer = await regenerate(_feedback(violated))
        except Exception as e:  # квота кончилась, сеть упала: идём дальше без модели
            log.warning("повторный запрос не удался: %s", e)
            break
        if not new_answer:
            break
        answer, fixes = autofix(question, new_answer)
        report["fixes"] += fixes
        violated = check(question, answer)
        if not violated:
            report["outcome"] = "retry_ok"

    if violated:
        cut = _truncate(drop_sentences(answer, violated))
        body = cut.replace(EMERGENCY_LINE, "").strip()  # строка про 112 содержательным ответом не считается
        if len(body) >= 40 and not check(question, cut):
            answer, report["outcome"] = cut, "cut"
        else:
            answer, _ = autofix(question, FALLBACK)
            report["outcome"] = "fallback"
    return answer, report
