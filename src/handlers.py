"""
Telegram handlers: commands, morning/evening free text, inline callback queries.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import ai as ai_mod
import config
import db
from jobs import (
    _schedule_start_ping,
    _schedule_endpoint_ping,
    _start_ping_keyboard,
    _endpoint_keyboard,
)

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _local_now() -> datetime:
    return datetime.now(config.TZ)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- auth guard ----------

def _authorized(update: Update) -> bool:
    if config.CHAT_ID is None:
        return True
    return update.effective_chat.id == config.CHAT_ID


# ---------- time parsing ----------

_TIME_PATTERNS = [
    re.compile(r"@\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*$", re.I),   # @ 10:30 AM/PM
    re.compile(r"\bat\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*$", re.I), # at 10:30 AM/PM
    re.compile(r"@\s*(\d{1,2}):(\d{2})\s*$"),                     # @ 10:30 (24h)
    re.compile(r"\bat\s+(\d{1,2}):(\d{2})\s*$", re.I),            # at 10:30 (24h)
    re.compile(r"@\s*(\d{1,2})\s*(am|pm)\s*$", re.I),             # @ 2pm
    re.compile(r"\bat\s+(\d{1,2})\s*(am|pm)\s*$", re.I),          # at 2pm
    re.compile(r"@\s*(\d{1,2})\s*$"),                              # @ 10 (24h)
    re.compile(r"\bat\s+(\d{1,2})\s*$", re.I),                     # at 10 (24h)
]

_NUMERIC_PREFIX = re.compile(r"^\d+[\.\)]\s*")


def _parse_task_line(line: str) -> tuple[str, str | None]:
    """Return (description, planned_start_HH:MM or None)."""
    line = line.strip()
    for pat in _TIME_PATTERNS:
        m = pat.search(line)
        if m:
            desc = line[:m.start()].strip().rstrip("@").strip()
            desc = _NUMERIC_PREFIX.sub("", desc)  # strip leading "1. " or "3) "
            groups = m.groups()
            # groups is (hour, minute, ampm) or (hour, ampm) or (hour,)
            if len(groups) == 3:
                # HH:MM AM/PM
                hour, minute, ampm = int(groups[0]), groups[1], groups[2].lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                return desc, f"{hour:02d}:{minute}"
            elif len(groups) == 2 and groups[1].lower() in ("am", "pm"):
                # H AM/PM
                hour, ampm = int(groups[0]), groups[1].lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                return desc, f"{hour:02d}:00"
            elif len(groups) == 2:
                # HH:MM (24h)
                return desc, f"{int(groups[0]):02d}:{groups[1]}"
            else:
                # H (24h)
                return desc, f"{int(groups[0]):02d}:00"
    desc = _NUMERIC_PREFIX.sub("", line)
    return desc, None


_HELP_TEXT = """\
🧠 *ADHD Bot*

*Daily flow*
Morning ping → send your task list, one per line:
  `Call dentist @ 14:00`
  `Finish report`
  `Gym @ 18:00`
Reply *done* when your plan is set.
Evening ping → tell me how it went.

*Add a task anytime*
/todo Buy milk
/todo Submit report @ 3pm

