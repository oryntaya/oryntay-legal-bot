"""Telegram-бот юридической компании Oryntay & partners."""
import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

import booking
import guard
import intake
import llm
import prompts

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

HISTORY_LIMIT = 20   # сколько последних сообщений помнить на чат
TG_MAX_LEN = 4096    # лимит длины сообщения в Telegram

# Кому приходят уведомления о записях (Telegram ID через запятую). Свой ID: команда /myid
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]

BTN_BOOK = "📅 Записаться к юристу"
BTN_MY = "📋 Моя запись"
BTN_RESET = "🔄 Новый вопрос"
BTN_CANCEL = "Отмена"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_BOOK)], [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_RESET)]],
    resize_keyboard=True,
)
BOOK_INLINE = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BTN_BOOK, callback_data="book")]])

dp = Dispatcher()
# Бот работает только в личных сообщениях: в группах им можно было бы жечь лимиты моделей
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")

histories: Dict[int, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # по одному запросу на чат за раз
http: httpx.AsyncClient  # создаётся в main()

# Защита от флуда и подбора промптов (лимиты настраиваются в .env)
limiter = guard.RateLimiter(int(os.getenv("RATE_PER_MIN", "6")), int(os.getenv("RATE_PER_DAY", "60")))
strikes = guard.Strikes()
_notice_at = {}  # type: Dict[int, float]  # когда пользователю в последний раз говорили про лимит


def should_notice(user_id: int, every: float = 30.0) -> bool:
    """Чтобы не отвечать на каждое сообщение флудера, предупреждаем не чаще раза в 30 секунд."""
    now = time.time()
    if now - _notice_at.get(user_id, 0) < every:
        return False
    _notice_at[user_id] = now
    return True


def fmt_wait(seconds: int) -> str:
    if seconds < 90:
        return "%d сек." % seconds
    if seconds < 5400:
        return "%d мин." % ((seconds + 59) // 60)
    return "%d ч." % ((seconds + 3599) // 3600)


class BookForm(StatesGroup):
    name = State()
    phone = State()
    topic = State()


class IntakeForm(StatesGroup):
    collecting = State()  # заявка в свободной форме: собираем поля из JSON


def chunks(text: str, size: int = TG_MAX_LEN):
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:  # админ мог не запускать бота
            log.warning("cannot notify admin %s: %s", admin_id, e)


def who(user) -> str:
    return "@" + user.username if user.username else "без username (id %s)" % user.id


# ---------- базовые команды ----------

@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! Это виртуальный помощник юридической компании Oryntay & partners.\n\n"
        "Я помогу разобраться в несложных юридических вопросах и дам предварительный совет. "
        "Опишите вашу ситуацию.\n\n"
        "Если нужна консультация живого юриста, нажмите «%s» или напишите в свободной форме, например: "
        "«Запишите меня завтра в 15:00, я Алия, телефон 8 701 123 45 67»." % BTN_BOOK,
        reply_markup=MAIN_KB,
    )


@dp.message(Command("myid"))
async def on_myid(message: Message):
    await message.answer("Ваш Telegram ID: %s" % message.from_user.id)


@dp.message(Command("reset"))
@dp.message(F.text == BTN_RESET)
async def on_reset(message: Message):
    histories.pop(message.chat.id, None)
    await message.answer("Начнём заново. Опишите ваш вопрос.", reply_markup=MAIN_KB)


@dp.message(Command("cancel"))
@dp.message(F.text == BTN_CANCEL)
async def on_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо, отменил.", reply_markup=MAIN_KB)


# ---------- запись на консультацию ----------

def dates_kb():
    days = booking.free_days()
    if not days:
        return None
    rows, row = [], []  # type: List[List[InlineKeyboardButton]], List[InlineKeyboardButton]
    for d in days:
        row.append(InlineKeyboardButton(text=booking.fmt_day(d), callback_data="d:" + d))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_dates(message: Message, user_id: int, edit: bool = False):
    if booking.active_count(user_id) >= booking.MAX_ACTIVE_PER_USER:
        await message.answer(
            "У вас уже %d активные записи. Чтобы записаться снова, отмените одну из них "
            "в разделе «%s»." % (booking.MAX_ACTIVE_PER_USER, BTN_MY)
        )
        return
    kb = dates_kb()
    if kb is None:
        await message.answer("Свободных дат ближайшее время нет. Попробуйте позже.")
        return
    text = "Выберите удобную дату консультации (%d мин):" % booking.SLOT_MINUTES
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@dp.message(Command("book"))
@dp.message(F.text == BTN_BOOK)
async def on_book(message: Message, state: FSMContext):
    await state.clear()
    await show_dates(message, message.from_user.id)


@dp.callback_query(F.data == "book")
async def cb_book(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await show_dates(cb.message, cb.from_user.id)


@dp.callback_query(F.data == "back")
async def cb_back(cb: CallbackQuery):
    await cb.answer()
    await show_dates(cb.message, cb.from_user.id, edit=True)


@dp.callback_query(F.data.startswith("d:"))
async def cb_day(cb: CallbackQuery):
    await cb.answer()
    day = cb.data[2:]
    times = booking.free_times(day)
    if not times:
        await cb.message.edit_text("На эту дату свободного времени уже нет. Выберите другую:",
                                   reply_markup=dates_kb())
        return
    rows, row = [], []  # type: List[List[InlineKeyboardButton]], List[InlineKeyboardButton]
    for s in times:
        row.append(InlineKeyboardButton(text=s[11:], callback_data="t:" + s))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="← Другая дата", callback_data="back")])
    await cb.message.edit_text(
        "Свободное время на %s:" % booking.fmt_day(day), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data.startswith("t:"))
