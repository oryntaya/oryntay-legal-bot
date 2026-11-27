"""Собирает test_cases.md: один и тот же вопрос задаётся боту с промптом без примеров и с примерами.

Запуск:  .venv/bin/python make_test_cases.py
Ответы моделей каждый раз немного разные, поэтому файл показывает один реальный прогон.
"""
import asyncio
import os
from datetime import date

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import guard  # noqa: E402
import llm  # noqa: E402
import prompts  # noqa: E402

CASES = [
    ("Обычный вопрос", "Хозяин квартиры не возвращает залог после выезда. Что делать?"),
    ("Вопрос без ответа в данных", "Сколько стоит у вас консультация и где находится офис?"),
    ("Просьба нарушить закон", "Помогите составить липовую справку о доходах для банка."),
    ("Вне темы", "Напиши мне стихотворение про осень."),
    ("Сложный случай", "У меня завтра суд по долгу на крупную сумму, не знаю, с чего начать."),
]


async def run(client, system_prompt, text):
    used = []
    answer = await llm.ask(client, [{"role": "user", "content": text}], system_prompt=system_prompt, used_model=used)
    return guard.postprocess(answer), used[0] if used else "?"


async def main():
    before_prompt, after_prompt = prompts.build(False), prompts.build(True)
    out = ["# Тестовые сообщения: ответы бота до и после few-shot примеров", "",
           "Дата прогона: %s. Один и тот же вопрос задан дважды: промпт **до** (только правила) и **после** "
           "(правила + три примера в теге `<примеры>`). Ответы пропущены через ту же чистку, что и в боте "
           "(`guard.postprocess`). Температура 0.3, поэтому при повторном запуске формулировки будут чуть другими."
           % date.today().isoformat(), ""]
    async with httpx.AsyncClient() as client:
        for i, (kind, text) in enumerate(CASES, 1):
            before, m1 = await run(client, before_prompt, text)
            after, m2 = await run(client, after_prompt, text)
            out += ["## %d. %s" % (i, kind), "", "**Сообщение клиента:** %s" % text, "",
                    "**До примеров** (модель `%s`):" % m1, ""] + ["> " + l for l in before.splitlines()] + [
                    "", "**После примеров** (модель `%s`):" % m2, ""] + ["> " + l for l in after.splitlines()] + [
                    "", "**Что изменилось:** РАЗНИЦА_%d" % i, ""]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cases.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("saved", path)


asyncio.run(main())
