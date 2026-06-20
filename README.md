# ADHD Coaching Bot — Sprint 1

Proactive daily task-initiation loop over Telegram. No LLM, no web dashboard.

## Local dev

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, leave TELEGRAM_CHAT_ID blank for now

cd src
python bot.py
# Message the bot /start — it replies with your numeric chat ID
# Paste it into TELEGRAM_CHAT_ID in .env, then restart
```

## Deploy to VM over Tailscale

```bash
# From your laptop, in the project dir
rsync -avz --exclude venv --exclude data --exclude .env \
  ./adhd-bot/ apps@<vm-tailnet-ip>:/opt/apps/adhd-bot/

# On the VM
sudo -iu apps
cd /opt/apps/adhd-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Edit .env with real token; run /start in Telegram to get chat ID
exit

sudo systemctl restart adhd-bot
journalctl -u adhd-bot -f
```

## systemd unit (place at /etc/systemd/system/adhd-bot.service)

```ini
[Unit]
Description=ADHD Coaching Bot
After=network.target

[Service]
User=apps
WorkingDirectory=/opt/apps/adhd-bot
ExecStart=/opt/apps/adhd-bot/venv/bin/python -u src/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Daily loop

| Time | What happens |
|------|-------------|
| 07:00 | Morning ping — send 1–3 must-dos, one per line, with optional `@ HH:MM` |
| Per task | Start ping with Yes / Snooze / Skip buttons |
| Timer end | Endpoint ping with Done / More time / Stuck buttons |
| 21:00 | Evening close — summary + one-line reflection |

## Commands

- `/today` — show today's plan + live status
- `/skip` — skip morning planning
- `/snooze` — snooze the next pending start ping
- `/silence_today` — suppress all remaining pings for today