*Commands*
/today — see today's task list
/todo <task> — add a task to today
/skip — skip today's morning plan
/snooze — snooze the current reminder
/silence\_today — no more pings today
/done — lock in your morning plan
/lesson — log a lesson learned
/lessons — view past lessons
/help — show this message
"""


# ---------- /start ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.log_event("command", payload="/start")
    await update.message.reply_text(
        f"Hey. Your chat ID is `{chat_id}`.\n\n"
        "Paste that into `TELEGRAM_CHAT_ID` in your `.env`, then restart the bot.\n\n"
        + _HELP_TEXT,
        parse_mode="Markdown",
    )


# ---------- /help ----------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    db.log_event("command", payload="/help")
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown")


# ---------- /today ----------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = db.get_tasks_for_date(today)
    db.log_event("command", payload="/today")

    if not tasks:
        await update.message.reply_text("No tasks planned yet today. I'll ping you at morning time, or just send me your list now.")
        return

    status_icon = {
        "planned": "○",
        "started": "▶",
        "done": "✓",
        "skipped": "–",
        "stuck": "?",
        "missed": "✗",
    }
    lines = ["Today's plan:"]
    for t in tasks:
        icon = status_icon.get(t["status"], "○")
        time_str = f"  {t['planned_start']}" if t["planned_start"] else ""
        lines.append(f"{icon} {t['description']}{time_str}  [{t['status']}]")
    await update.message.reply_text("\n".join(lines))


# ---------- /skip ----------

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    db.set_day_flag(today, "morning_done", 1)
    db.log_event("silence_today", payload="/skip")
    await update.message.reply_text("Got it — taking the day off. No tasks logged.")


# ---------- /snooze ----------

async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    tasks = db.get_tasks_for_date(today)
    db.log_event("command", payload="/snooze")

    # Find the next planned task with a start time that hasn't fired yet
    pending = [
        t for t in tasks
        if t["status"] == "planned" and t["planned_start"]
    ]
    if not pending:
        await update.message.reply_text("No upcoming start pings to snooze.")
        return

    task = pending[0]
    snooze_until = _utc_now() + timedelta(minutes=config.SNOOZE_MINUTES)
    _schedule_start_ping(context.application, task["id"], snooze_until)
    local_time = snooze_until.astimezone(config.TZ).strftime("%H:%M")
    db.log_event("snoozed", task_id=task["id"])
    await update.message.reply_text(f"Pushed {config.SNOOZE_MINUTES} min. Next nudge at {local_time}.")


# ---------- /silence_today ----------

async def cmd_silence_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    db.set_day_flag(today, "silenced", 1)
    db.log_event("silence_today")

    # Cancel any pending jobs for today
    current_jobs = context.application.job_queue.jobs()
    for job in current_jobs:
        if job.name and (job.name.startswith("start_ping_") or job.name.startswith("endpoint_ping_")):
            job.schedule_removal()

    h, m = config.evening_time()
    await update.message.reply_text(
        f"Quiet for the rest of today. Back tomorrow at {h:02d}:{m:02d}."
    )


# ---------- morning plan (free text) ----------

def _lock_morning_plan_text(tasks: list) -> str:
    lines = ["Locked in:"]
    for i, t in enumerate(tasks, 1):
        time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
        lines.append(f"{i}. {t['description']} — {time_label}")
    if any(t["planned_start"] for t in tasks):
        lines.append("I'll nudge you at each start time.")
    else:
        lines.append("No start times — you're on your own schedule today.")
    return "\n".join(lines)


async def handle_morning_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse the user's free-text morning plan into tasks."""
    today = _today()
    state = db.get_day_state(today)
    if state["morning_done"]:
        return

    existing = db.get_tasks_for_date(today)
    slots_left = 3 - len(existing)
    if slots_left <= 0:
        # Auto-lock — shouldn't normally reach here
        db.set_day_flag(today, "morning_done", 1)
        return

    raw_lines = [l.strip() for l in update.message.text.strip().splitlines() if l.strip()]
    if not raw_lines:
        return

    capped = len(raw_lines) > slots_left
    lines = raw_lines[:slots_left]

    now_local = _local_now()
    added = 0

    for line in lines:
        desc, planned_start = _parse_task_line(line)
        if not desc:
            continue

        if planned_start:
            h, m = planned_start.split(":")
            start_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if start_local <= now_local:
                planned_start = None

        task_id = db.add_task(today, desc, planned_start, config.DEFAULT_TIMER_MINUTES)
        db.log_event("task_added", task_id=task_id, payload=desc)
        added += 1

        if planned_start:
            h, m = planned_start.split(":")
            run_at_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            run_at_utc = run_at_local.astimezone(timezone.utc)
            _schedule_start_ping(context.application, task_id, run_at_utc)

    if added == 0:
        await update.message.reply_text(
            "Couldn't parse that — send one task per line, e.g. `Call dentist @ 14:00`.",
            parse_mode="Markdown",
        )
        return

    all_tasks = db.get_tasks_for_date(today)
    total = len(all_tasks)

    if capped:
        extra = "\nCapped at 3 — the rest can wait. Finishing beats listing."
    else:
        extra = ""

    if total >= 3:
        db.set_day_flag(today, "morning_done", 1)
        await update.message.reply_text(_lock_morning_plan_text(all_tasks) + extra)
    else:
        remaining = 3 - total
        task_word = "task" if remaining == 1 else "tasks"
        list_lines = ["So far:"]
        for i, t in enumerate(all_tasks, 1):
            time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
            list_lines.append(f"{i}. {t['description']} — {time_label}")
        list_lines.append(f"\nAdd up to {remaining} more {task_word}, or /done to lock in.{extra}")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Lock in", callback_data="lock_morning:0")]])
        await update.message.reply_text("\n".join(list_lines), reply_markup=keyboard)


