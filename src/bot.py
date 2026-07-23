"""
Entry point. Wires the Application, registers handlers + daily jobs,
rehydrates today's jobs from DB, then starts long-polling.
"""
import logging
import sys

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import config
import db
from handlers import (
    cmd_start,
    cmd_today,
    cmd_skip,
    cmd_snooze,
    cmd_silence_today,
    cmd_done,
    cmd_help,
    cmd_lesson,
    cmd_lessons,
    cmd_weekly,
    cmd_trends,
    cmd_todo,
    cmd_at,
    cmd_begin,
    handle_text,
    handle_callback,
)
from jobs import schedule_morning, schedule_evening, rehydrate_jobs, check_daily_jobs_scheduled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def _post_init(app) -> None:
    await app.bot.set_my_commands([
        BotCommand("today",         "See today's task list"),
        BotCommand("todo",          "Add a task: /todo Buy milk @ 14:00"),
        BotCommand("begin",         "Start a planned task now"),
        BotCommand("at",            "Set time on task: /at 1 8:30 PM"),
        BotCommand("skip",          "Skip today's morning plan"),
        BotCommand("snooze",        "Snooze the current reminder"),
        BotCommand("silence_today", "No more pings today"),
        BotCommand("done",          "Lock in your morning plan"),
        BotCommand("lesson",        "Log a lesson learned"),
        BotCommand("lessons",       "View past lessons"),
        BotCommand("weekly",        "Weekly review: done/missed/stuck + lessons"),
        BotCommand("trends",        "Estimate vs. actual time trends"),
        BotCommand("help",          "Show all commands"),
    ])


def main() -> None:
    db.init_db()

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # --- commands ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("silence_today", cmd_silence_today))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lesson", cmd_lesson))
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("trends", cmd_trends))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("at", cmd_at))
    app.add_handler(CommandHandler("begin", cmd_begin))

    # --- free text ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # --- inline keyboard callbacks ---
    app.add_handler(CallbackQueryHandler(handle_callback))

    # --- error handler so job failures appear in logs ---
    async def _error_handler(update, context) -> None:
        logger.error("Unhandled exception", exc_info=context.error)
    app.add_error_handler(_error_handler)

    # --- daily prompts (self-rescheduling run_once, avoids APScheduler TZ issues) ---
    schedule_morning(app)
    schedule_evening(app)

    # --- hourly self-heal: catch the morning/evening job silently vanishing ---
    app.job_queue.run_repeating(check_daily_jobs_scheduled, interval=3600, first=3600, name="daily_jobs_healthcheck")

    # --- rehydrate today's one-shot jobs after a restart ---
    rehydrate_jobs(app)
    logger.info("Job rehydration complete.")

    logger.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
