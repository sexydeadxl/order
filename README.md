# OrderFlow — Factory Operations

Current deployment: Flask on Vercel, Cloudflare D1 through an authenticated Worker,
and Gmail SMTP reminders. See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration.
The Railway instructions below describe the original deployment only.

Never commit `.env`. Configure production secrets in Vercel before deploying.

## What changed
- Generic orders without any payment fields. Payment entries stay in the Accounts-only area.
- Edit/cancel orders (cancellation is blocked after production has started to avoid invalidating stock history).
- Partial production, transfer-to-selling, receipt, and dispatch, all with quantity validation and activity audit.
- Delivery dashboard and **Factory Work List**: ordinary future orders; reminder at 3 days, red alerts at 2, 1, today, and overdue. Unfinished partial deliveries stay visible.
- Order CSV download; separate payment tracking. Role restrictions: Admin all, Production quantities, Selling orders/receipt/dispatch, Accounts payments/reports.
- Google Calendar OAuth sync on order create/edit. When fully dispatched or cancelled, deletion of the linked calendar event is attempted. Failed sync is logged, and retry is available in Orders.
- CLI email reminders and CLI database backups. These **do not run automatically** unless you set up an external scheduler. WhatsApp/SMS are **not integrated**.

## Fresh Railway deployment
1. Create a private repo, connect it to Railway Hobby. Add a Railway PostgreSQL service and set `DATABASE_URL=${{Postgres.DATABASE_URL}}` in the web service. Set a random long `SECRET_KEY` and `COOKIE_SECURE=1`.
2. The included `railway.json` starts Gunicorn, and `requirements.txt` declares dependencies.
3. Run `flask --app app init-db` once, and `flask --app app create-admin` in a console that supports interactive input. Password must be 12+ characters. Never use demo credentials.
4. Configure a public domain/HTTPS, confirm `/health` and login, and test a sample 10-piece order through 4 produced, 4 sent, 4 received, 2 dispatched and remaining balances.
5. Keep database backups outside ephemeral containers and configure Railway monitoring.

## Upgrade from an existing v4 database (IMPORTANT)
- **Back up PostgreSQL before running anything.** Do not delete/recreate your existing DB.
- Deploy the new source; run `flask --app app migrate-db` before serving the upgraded code. This adds missing quantity columns, cancellation flag and Calendar error column and creates new log tables.
- Previously completed production/transfers/dispatch are NOT auto-converted to quantities. Reconcile previously active orders before allowing new movements. A default of zero is conservative and does not prove old work never happened.
- Keep the same `DATABASE_URL` and `SECRET_KEY` to preserve existing records/sessions. A separate test/staging database is strongly recommended first.

## Email reminders (optional)
Set `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (optional) and `ALERT_EMAIL_TO`. Run `flask --app app send-delivery-reminders` once per day with an external scheduler, e.g. a dedicated Railway scheduled service. One email lists undelivered orders due in <=3 days; results are deduplicated once/day in `reminder_log`. On SMTP failure the command exits with an error, and the next run can retry. Without SMTP credentials, only dashboard notifications work.

## Backups (optional CLI)
Run `flask --app app backup-db` from a machine with `pg_dump` installed for PostgreSQL or for local SQLite. Backups are written under `BACKUP_DIR` (default `backups/`). Set `BACKUP_S3_BUCKET`, AWS credentials, region, optionally `BACKUP_S3_ENDPOINT_URL` and `BACKUP_S3_PREFIX` to upload each backup to S3-compatible **offsite storage**. Schedule a separate Railway cron service to execute `flask --app app backup-db` daily. Install `pg_dump` inside the cron image for PostgreSQL backup. Test restore procedures. Without external storage this command creates an EPHEMERAL backup and does NOT protect Railway data. Test restore procedures.

## Google Calendar (optional)
Configure Google Calendar API and Google OAuth **web app** client in Google Cloud Console. Redirect URI: `https://YOUR-DOMAIN/calendar/callback`. Add Railway variables `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, login as Admin, connect under Orders. Access tokens are stored in database; restrict access and protect backups. Connected account primary calendar is used. Google notifications need to be enabled separately on users' devices. Testing-mode refresh tokens may expire; reconnect if needed.

## Limitations to address before sensitive production use
- Code compiles; full browser, permission, concurrency, Railway deployment, third-party OAuth and SMTP tests are still required. No guarantee of uninterrupted service.
- A highly concurrent multi-worker workload requires transactional row locks for quantity updates and robust schema-migration tooling (e.g. Alembic). For initial use keep conservative worker concurrency and monitor.
- Legacy paid/closed orders need manual reconciliation after schema upgrade. Multi-line item orders and stock valuation are not yet included.
- No automatic WhatsApp/SMS sending. Email reminders require SMTP and a scheduler. Backups require a scheduler and offsite storage.
