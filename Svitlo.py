import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_FILE)
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError(f"BOT_TOKEN не заданий. Створи файл {ENV_FILE} і додай BOT_TOKEN=...")

try:
    ADMIN_IDS = {
        int(value.strip())
        for value in os.getenv("ADMIN_IDS", "").split(",")
        if value.strip()
    }
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS має містити числові Telegram ID через кому.") from exc
if any(admin_id <= 0 for admin_id in ADMIN_IDS):
    raise RuntimeError("ADMIN_IDS має містити додатні Telegram ID.")

SCHEDULE_API_URL = "https://be-svitlo.oe.if.ua/schedule-by-queue"
UKRAINE_TZ = ZoneInfo("Europe/Kyiv")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))  # секунд
NOTIFY_MINUTES = int(os.getenv("NOTIFY_MINUTES", "10"))
QUEUES = [f"{n}.{part}" for n in range(1, 6) for part in (1, 2)]
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", Path(__file__).resolve().with_name("bot_state.json"))).expanduser()
if not STATE_FILE.is_absolute():
    STATE_FILE = Path(__file__).resolve().parent / STATE_FILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("svitlo-bot")

bot = Bot(token=TOKEN)
dp = Dispatcher()

state = {
    "users": {},
    "banned_users": {},
    "last_schedules": {},
    "notified": [],
    "notified_schedules": [],
    "last_check": None,
    "last_error": None
}



def load_state():
    global state
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Файл стану має містити JSON-об'єкт.")
            for key, default in state.items():
                value = loaded.get(key, default)
                if key in {"users", "banned_users", "last_schedules"} and not isinstance(value, dict):
                    raise ValueError(f"Поле {key} у файлі стану має бути об'єктом.")
                if key == "notified" and not isinstance(value, list):
                    raise ValueError("Поле notified у файлі стану має бути списком.")
                if key == "notified_schedules" and not isinstance(value, list):
                    raise ValueError("Поле notified_schedules у файлі стану має бути списком.")
                state[key] = value
            log.info("Стан бота завантажено з %s", STATE_FILE)
        except Exception:
            log.exception("Не вдалося прочитати файл стану %s; файл залишено без змін", STATE_FILE)
            raise


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_file.replace(STATE_FILE)


def get_schedule(queue: str):
    try:
        response = requests.get(
            SCHEDULE_API_URL,
            params={"queue": queue},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        log.exception("Помилка отримання графіка для черги %s", queue)
        return None

    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        log.error("API не повернуло графік для черги %s", queue)
        return None

    schedule_data = data[0]
    raw_event_date = schedule_data.get("eventDate")
    if not isinstance(raw_event_date, str):
        log.error("API не повернуло дату графіка для черги %s", queue)
        return None
    try:
        try:
            event_date = datetime.strptime(raw_event_date, "%d.%m.%Y").date()
        except ValueError:
            event_date = date.fromisoformat(raw_event_date)
    except ValueError:
        log.error("API повернуло некоректну дату графіка для черги %s: %s", queue, raw_event_date)
        return None

    queues_data = schedule_data.get("queues", {})
    if not isinstance(queues_data, dict):
        log.error("Некоректний формат графіка для черги %s", queue)
        return None

    if queue not in queues_data:
        log.error("API не повернуло графік для черги %s", queue)
        return None

    queue_data = queues_data[queue]
    if not isinstance(queue_data, list):
        log.error("Некоректний формат графіка для черги %s", queue)
        return None
    if not queue_data:
        return event_date, []

    result = []
    for item in queue_data:
        if not isinstance(item, dict):
            continue
        start = item.get("from")
        end = item.get("to")
        if start and end:
            result.append((start, end))

    return event_date, result


def _parse_schedule_minute(value: str) -> int:
    value = value.strip()
    if value == "24:00":
        return 24 * 60
    try:
        parsed_time = time.fromisoformat(value)
    except ValueError:
        parsed_time = datetime.fromisoformat(value).time()
    return parsed_time.hour * 60 + parsed_time.minute


def _intervals_to_minutes(intervals):
    minute_intervals = []
    for start, end in intervals:
        start_minute = _parse_schedule_minute(start)
        end_minute = _parse_schedule_minute(end)
        if end_minute < start_minute:
            end_minute += 24 * 60
        if end_minute <= start_minute:
            continue
        if end_minute <= 24 * 60:
            minute_intervals.append((start_minute, end_minute))
        else:
            minute_intervals.append((start_minute, 24 * 60))
            minute_intervals.append((0, end_minute - 24 * 60))

    merged = []
    for start_minute, end_minute in sorted(minute_intervals):
        if merged and start_minute <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_minute))
        else:
            merged.append((start_minute, end_minute))
    return merged