async def cb_time(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    slot = cb.data[2:]
    if not booking.is_free(slot):
        await cb.message.edit_text("Это время только что заняли. Выберите другое:", reply_markup=dates_kb())
        return
    await state.set_state(BookForm.name)
    await state.update_data(slot=slot)
    await cb.message.edit_text("Вы выбрали: %s." % booking.fmt_slot(slot))
    await cb.message.answer(
        "Как вас зовут?",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True),
    )


@dp.message(BookForm.name, F.text)
async def form_name(message: Message, state: FSMContext):
    name = guard.clean(message.text, 200)
    if not 2 <= len(name) <= 60:
        await message.answer("Напишите, пожалуйста, имя (от 2 до 60 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(BookForm.phone)
    await message.answer(
        "Укажите номер телефона для связи или нажмите кнопку ниже.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
                      [KeyboardButton(text=BTN_CANCEL)]],
            resize_keyboard=True,
        ),
    )


@dp.message(BookForm.phone, F.contact | F.text)
async def form_phone(message: Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        await message.answer("Не похоже на номер телефона. Пример: +7 701 123 45 67")
        return
    await state.update_data(phone=("+" + digits) if message.contact else guard.clean(raw, 30))
    await state.set_state(BookForm.topic)
    await message.answer(
        "Коротко опишите, с каким вопросом вы хотите прийти (или отправьте «-», чтобы пропустить). "
        "Паспортные данные и ИИН писать не нужно.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True),
    )


@dp.message(BookForm.topic, F.text)
async def form_topic(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    topic = guard.clean(message.text, 500)
    if topic == "-":
        topic = ""
    topic, found = guard.redact(topic)
    if found:
        await message.answer("Из описания я убрал: %s. Такие данные не нужны, юрист запросит их при встрече."
                             % ", ".join(found))
    user = message.from_user
    try:
        booking_id = booking.book(user.id, user.username, data["name"], data["phone"], topic, data["slot"])
    except booking.SlotTaken:
        await state.clear()
        await message.answer("Это время только что заняли, приношу извинения. Выберите другое.",
                             reply_markup=MAIN_KB)
        await show_dates(message, user.id)
        return
    except booking.LimitReached:
        await state.clear()
        await message.answer("У вас уже максимум активных записей.", reply_markup=MAIN_KB)
        return
    await state.clear()
    await message.answer(
        "✅ Вы записаны на консультацию\n\n"
        "📅 %s\n👤 %s\n📞 %s\n\n"
        "Если планы изменятся, отмените запись в разделе «%s»."
        % (booking.fmt_slot(data["slot"]), data["name"], data["phone"], BTN_MY),
        reply_markup=MAIN_KB,
    )
    await notify_admins(
        bot,
        "🆕 Новая запись #%d\n📅 %s\n👤 %s\n📞 %s\n💬 %s\nTelegram: %s"
        % (booking_id, booking.fmt_slot(data["slot"]), data["name"], data["phone"],
           topic or "—", who(user)),
    )


@dp.message(Command("mybooking"))
@dp.message(F.text == BTN_MY)
async def on_my(message: Message):
    rows = booking.user_bookings(message.from_user.id)
    if not rows:
        await message.answer("Активных записей нет. Нажмите «%s», чтобы записаться." % BTN_BOOK)
        return
    for r in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить запись", callback_data="cx:%d" % r["id"])]]
        )
        await message.answer("📅 %s\n👤 %s" % (booking.fmt_slot(r["slot"]), r["name"]), reply_markup=kb)


@dp.callback_query(F.data.startswith("cx:"))
async def cb_cancel_booking(cb: CallbackQuery, bot: Bot):
    row = booking.cancel(int(cb.data[3:]), cb.from_user.id)
    if row is None:
        await cb.answer("Запись уже отменена", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    await cb.answer()
    await cb.message.edit_text("Запись на %s отменена." % booking.fmt_slot(row["slot"]))
    await notify_admins(
        bot,
        "❌ Запись #%d отменена клиентом\n📅 %s\n👤 %s, %s"
        % (row["id"], booking.fmt_slot(row["slot"]), row["name"], row["phone"]),
    )


@dp.message(Command("bookings"))
async def on_bookings(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = booking.upcoming_all()
    if not rows:
        await message.answer("Предстоящих записей нет.")
        return
    text = "\n\n".join(
        "#%d  %s\n%s, %s\n%s" % (r["id"], booking.fmt_slot(r["slot"]), r["name"], r["phone"], r["topic"] or "—")
        for r in rows
    )
    for part in chunks(text):
        await message.answer(part)


# ---------- проверки входящего текста (общие для диалога и приёма заявки) ----------

async def prepare(message: Message, bot: Bot) -> Optional[str]:
    """Прогоняет сообщение через защиту. Возвращает чистый текст или None, если ответ уже отправлен."""
    user = message.from_user
    is_admin = user.id in ADMIN_IDS

    # 1. Заглушённым (после повторных попыток взлома) не отвечаем вообще
    if not is_admin and strikes.muted_for(user.id):
        return None

    # 2. Слишком длинный ввод не режем молча, а просим сократить
    if len(message.text) > guard.MAX_INPUT_CHARS:
        await message.answer("Сообщение слишком длинное (максимум %d символов). Сократите его, пожалуйста."
                             % guard.MAX_INPUT_CHARS)
        return None

    # 3. Чистим скрытые символы и подделку служебных тегов
    text = guard.clean(message.text)
    if not text:
        return None

    # 4. Явные попытки выманить промпт или сменить роль: отвечаем заготовкой, модель не трогаем
    if guard.looks_like_injection(text):
        log.warning("injection attempt from user %s", user.id)  # сам текст в лог не пишем
        if not is_admin and strikes.add(user.id):
            await notify_admins(bot, "🚨 %s неоднократно пытался сменить инструкции бота, "
                                     "заглушён на %d мин." % (who(user), strikes.mute // 60))
        await message.answer(prompts.REFUSAL)
        return None

    # 5. Лимит запросов: берегу бесплатную квоту моделей от одного пользователя
    if not is_admin:
        ok, wait = limiter.check(user.id)
        if not ok:
            if should_notice(user.id):
                await message.answer("Вы пишете слишком часто. Попробуйте через %s" % fmt_wait(wait))
            return None

    # 6. Номера карт, ИИН и ключи в модель не отправляем
    text, found = guard.redact(text)
    if found:
        await message.answer("⚠️ Я скрыл в вашем сообщении: %s. Такие данные в чат лучше не отправлять, "
                             "юрист запросит их при личной консультации." % ", ".join(found))
    return text


# ---------- приём заявки через JSON ----------

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="ic:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ic:no"),
    ]])


async def run_intake(message: Message, state: FSMContext, bot: Bot, text: str):
    """Один шаг приёма заявки: модель достаёт поля в JSON, код проверяет и решает, что спросить дальше."""
    data = await state.get_data()
    draft, texts, asked, turns = data["draft"], data["texts"] + [text], data["asked"], data["turns"] + 1

    if turns > intake.MAX_TURNS:
        await state.clear()
        await message.answer("Не получается собрать заявку в переписке. Нажмите «%s» и выберите время кнопками."
                             % BTN_BOOK, reply_markup=MAIN_KB)
        return

    await bot.send_chat_action(message.chat.id, "typing")
    prompt = intake.build_prompt(datetime.now(booking.TZ), draft, asked)
    try:
        reply = await llm.extract(http, prompt, text)
    except llm.LLMError as e:
        log.error("intake LLM failed: %s", e)
        await state.clear()
        await message.answer("Не получилось разобрать заявку автоматически. Нажмите «%s» и выберите время "
                             "кнопками." % BTN_BOOK, reply_markup=MAIN_KB)
        return

    fields = intake.parse_json(reply)
    log.info("intake json keys: %s", sorted(k for k, v in fields.items() if v))  # значения в лог не пишем
    draft, problems = intake.update_draft(draft, fields, " ".join(texts))
    asked = intake.missing(draft)
    await state.update_data(draft=draft, texts=texts, asked=asked, turns=turns)

    if intake.is_complete(draft):
        text_out = "\n".join(problems + [intake.summary(draft)])
        await message.answer(text_out, reply_markup=confirm_kb())
    else:
        await message.answer(intake.reask_text(draft, problems))


@dp.message(IntakeForm.collecting, F.text)
async def on_intake_text(message: Message, state: FSMContext, bot: Bot):
    text = await prepare(message, bot)
    if text is not None:
        await run_intake(message, state, bot, text)


@dp.callback_query(F.data == "ic:no")
async def cb_intake_no(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Заявка отменена. Если захотите записаться, просто напишите об этом.")


@dp.callback_query(F.data == "ic:yes")
async def cb_intake_yes(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    draft = data.get("draft") or {}
    if not intake.is_complete(draft):
        await state.clear()
        await cb.message.edit_text("Заявка устарела. Напишите, что хотите записаться, и начнём заново.")
        return
    slot = draft["date"] + "T" + draft["time"]
    user = cb.from_user
    try:
        booking_id = booking.book(user.id, user.username, draft["name"], draft["phone"],
                                  draft.get("topic", ""), slot)
    except booking.SlotTaken:
        draft.pop("time")
        await state.update_data(draft=draft)
        await cb.message.edit_text("Это время только что заняли. " + intake.reask_text(draft, []))
        return
    except booking.LimitReached:
        await state.clear()
        await cb.message.edit_text("У вас уже максимум активных записей. Отмените одну в разделе «%s»." % BTN_MY)
        return
    await state.clear()
    await cb.message.edit_text(
        "✅ Заявка принята, вы записаны на консультацию\n\n📅 %s\n👤 %s\n📞 %s\n\n"
        "Если планы изменятся, отмените запись в разделе «%s»."
        % (booking.fmt_slot(slot), draft["name"], draft["phone"], BTN_MY)
    )
    await notify_admins(
        bot,
        "🆕 Новая заявка #%d (через чат)\n📅 %s\n👤 %s\n📞 %s\n💬 %s\nTelegram: %s"
        % (booking_id, booking.fmt_slot(slot), draft["name"], draft["phone"],
           draft.get("topic") or "—", who(user)),
    )


# ---------- обычный разговор с ИИ ----------

@dp.message(StateFilter(None), F.text)
async def on_text(message: Message, bot: Bot, state: FSMContext):
    text = await prepare(message, bot)
    if text is None:
        return

    # Просьба записаться: разбираем заявку через JSON, обычный диалог не нужен
    if intake.wants_booking(text):
        await state.set_state(IntakeForm.collecting)
        await state.update_data(draft={}, texts=[], asked=[], turns=0)
        await run_intake(message, state, bot, text)
        return

    chat_id = message.chat.id
    async with locks[chat_id]:
        history = histories[chat_id]
        history.append({"role": "user", "content": text})
        await bot.send_chat_action(chat_id, "typing")
        try:
            answer = await llm.ask_checked(http, list(history))
        except llm.LLMError as e:
            history.pop()  # не оставляем без ответа вопрос в истории
            log.error("LLM failed: %s", e)
            await message.answer(
                "Сейчас не получается ответить, попробуйте чуть позже. "
                "Вы можете записаться к юристу кнопкой ниже.",
                reply_markup=BOOK_INLINE,
            )
            return
        # 7. Если модель выдала кусок системного промпта, подменяем ответ
        if guard.leaks_prompt(answer):
            log.warning("prompt leak blocked for user %s (overlap=%d)", user.id, guard.prompt_overlap(answer))
            answer = prompts.REFUSAL
        answer = guard.postprocess(answer) or prompts.REFUSAL
        history.append({"role": "assistant", "content": answer})
    parts = list(chunks(answer))
    for i, part in enumerate(parts):
        # кнопка записи только под последней частью ответа
        await message.answer(part, reply_markup=BOOK_INLINE if i == len(parts) - 1 else None)


@dp.message(StateFilter(None))
async def on_other(message: Message):
    await message.answer("Я понимаю только текстовые сообщения. Опишите ситуацию словами.")


async def main():
    global http
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN в .env (токен берётся у @BotFather)")
    booking.init_db()
    http = httpx.AsyncClient()
    bot = Bot(token)
    try:
        log.info("models: %s", ", ".join(llm.get_models()))
        log.info("admins: %s", ADMIN_IDS or "не заданы (ADMIN_IDS)")
        await dp.start_polling(bot)
    finally:
        await http.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