# ---------- /todo (add task to today) ----------

async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: `/todo Call dentist @ 14:00`\n"
            "The `@ HH:MM` time is optional.",
            parse_mode="Markdown",
        )
        return
    desc, planned_start = _parse_task_line(text)

    # Drop times already in the past
    now_local = _local_now()
    if planned_start:
        h, ms = planned_start.split(":")
        start_local = now_local.replace(hour=int(h), minute=int(ms), second=0, microsecond=0)
        if start_local <= now_local:
            planned_start = None

    task_id = db.add_task(_today(), desc, planned_start, config.DEFAULT_TIMER_MINUTES)
    db.log_event("command", task_id=task_id, payload="/todo")

    # Schedule start ping if a future time was given
    if planned_start:
        h, ms = planned_start.split(":")
        run_at_local = now_local.replace(hour=int(h), minute=int(ms), second=0, microsecond=0)
        _schedule_start_ping(context.application, task_id, run_at_local.astimezone(timezone.utc))

    today = _today()
    state = db.get_day_state(today)

    # During morning planning phase: integrate into the plan instead of asking for estimate
    if not state["morning_done"]:
        all_tasks = db.get_tasks_for_date(today)
        if len(all_tasks) >= 3:
            db.set_day_flag(today, "morning_done", 1)
            await update.message.reply_text(_lock_morning_plan_text(all_tasks))
        else:
            remaining = 3 - len(all_tasks)
            task_word = "task" if remaining == 1 else "tasks"
            lines = ["So far:"]
            for i, t in enumerate(all_tasks, 1):
                time_label = t["planned_start"] if t["planned_start"] else "unscheduled"
                lines.append(f"{i}. {t['description']} — {time_label}")
            lines.append(f"\nAdd up to {remaining} more {task_word}, or /done to lock in.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✓ Lock in", callback_data="lock_morning:0")]])
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard)
        return

    # Mid-day: ask for time estimate so we can set a useful timer
    ai_estimate = await asyncio.to_thread(ai_mod.estimate_task_minutes, desc)
    time_str = f" @ {planned_start}" if planned_start else ""

    if ai_estimate is not None:
        db.update_task_time_estimates(task_id, ai_estimate=ai_estimate)
        prompt = (
            f"✅ Added: *{desc}*{time_str}\n\n"
            f"\U0001f916 AI estimate: ~{ai_estimate} min. How long do you think it'll take? "
            f"(reply with minutes, or /skip)"
        )
    else:
        prompt = (
            f"✅ Added: *{desc}*{time_str}\n\n"
            f"How long do you think this will take? (reply with minutes, or /skip)"
        )

    context.user_data["awaiting_estimate_task_id"] = task_id
    await update.message.reply_text(prompt, parse_mode="Markdown")


# ---------- /done (lock morning plan) ----------

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    today = _today()
    state = db.get_day_state(today)
    if state["morning_done"]:
        await update.message.reply_text("Morning plan already locked in.")
        return
    tasks = db.get_tasks_for_date(today)
    if not tasks:
        await update.message.reply_text("No tasks yet — send your list first.")
        return
    db.set_day_flag(today, "morning_done", 1)
    await update.message.reply_text(_lock_morning_plan_text(tasks))


# ---------- evening reply (free text) ----------