def fetch_all_schedules():
    schedules = {}
    for queue in QUEUES:
        schedule = get_schedule(queue)
        if schedule is None:
            raise RuntimeError(f"Не вдалося отримати графік для черги {queue} з API.")
        event_date, intervals = schedule
        schedules[queue] = {
            "event_date": event_date.isoformat(),
            "intervals": _intervals_to_minutes(intervals)
        }
    return schedules


def _format_schedule_interval(interval):
    start_minute, end_minute = interval
    start = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
    end = "24:00" if end_minute == 24 * 60 else f"{end_minute // 60:02d}:{end_minute % 60:02d}"
    return f"{start}–{end}"


def schedule_to_text(intervals):
    if not intervals:
        return "🟢 00:00–24:00 — світло весь день (за графіком відключень не заплановано)."

    segments = []
    cursor = 0
    for start_minute, end_minute in intervals:
        if cursor < start_minute:
            segments.append(f"🟢 {_format_schedule_interval((cursor, start_minute))}")
        segments.append(f"🔴 {_format_schedule_interval((start_minute, end_minute))}")
        cursor = end_minute
        print("Подякувати автору можна на 4441111030452761")
    if cursor < 24 * 60:
        segments.append(f"🟢 {_format_schedule_interval((cursor, 24 * 60))}")
    return "\n".join(segments)


def schedule_transitions(intervals):
    transitions = []
    spans_midnight = bool(
        intervals and intervals[0][0] == 0 and intervals[-1][1] == 24 * 60
    )
    for start_minute, end_minute in intervals:
        if start_minute != 0 or not spans_midnight:
            transitions.append((start_minute, "off"))
        if end_minute != 24 * 60 or not spans_midnight:
            transitions.append((end_minute, "on"))
    return sorted(transitions)


def make_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=(
            [[KeyboardButton(text=q) for q in QUEUES[i:i + 2]] for i in range(0, len(QUEUES), 2)]
            + [[
                KeyboardButton(text="🔔 Увімкнути сповіщення"),
                KeyboardButton(text="🔕 Вимкнути сповіщення")
            ]]
        ),
        resize_keyboard=True
    )


def user_record(chat_id):
    return state.setdefault("users", {}).setdefault(str(chat_id), {"queue": None})


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def ban_middleware(handler, event: Message, data: dict):
    user = event.from_user
    if user and not is_admin(user.id) and str(user.id) in state.get("banned_users", {}):
        await event.answer("⛔ Доступ до бота обмежено адміністратором.")
        return None
    return await handler(event, data)


dp.message.outer_middleware(ban_middleware)


async def require_admin(message: Message) -> bool:
    if message.from_user and is_admin(message.from_user.id):
        return True
    await message.answer("⛔ Ця команда доступна лише адміністратору.")
    return False


def _safe_user_label(value: str) -> str:
    return " ".join(value.split())[:80]


@dp.message(Command("myid"))
async def my_id(message: Message):
    if message.from_user:
        await message.answer(f"Твій Telegram ID: {message.from_user.id}")


