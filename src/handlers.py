"""
Telegram handlers: commands, morning/evening free text, inline callback queries.
"""
import logging
import re
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

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
    re.compile(r"@\s*(\d{1,2}):(\d{2})\s*$"),          # @ 10:30 or @10:30
    re.compile(r"@\s*(\d{1,2})\s*$"),                    # @10
    re.compile(r"\bat\s+(\d{1,2}):(\d{2})\s*$", re.I),  # at 10:30
    re.compile(r"\bat\s+(\d{1,2})\s*$", re.I),          # at 10
    re.compile(r"@\s*(\d{1,2})(am|pm)\s*$", re.I),      # @2pm
    re.compile(r"\bat\s+(\d{1,2})(am|pm)\s*$", re.I),   # at 2pm
]


def _parse_task_line(line: str) -> tuple[str, str | None]:
    """Return (description, planned_start_HH:MM or None)."""
    line = line.strip()
    for pat in _TIME_PATTERNS:
        m = pat.search(line)
        if m:
            desc = line[:m.start()].strip().rstrip("@").strip()
            groups = m.groups()
            if len(groups) == 2 and groups[1] in ("am", "pm", "AM", "PM"):
                hour = int(groups[0])
                if groups[1].lower() == "pm" and hour != 12:
                    hour += 12
                if groups[1].lower() == "am" and hour == 12:
                    hour = 0
                return desc, f"{hour:02d}:00"
            elif len(groups) == 2:
                return desc, f"{int(groups[0]):02d}:{groups[1]}"
            else:
                return desc, f"{int(groups[0]):02d}:00"
    return line, None


# ---------- /start ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.log_event("command", payload="/start")
    await update.message.reply_text(
        f"Hey. Your chat ID is `{chat_id}`.\n\n"
        "Paste that into `TELEGRAM_CHAT_ID` in your `.env`, then restart the bot.\n\n"
        "I'll ping you at your morning and evening times to run the daily loop.",
        parse_mode="Markdown",
    )


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

async def handle_morning_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse the user's free-text morning plan into tasks."""
    today = _today()
    state = db.get_day_state(today)
    if state["morning_done"]:
        return  # already done — fall through to generic handler

    lines = [l.strip() for l in update.message.text.strip().splitlines() if l.strip()]
    if not lines:
        return

    if len(lines) > 3:
        lines = lines[:3]
        capped = True
    else:
        capped = False

    now_local = _local_now()
    locked_lines = ["Locked in:"]
    task_count = 0

    for line in lines:
        desc, planned_start = _parse_task_line(line)
        if not desc:
            continue

        # Skip if the planned start has already passed today
        if planned_start:
            h, m = planned_start.split(":")
            start_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if start_local <= now_local:
                planned_start = None  # treat as unscheduled

        task_id = db.add_task(today, desc, planned_start, config.DEFAULT_TIMER_MINUTES)
        db.log_event("task_added", task_id=task_id, payload=desc)
        task_count += 1

        time_label = planned_start if planned_start else "unscheduled"
        locked_lines.append(f"{task_count}. {desc} — {time_label}")

        if planned_start:
            h, m = planned_start.split(":")
            run_at_local = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            run_at_utc = run_at_local.astimezone(timezone.utc)
            _schedule_start_ping(context.application, task_id, run_at_utc)

    if task_count == 0:
        await update.message.reply_text("Couldn't parse that — send one task per line, e.g. `Call dentist @ 14:00`.", parse_mode="Markdown")
        return

    db.set_day_flag(today, "morning_done", 1)

    any_scheduled = any(t["planned_start"] for t in db.get_tasks_for_date(today))
    if any_scheduled:
        locked_lines.append("I'll nudge you at each start time.")
    else:
        locked_lines.append("No start times set — you're on your own schedule today.")

    if capped:
        locked_lines.append("\nCapped at 3 — the rest can wait. Finishing beats listing.")

    await update.message.reply_text("\n".join(locked_lines))


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

    # Outcome note takes highest priority
    if "awaiting_outcome_task_id" in context.user_data:
        await handle_outcome_note(update, context)
        return

    today = _today()
    state = db.get_day_state(today)

    if not state["morning_done"]:
        await handle_morning_plan(update, context)
        return

    if state["morning_done"] and not state["evening_done"] and _local_now().hour >= 18:
        await handle_evening_reply(update, context)
        return


# ---------- inline callback queries ----------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    query = update.callback_query
    await query.answer()

    action, task_id_str = query.data.split(":", 1)
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
        # Ask for optional outcome note
        context.user_data["awaiting_outcome_task_id"] = task_id
        await context.bot.send_message(
            chat_id=config.CHAT_ID,
            text="Note for next time? (or /skip)",
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
