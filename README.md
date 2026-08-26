# Karaoke Queue Telegram Bot

A single-venue karaoke queue bot. Users DM the bot to join the queue, check
their status, and get notified when it's their turn. Admins (anyone with the
password) can manage the queue and end the session.

## Setup

1. **Create the bot** — message [@BotFather](https://t.me/BotFather) on
   Telegram, run `/newbot`, and copy the token it gives you.

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up PostgreSQL** — any Postgres instance works (local, Railway,
   Fly.io, Supabase, etc). Tables are created automatically on first run.

4. **Configure environment variables** — copy `.env.example` to `.env` and
   fill in:

   ```
   TELEGRAM_BOT_TOKEN=...
   DATABASE_URL=postgresql://user:password@host:5432/dbname
   ADMIN_PASSWORD=...
   ```

5. **Run it**

   ```bash
   python -m bot.main
   ```

## Deploying (Railway / Fly.io / similar)

- Push this repo to your PaaS of choice.
- Add a PostgreSQL addon/database and copy its connection string into
  `DATABASE_URL`.
- Set `TELEGRAM_BOT_TOKEN` and `ADMIN_PASSWORD` as env vars/secrets.
- The included `Procfile` runs the bot as a worker process (it uses polling,
  not webhooks, so no public URL is needed).

## Commands

**Everyone (private chat with the bot):**
- `/start` — instructions
- `/add <name> - <song> - <artist> - <youtube_link>` — join the queue
- `/status` — your position and who's ahead of you
- `/skip` — remove yourself from the queue
- `/done` — mark your performance complete (advances the queue)
- `/history` — songs performed so far tonight

**Admins:**
- `/admin <password>` — authenticate
- `/queue` — full queue with details
- `/remove <position or name>` — remove a specific submission
- `/end` — clear the queue and history for a new session

## Notes on design choices

- **Submission limits**: 2 songs per person while the queue has fewer than
  10 entries, 1 song per person once it hits 10+.
- **Notifications**: when `/done` or `/skip` advances the queue, the bot DMs
  whoever is now first — this only works if that user has started a chat
  with the bot at least once (a Telegram requirement, not something the bot
  can work around).
- **Admin sessions** persist across restarts (stored in Postgres), so admins
  don't need to re-authenticate unless you manually clear the table.