@dp.message(Command("users"))
async def list_users(message: Message):
    if not await require_admin(message):
        return

    users = state.get("users", {})
    banned_users = state.get("banned_users", {})
    lines = [f"👥 Користувачів, які запускали бота: {len(users)}"]
    for chat_id, record in sorted(users.items()):
        record = record if isinstance(record, dict) else {}
        name = _safe_user_label(record.get("first_name") or "Без імені")
        username = record.get("username")
        username_label = f"@{_safe_user_label(username)}" if username else "username відсутній"
        queue = record.get("queue") or "чергу не вибрано"
        ban = " | ЗАБАНЕНИЙ" if chat_id in banned_users else ""
        lines.append(f"{chat_id} ({username_label}) | {name} | {queue}{ban}")

    text = "\n".join(lines)
    for offset in range(0, len(text), 3500):
        await message.answer(text[offset:offset + 3500])


@dp.message(Command("ban"))
async def ban_user(message: Message):
    if not await require_admin(message):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.answer("Використання: /ban <user_id> [причина]\nID користувачів можна подивитися командою /users.")
        return

    target_id = int(parts[1])
    if is_admin(target_id):
        await message.answer("Не можна забанити адміністратора.")
        return

    banned_users = state.setdefault("banned_users", {})
    if str(target_id) in banned_users:
        await message.answer(f"Користувач {target_id} уже забанений.")
        return

    reason = parts[2].strip() if len(parts) > 2 else ""
    banned_users[str(target_id)] = {
        "banned_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason
    }
    save_state()
    await message.answer(f"✅ Користувача {target_id} забанено.")


