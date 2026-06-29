"""
Entry point. Wires the Application, registers handlers + daily jobs,
rehydrates today's jobs from DB, then starts long-polling.
"""
import logging
import sys
from datetime import time as dtime

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
    cmd_lesson,
    cmd_lessons,
    handle_text,
    handle_callback,
)
from jobs import morning_prompt, evening_prompt, rehydrate_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    db.init_db()

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    # --- commands ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("silence_today", cmd_silence_today))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("lesson", cmd_lesson))
    app.add_handler(CommandHandler("lessons", cmd_lessons))

    # --- free text ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # --- inline keyboard callbacks ---
    app.add_handler(CallbackQueryHandler(handle_callback))

    # --- daily recurring jobs ---
    mh, mm = config.morning_time()
    eh, em = config.evening_time()

    app.job_queue.run_daily(
        morning_prompt,
        time=dtime(hour=mh, minute=mm, tzinfo=config.TZ),
        name="morning_daily",
    )
    app.job_queue.run_daily(
        evening_prompt,
        time=dtime(hour=eh, minute=em, tzinfo=config.TZ),
        name="evening_daily",
    )

    # --- rehydrate today's one-shot jobs after a restart ---
    rehydrate_jobs(app)
    logger.info("Job rehydration complete.")

    logger.info("Bot starting (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