async def handle_evening_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the one-line evening reflection."""
    today = _today()
    state = db.get_day_state(today)
    if not state["morning_done"] or state["evening_done"]:
        return  # not in evening state

    # Only accept after 18:00 local to avoid swallowing daytime messages
    if _local_now().hour < 18:
        return

    text = update.message.text.strip()
    db.log_event("evening_response", payload=text)
    db.set_day_flag(today, "evening_done", 1)
    await update.message.reply_text("Logged. See you tomorrow.")


# ---------- /lesson + /lessons ----------

async def cmd_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    context.user_data["lesson_stage"] = "went_well"
    await update.message.reply_text("What went well today?")


async def cmd_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    lessons = db.get_lessons(limit=7)
    if not lessons:
        await update.message.reply_text("No lessons logged yet. Use /lesson to add one.")
        return
    parts = []
    for lesson in lessons:
        parts.append(f"*{lesson['date']}*")
        parts.append(f"✓ {lesson['went_well']}")
        parts.append(f"△ {lesson['to_improve']}")
        if lesson["learning"]:
            parts.append(f"💡 {lesson['learning']}")
        parts.append("")
    await update.message.reply_text("\n".join(parts).strip(), parse_mode="Markdown")


async def handle_lesson_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stage = context.user_data.get("lesson_stage")
    text = update.message.text.strip()

    if stage == "went_well":
        context.user_data["lesson_went_well"] = text
        context.user_data["lesson_stage"] = "to_improve"
        await update.message.reply_text("What could go better?")

    elif stage == "to_improve":
        context.user_data["lesson_to_improve"] = text
        context.user_data["lesson_stage"] = "learning"
        await update.message.reply_text("What did you learn, if anything? (/skip to leave blank)")

    elif stage == "learning":
        today = _today()
        went_well = context.user_data.pop("lesson_went_well", "")
        to_improve = context.user_data.pop("lesson_to_improve", "")
        context.user_data.pop("lesson_stage", None)
        learning = None if text.lower() in ("skip", "/skip", "") else text
        db.add_lesson(today, went_well, to_improve, learning)
        db.log_event("lesson_logged", payload=today)
        await update.message.reply_text("Lesson logged.")


# ---------- time estimate collection ----------

def _padded_timer(user_estimate: int) -> int:
    """Add ~20% padding to user estimate, rounded to nearest 5 min, minimum +5."""
    import math
    padding = max(5, math.ceil(user_estimate * 0.20 / 5) * 5)
    return user_estimate + padding


def _parse_estimate_minutes(text: str) -> int | None:
    """Parse user's time estimate. Handles '30', '1:30' (1h30m), '1h', '1.5h'."""
    text = text.strip().lower()
    # H:MM format → hours * 60 + minutes
    m = re.fullmatch(r"(\d+):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # Nh or NhMm
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*h(?:r|ours?)?(?:\s*(\d+)\s*m?)?", text)
    if m:
        hours = float(m.group(1))
        mins = int(m.group(2)) if m.group(2) else 0
        return round(hours * 60) + mins
    # Plain number (minutes)
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


async def _handle_user_estimate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = context.user_data.pop("awaiting_estimate_task_id")
    text = update.message.text.strip()
    if text.lower() in ("skip", "/skip", ""):
        await update.message.reply_text("No estimate saved — timer stays at default.")
        return
    minutes = _parse_estimate_minutes(text)
    if minutes is None:
        context.user_data["awaiting_estimate_task_id"] = task_id
        await update.message.reply_text("Enter a number of minutes (e.g. `30`, `1:30`, `1h`), or /skip.", parse_mode="Markdown")
        return
    timer = _padded_timer(minutes)
    db.update_task_time_estimates(task_id, user_estimate=minutes)
    db.update_timer_minutes(task_id, timer)
    task = db.get_task(task_id)
    ai_est = task["ai_estimate_minutes"] if task else None
    if ai_est:
        diff = minutes - ai_est
        sign = "+" if diff >= 0 else ""
        await update.message.reply_text(
            f"Got it — your estimate: {minutes} min (AI: {ai_est} min, diff {sign}{diff}). Timer set to {timer} min."
        )
    else:
        await update.message.reply_text(f"Got it — your estimate: {minutes} min. Timer set to {timer} min.")


async def _handle_actual_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = context.user_data.pop("awaiting_actual_time_task_id")
    text = update.message.text.strip()
    if text.lower() not in ("skip", "/skip", ""):
        digits = re.sub(r"[^\d]", "", text)
        if not digits:
            context.user_data["awaiting_actual_time_task_id"] = task_id
            await update.message.reply_text("Enter minutes (e.g. `45`), or /skip.", parse_mode="Markdown")
            return
        actual = int(digits)
        db.update_task_time_estimates(task_id, actual=actual)
        task = db.get_task(task_id)
        parts = [f"Actual: {actual} min."]
        if task:
            if task["user_estimate_minutes"]:
                d = actual - task["user_estimate_minutes"]
                parts.append(f"You estimated {task['user_estimate_minutes']} min ({'+'if d>=0 else ''}{d}).")
            if task["ai_estimate_minutes"]:
                d = actual - task["ai_estimate_minutes"]
                parts.append(f"AI estimated {task['ai_estimate_minutes']} min ({'+'if d>=0 else ''}{d}).")
        await update.message.reply_text(" ".join(parts))

    context.user_data["awaiting_outcome_task_id"] = task_id
    await update.message.reply_text("Note for next time? (or /skip)")


# ---------- outcome note after Done ----------

async def handle_outcome_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store the optional outcome note after marking a task done."""
    if "awaiting_outcome_task_id" not in context.user_data:
        return False
    task_id = context.user_data.pop("awaiting_outcome_task_id")
    note = update.message.text.strip()
    if note.lower() not in ("skip", "/skip", ""):
        db.update_task_status(task_id, "done", outcome_note=note)
    await update.message.reply_text("Noted.")
    return True


# ---------- free-text router ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    # Lesson collection takes highest priority
    if "lesson_stage" in context.user_data:
        await handle_lesson_response(update, context)
        return

    if "awaiting_estimate_task_id" in context.user_data:
        await _handle_user_estimate(update, context)
        return

    if "awaiting_actual_time_task_id" in context.user_data:
        await _handle_actual_time(update, context)
        return

    # Outcome note
    if "awaiting_outcome_task_id" in context.user_data:
        await handle_outcome_note(update, context)
        return

    today = _today()
    state = db.get_day_state(today)

    if not state["morning_done"]:
        await handle_morning_plan(update, context)
        return

    if not state["evening_done"] and _local_now().hour >= 18:
        await handle_evening_reply(update, context)
        return

    # Plan is locked, not yet evening — give feedback instead of silently ignoring
    await update.message.reply_text(
        "Plan is locked in. Use /today to see your tasks, or /todo to add one."
    )


# ---------- inline callback queries ----------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    query = update.callback_query
    await query.answer()

    action, task_id_str = query.data.split(":", 1)

    # lock_morning doesn't need a task_id
    if action == "lock_morning":
        today = _today()
        state = db.get_day_state(today)
        if state["morning_done"]:
            await query.edit_message_text("Already locked in.")
            return
        tasks = db.get_tasks_for_date(today)
        if not tasks:
            await query.edit_message_text("No tasks to lock in yet.")
            return
        db.set_day_flag(today, "morning_done", 1)
        await query.edit_message_text(_lock_morning_plan_text(tasks))
        return

    task_id = int(task_id_str)
    task = db.get_task(task_id)
    if task is None:
        await query.edit_message_text("Task not found.")
        return

    now_utc = _utc_now()

    # --- start ping responses ---
    if action == "start_yes":
        if task["status"] != "planned":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "started", started_at=now_utc.isoformat())
        db.log_event("timer_started", task_id=task_id)
        endpoint_at = now_utc + timedelta(minutes=task["timer_minutes"])
        _schedule_endpoint_ping(context.application, task_id, endpoint_at)
        await query.edit_message_text(f"Timer running — {task['timer_minutes']} min. Go.")

    elif action == "start_snooze":
        if task["status"] != "planned":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        snooze_at = now_utc + timedelta(minutes=config.SNOOZE_MINUTES)
        _schedule_start_ping(context.application, task_id, snooze_at)
        local_time = snooze_at.astimezone(config.TZ).strftime("%H:%M")
        db.log_event("snoozed", task_id=task_id)
        await query.edit_message_text(f"Pushed {config.SNOOZE_MINUTES}. Next nudge at {local_time}.")

    elif action == "start_skip":
        if task["status"] != "planned":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "skipped")
        db.log_event("task_skipped", task_id=task_id)
        await query.edit_message_text("Skipped — logged, not judged.")

    # --- endpoint ping responses ---
    elif action == "end_done":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "done", completed_at=now_utc.isoformat())
        db.log_event("task_done", task_id=task_id)
        await query.edit_message_text("One down.")
        context.user_data["awaiting_actual_time_task_id"] = task_id
        await context.bot.send_message(
            chat_id=config.CHAT_ID,
            text="How long did that actually take? (minutes, or /skip)",
        )

    elif action == "end_more":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        new_endpoint = now_utc + timedelta(minutes=task["timer_minutes"])
        _schedule_endpoint_ping(context.application, task_id, new_endpoint)
        db.log_event("more_time", task_id=task_id)
        await query.edit_message_text(f"Another {task['timer_minutes']}. Keep going.")

    elif action == "end_stuck":
        if task["status"] != "started":
            await query.edit_message_text(f"Already {task['status']}.")
            return
        db.update_task_status(task_id, "stuck")
        db.log_event("task_stuck", task_id=task_id)
        await query.edit_message_text(
            "Parked. We'll dig into what 'stuck' means in the weekly review. Move on?"
        )
