# Vercel and Cloudflare D1 setup

Status: remote schema, Worker deployment and admin creation completed. Login and
ten authenticated pages passed live D1 checks. Vercel configuration, full workflow
testing and live email tests still require completion.

Account: 7bcbe6b2e68e329b6de8d0005b0c96dd
Database: orderflow / 4680a988-a5c6-424b-aee6-46e8a80596d4

1. Sign into the correct account with `wrangler login`, then verify `wrangler whoami`.
2. On the EMPTY database only, run `wrangler d1 execute orderflow --remote --file schema.sql`.
3. Set a random shared secret with `wrangler secret put DB_SECRET` and deploy with `wrangler deploy`.
4. Configure Vercel: D1_WORKER_URL (deployed Worker URL), D1_WORKER_SECRET (same
   secret), SECRET_KEY (different random secret), COOKIE_SECURE=1, CRON_SECRET
   (another random secret), SMTP_HOST=smtp.gmail.com, SMTP_PORT=587,
   SMTP_USER, SMTP_PASSWORD (Google App Password), ALERT_EMAIL_TO.
   Leave DATABASE_URL unset when using D1. Never commit credentials.
5. With the same database environment configured locally, run
   `python -m flask --app app create-admin` for interactive admin setup.
6. Import this project into Vercel as Flask. The configured reminder schedule is
   03:30 UTC (09:00 India time). Scheduling precision depends on the Vercel plan.
7. Test login and a complete partial-production/transfer/receipt/dispatch/payment
   workflow, then test delivery reminders with an explicitly approved recipient.

The existing Railway README describes the previous setup. D1 backups use Cloudflare
Time Travel; do not use the old backup-db CLI against D1.

## Release limitation

The SQLAlchemy D1 adapter auto-commits each statement. Rollback does not undo a
previous remote write. Production, transfer, receipt and dispatch now use a D1
batch to update quantities and history atomically with database-side stock limits.
Other edits, cancellations and concurrent reminder invocations still need concurrency
testing before simultaneous production use. The full workflow is not yet validated.

The existing administrator username and generated password are in the ignored
local `.env` as ADMIN_USERNAME and ADMIN_PASSWORD. These are login credentials;
they do not need to be added to Vercel environment variables.
