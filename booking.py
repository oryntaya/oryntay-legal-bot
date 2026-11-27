"""Расписание консультаций и запись клиентов (SQLite).

Один слот = одна консультация. Рабочие часы, длительность слота и горизонт записи
настраиваются переменными окружения (см. .env.example).
"""
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
except Exception:  # нет tzdata в системе
    TZ = timezone(timedelta(hours=5))

DB_PATH = os.getenv("BOOKINGS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.db"))

WORK_DAYS = {0, 1, 2, 3, 4}                      # пн-пт
WORK_START = int(os.getenv("WORK_START", "10"))  # час начала приёма
WORK_END = int(os.getenv("WORK_END", "18"))      # час окончания приёма
LUNCH_START, LUNCH_END = 13, 14                  # перерыв, слоты в него не попадают
SLOT_MINUTES = int(os.getenv("SLOT_MINUTES", "60"))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))
MIN_LEAD = timedelta(hours=int(os.getenv("MIN_LEAD_HOURS", "2")))
MAX_ACTIVE_PER_USER = 2

FMT = "%Y-%m-%dT%H:%M"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

_conn: Optional[sqlite3.Connection] = None


class SlotTaken(Exception):
    pass


class LimitReached(Exception):
    pass


def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(DB_PATH)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            tg_username TEXT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            topic TEXT,
            slot TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_active_slot ON bookings(slot) WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS ix_bookings_tg ON bookings(tg_id);
        """
    )
    _conn.commit()


def _now() -> datetime:
    return datetime.now(TZ)


def _generate(now: datetime) -> List[str]:
    step = timedelta(minutes=SLOT_MINUTES)
    out = []  # type: List[str]
    for offset in range(DAYS_AHEAD + 1):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in WORK_DAYS:
            continue
        cur = datetime.combine(day, time(WORK_START))
        end = datetime.combine(day, time(WORK_END))
        lunch_a = datetime.combine(day, time(LUNCH_START))
        lunch_b = datetime.combine(day, time(LUNCH_END))
        while cur + step <= end:
            overlaps_lunch = cur < lunch_b and cur + step > lunch_a
            # сравниваем «настенное» время офиса с текущим временем в том же поясе
            is_future = cur.replace(tzinfo=TZ) >= now + MIN_LEAD
            if not overlaps_lunch and is_future:
                out.append(cur.strftime(FMT))
            cur += step
    return out


def free_slots() -> List[str]:
    now = _now()
    rows = _conn.execute(
        "SELECT slot FROM bookings WHERE status = 'active' AND slot >= ?", (now.strftime(FMT),)
    )
    taken = {r["slot"] for r in rows}
    return [s for s in _generate(now) if s not in taken]


def free_days() -> List[str]:
    """Даты (YYYY-MM-DD), на которые есть хотя бы одно свободное время."""
    days = []  # type: List[str]
    for s in free_slots():
        d = s[:10]
        if d not in days:
            days.append(d)
    return days


def free_times(day: str) -> List[str]:
    """Свободные слоты на дату в виде полных ключей YYYY-MM-DDTHH:MM."""
    return [s for s in free_slots() if s.startswith(day)]


def is_free(slot: str) -> bool:
    return slot in free_slots()


def active_count(tg_id: int) -> int:
    row = _conn.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE tg_id = ? AND status = 'active' AND slot >= ?",
        (tg_id, _now().strftime(FMT)),
    ).fetchone()
    return row["c"]


def book(tg_id: int, username: Optional[str], name: str, phone: str, topic: str, slot: str) -> int:
    if active_count(tg_id) >= MAX_ACTIVE_PER_USER:
        raise LimitReached()
    if not is_free(slot):
        raise SlotTaken()
    try:
        cur = _conn.execute(
            "INSERT INTO bookings (tg_id, tg_username, name, phone, topic, slot, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_id, username, name, phone, topic, slot, _now().strftime(FMT)),
        )
        _conn.commit()
    except sqlite3.IntegrityError:
        raise SlotTaken()
    return cur.lastrowid


def user_bookings(tg_id: int) -> List[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM bookings WHERE tg_id = ? AND status = 'active' AND slot >= ? ORDER BY slot",
        (tg_id, _now().strftime(FMT)),
    ).fetchall()


def cancel(booking_id: int, tg_id: int) -> Optional[sqlite3.Row]:
    row = _conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND tg_id = ? AND status = 'active'", (booking_id, tg_id)
    ).fetchone()
    if row is None:
        return None
    _conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    _conn.commit()
    return row


def upcoming_all() -> List[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM bookings WHERE status = 'active' AND slot >= ? ORDER BY slot",
        (_now().strftime(FMT),),
    ).fetchall()


def fmt_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return "%s %s" % (WEEKDAYS[d.weekday()], d.strftime("%d.%m"))


def fmt_slot(slot: str) -> str:
    d = datetime.strptime(slot, FMT)
    return "%s, %d %s, %s" % (WEEKDAYS[d.weekday()], d.day, MONTHS[d.month - 1], d.strftime("%H:%M"))