@dp.message(Command("unban"))
async def unban_user(message: Message):
    if not await require_admin(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.answer("Використання: /unban <user_id>")
        return

    target_id = parts[1]
    banned_users = state.setdefault("banned_users", {})
    if banned_users.pop(target_id, None) is None:
        await message.answer(f"Користувача {target_id} немає у списку банів.")
        return

    save_state()
    await message.answer(f"✅ Користувача {target_id} розбанено.")



@dp.message(Command("start"))
async def start(message: Message):
    record = None
    if message.from_user:
        record = user_record(str(message.chat.id))
        record["first_name"] = message.from_user.full_name
        record["username"] = message.from_user.username
        record.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
        save_state()
    notifications_enabled = record.get("notifications_enabled", True) if record else True
    await message.answer(
        "Привіт! Я відстежую графік відключень Прикарпаттяобленерго.\n\n"
        "Обери свою чергу кнопкою нижче. Після вибору я надсилатиму попередження за 10 хв до відключення "
        "та повідомлятиму, якщо графік зміниться. Кнопкою внизу можна вимкнути або знову увімкнути автоматичні сповіщення.",
        reply_markup=make_keyboard()
    )



@dp.message(Command("status"))
async def status(message: Message):
    chat_id = str(message.chat.id)
    record = state.get("users", {}).get(chat_id)
    queue = record.get("queue") if record else None
    notifications_enabled = record.get("notifications_enabled", True) if record else True
    last_check = state.get("last_check") or "ще не перевірявся"
    error = state.get("last_error")
    notification_status = "увімкнені" if notifications_enabled else "вимкнені"
    text = (
        f"🤖 Бот запущений.\nТвоя черга: {queue or 'не вибрана'}\n"
        f"Автоматичні сповіщення: {notification_status}\nОстання перевірка сайту: {last_check}"
    )
    if error:
        text += f"\n⚠️ Остання помилка: {error}"
    await message.answer(text)



@dp.message(Command("test"))
async def test(message: Message):
    await message.answer("✅ Тестове повідомлення. Telegram-сповіщення працюють: бот може надсилати повідомлення в цей чат.")



@dp.message(Command("stop"))
async def stop(message: Message):
    record = state.get("users", {}).get(str(message.chat.id))
    if record:
        record["queue"] = None
        save_state()
    await message.answer("Моніторинг для цього чату вимкнено. Щоб знову підписатися, обери чергу.")


async def set_notifications(message: Message, enabled: bool):
    record = user_record(str(message.chat.id))
    record["notifications_enabled"] = enabled
    save_state()
    if enabled:
        text = "🔔 Автоматичні сповіщення увімкнено."
    else:
        text = "🔕 Автоматичні сповіщення вимкнено. Щоб отримувати їх знову, натисни кнопку нижче."
    await message.answer(text, reply_markup=make_keyboard())


def notification_action(text: str) -> bool | None:
    words = "".join(character for character in text.casefold() if character.isalnum() or character.isspace()).split()
    if not words:
        return None
    action = words[0]
    has_notification_word = any(word.startswith("сповіщ") for word in words)
    if action in {"увімкнути", "увімкни", "включити", "enable", "on"}:
        return True if has_notification_word or len(words) == 1 else None
    if action in {"вимкнути", "вимкни", "відключити", "disable", "off"}:
        return False if has_notification_word or len(words) == 1 else None
    return None


@dp.message(Command("notifications", "notify"))
async def notifications_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Використання: /notifications on або /notifications off")
        return

    action = parts[1].strip().casefold()
    if action in {"on", "увімкнути", "увімкни"}:
        await set_notifications(message, True)
    elif action in {"off", "вимкнути", "вимкни"}:
        await set_notifications(message, False)
    else:
        await message.answer("Використання: /notifications on або /notifications off")


@dp.message(
    F.text.func(
        lambda text: notification_action(text) is not None
    )
)
async def toggle_notifications(message: Message):
    enabled = notification_action(message.text)
    if enabled is not None:
        await set_notifications(message, enabled)



@dp.message(F.text.in_(QUEUES))
async def choose_queue(message: Message):
    queue = message.text.strip()
    chat_id = str(message.chat.id)
    record = user_record(chat_id)
    record["queue"] = queue
    if message.from_user:
        record["first_name"] = message.from_user.full_name
        record["username"] = message.from_user.username
        record.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
    save_state()
    try:
        schedules = await asyncio.to_thread(fetch_all_schedules)
        schedule = schedules.get(queue)
        if schedule is None:
            await message.answer(f"⚠️ Чергу {queue} не вдалося знайти на сайті. Я зберіг вибір і повторю перевірку автоматично.")
        else:
            event_date = date.fromisoformat(schedule["event_date"])
            notifications_enabled = record.get("notifications_enabled", True)
            notification_message = (
                f"🔔 Моніторинг увімкнено. Попередження — за {NOTIFY_MINUTES} хв до відключення."
                if notifications_enabled
                else "🔕 Автоматичні сповіщення вимкнені. Увімкни їх кнопкою нижче."
            )
            await message.answer(
                f"📅 Графік для черги {queue} на {event_date:%d.%m.%Y}:\n"
                f"{schedule_to_text(schedule['intervals'])}\n\n"
                f"{notification_message}\n"
                "Підтримати автора: 4874100035861288",
                reply_markup=make_keyboard()
            )
    except Exception as e:
        log.exception("Помилка під час отримання графіка")
        await message.answer("⚠️ Не зміг зараз отримати графік із сайту, але чергу збережено. Спробую ще раз автоматично.")



async def send_to_queue(queue, text):
    sent_count = 0
    for chat_id, user in list(state.get("users", {}).items()):
        if (
            isinstance(user, dict)
            and user.get("queue") == queue
            and user.get("notifications_enabled", True) is not False
        ):
            try:
                await bot.send_message(int(chat_id), text)
                sent_count += 1
            except Exception:
                log.exception("Не вдалося надіслати повідомлення chat_id=%s", chat_id)
    return sent_count



async def monitor_loop():
    log.info("Моніторинг графіків запущено. Інтервал: %s секунд", CHECK_INTERVAL)
    while True:
        try:
            schedules = await asyncio.to_thread(fetch_all_schedules)
            previous = state.setdefault("last_schedules", {})
            notified_schedules = set(state.get("notified_schedules", []))
            now = datetime.now(UKRAINE_TZ)
            # Порівнюємо зміни лише для попереднього графіка тієї самої дати.
            for queue, schedule in schedules.items():
                event_date = date.fromisoformat(schedule["event_date"])
                event_date_text = event_date.strftime("%d.%m.%Y")
                intervals = schedule["intervals"]
                new_intervals = sorted(intervals)
                old_intervals = previous.get(queue)
                old_event_date = None
                if isinstance(old_intervals, dict):
                    old_event_date = old_intervals.get("event_date")
                    old_intervals = old_intervals.get("intervals")
                if isinstance(old_intervals, list) and all(
                    isinstance(item, list) and len(item) == 2 and
                    all(isinstance(value, int) for value in item)
                    for item in old_intervals
                ):
                    old_intervals = sorted(tuple(item) for item in old_intervals)
                else:
                    old_intervals = None
                if (
                    old_intervals is not None
                    and old_event_date == event_date.isoformat()
                    and old_intervals != new_intervals
                ):
                    old_set, new_set = set(old_intervals), set(new_intervals)
                    added, removed = sorted(new_set - old_set), sorted(old_set - new_set)
                    details = []
                    if added:
                        details.append("Додали відключення: " + ", ".join(_format_schedule_interval(interval) for interval in added))
                    if removed:
                        details.append("Прибрали відключення: " + ", ".join(_format_schedule_interval(interval) for interval in removed))
                    msg = (
                        f"🔄 ГРАФІК ЗМІНИВСЯ!\nЧерга {queue}\nДата графіка: {event_date_text}\n"
                        + "\n".join(details)
                        + "\n\nНовий графік:\n"
                        + schedule_to_text(new_intervals)
                    )
                    await send_to_queue(queue, msg)
                previous[queue] = {
                    "event_date": event_date.isoformat(),
                    "intervals": [list(interval) for interval in new_intervals]
                }

                schedule_notification_key = f"{event_date.isoformat()}|{queue}"
                if (
                    event_date == now.date() + timedelta(days=1)
                    and schedule_notification_key not in notified_schedules
                ):
                    sent_count = await send_to_queue(
                        queue,
                        f"📅 ДОДАНО ГРАФІК НА {event_date_text}!\n"
                        f"Черга {queue}\n\n{schedule_to_text(new_intervals)}"
                    )
                    if sent_count:
                        notified_schedules.add(schedule_notification_key)
                        state["notified_schedules"] = list(notified_schedules)

                # Нагадування про вимкнення та відновлення світла.
                for transition_minute, action in schedule_transitions(new_intervals):
                    transition_dt = datetime.combine(
                        event_date,
                        time.min,
                        tzinfo=UKRAINE_TZ
                    ) + timedelta(minutes=transition_minute)
                    delta = (transition_dt - now).total_seconds()
                    notification_key = f"{transition_dt.date().isoformat()}|{queue}|{action}|{transition_minute % (24 * 60)}"
                    notified = set(state.get("notified", []))
                    if 0 < delta <= NOTIFY_MINUTES * 60 and notification_key not in notified:
                        action_text = "вимкнуть" if action == "off" else "увімкнуть"
                        sent_count = await send_to_queue(
                            queue,
                            f"⚠️ Через приблизно {max(1, int((delta + 59) // 60))} хв {action_text} світло!\n"
                            f"Черга {queue}\nЧас: {transition_dt:%H:%M}"
                        )
                        if sent_count:
                            notified.add(notification_key)
                            state["notified"] = list(notified)
                        else:
                            log.warning(
                                "Не вдалося доставити нагадування для черги %s; спробуємо знову під час наступної перевірки",
                                queue
                            )
            # Keep reminder history limited to the current and previous day.
            today = datetime.now(UKRAINE_TZ).date()
            state["notified"] = [k for k in state.get("notified", []) if k.split("|", 1)[0] >= (today - timedelta(days=1)).isoformat()]
            state["notified_schedules"] = [
                key for key in state.get("notified_schedules", [])
                if key.split("|", 1)[0] >= today.isoformat()
            ]
            state["last_check"] = datetime.now(UKRAINE_TZ).strftime("%d.%m.%Y %H:%M:%S")
            state["last_error"] = None
            save_state()
        except Exception as e:
            state["last_error"] = str(e)[:300]
            log.exception("Помилка перевірки графіків")
            save_state()
        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    load_state()
    log.info("Бот запускається")
    monitor_task = asyncio.create_task(monitor_loop())
    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
