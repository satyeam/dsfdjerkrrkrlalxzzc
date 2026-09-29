# RaxiGame Gift Code Bot

## Deploy (GitHub + Railway)
1. Push this folder to a PRIVATE GitHub repo (bot.py contains the test token).
2. Railway -> New Project -> Deploy from GitHub repo.
3. In the same project: New -> Database -> PostgreSQL.
4. Bot service -> Variables: add DATABASE_URL (reference the Postgres service's DATABASE_URL).
   BOT_TOKEN / ADMIN_IDS / MAIN_ADMIN_ID are optional (defaults are in bot.py).
5. Deploy. It runs as a worker (see Procfile).

## Admin (/admin)
- Gift Code Stock: set the code for 50 / 120 / 250 / 500 / 1000 / 2000 Rs
- Edit Welcome Message: text, or photo + caption
- Broadcast: send any message to every user who has opened the bot (with preview + confirm)
- Manage Admins: any admin can add admins; only the main admin (8148605224) can remove them

## Tiers
1,000+ -> 50Rs | 2,000+ -> 120Rs | 5,000+ -> 250Rs
12,000+ -> 500Rs | 25,000+ -> 1000Rs | 50,000+ -> 2000Rs
One claim per UID and per Telegram account per day (resets at midnight IST).
