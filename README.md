# django-google-health

[![CI](https://github.com/andyreagan/django-google-health/actions/workflows/ci.yml/badge.svg)](https://github.com/andyreagan/django-google-health/actions/workflows/ci.yml)

A reusable Django app for the [Google Health API](https://developers.google.com/health) — the successor to the Fitbit Web API. Handles the Google OAuth 2.0 flow, fetches user health data from `health.googleapis.com`, and persists it through [`django-healthdatamodel`](https://github.com/andyreagan/django-healthdatamodel) so the same storage and query layer serves Apple Health, Fitbit, and Google Health side-by-side.

> Google recommends launching new integrations **after the end of May 2026** to align with legacy Fitbit account deprecation. See `docs/google-health/get-started.md`.

## Status

Early scaffolding. The package, demo project, OAuth model, and CI are in place. OAuth views, the HTTP client, ingest mapping, and webhook handling are stubbed and will land in follow-up slices.

## Install

```
pip install django-google-health
```

Add both this app and `django-healthdatamodel` to `INSTALLED_APPS`, then run migrations:

```python
INSTALLED_APPS = [
    ...
    "healthdatamodel",
    "googlehealth",
]
```

```
python manage.py migrate
```

The model uses `settings.AUTH_USER_MODEL` so it works with any custom user model.

## Configuration

```python
GOOGLE_HEALTH_CLIENT_ID = "..."        # from Google Cloud Console
GOOGLE_HEALTH_CLIENT_SECRET = "..."
GOOGLE_HEALTH_REDIRECT_URI = "https://your-app.example.com/google-health/callback"
```

Set up the OAuth client in [Google Cloud Console](https://console.cloud.google.com/) and enable the Google Health API. See `docs/google-health/codelabs-make-your-first-api-call.md` for a step-by-step walkthrough.

## Mobile (backend-owned) OAuth flow

The session views (`connect`/`callback`) assume a logged-in browser. Mobile
apps get a backend-owned flow instead — tokens are minted by your
confidential web client (so they stay refreshable server-side), and no Django
session is needed:

1. Your **authenticated API endpoint** (DRF view, FastAPI route, …) calls
   `googlehealth.oauth.start_mobile_flow(customer, deeplink="yourapp://google-health")`
   and returns the consent URL to the app.
2. The app opens the URL in a system browser
   (ASWebAuthenticationSession / Chrome Custom Tab — don't follow it as a
   redirect).
3. Google redirects to the **public** `googlehealth.views.mobile_callback`
   (`google-health/mobile/callback/`, URL name
   `googlehealth:mobile_callback`) — the customer is resolved from a
   single-use, TTL-bounded `GoogleHealthOAuthState` row, not a session.
   Point `GOOGLE_HEALTH_REDIRECT_URI` (and the Google Cloud client's
   authorized redirect URI) at wherever you serve it.
4. The callback 302s the browser to the app's deep link:
   `<deeplink>?status=success|denied|error[&reason=...]`.
5. On success the `googlehealth.signals.mobile_connected` signal fires with
   `customer` and `connection` — hook it to activate the data source, kick
   off a first sync, etc.

Related settings (all optional):

```python
GOOGLE_HEALTH_APP_DEEPLINK = "yourapp://google-health"  # default deep link; must be a non-http(s) app scheme
GOOGLE_HEALTH_MOBILE_STATE_TTL_MINUTES = 10             # state row time-to-live
GOOGLE_HEALTH_DEFAULT_SCOPES = [...]                    # shared with the session flow
```

The API endpoint and the callback can run in separate deployments (e.g. a
Lambda API and a Kubernetes web tier) — they only need to share the database.

## Scopes

Google Health scopes are namespaced under `https://www.googleapis.com/auth/googlehealth.*`. The complete list lives in `googlehealth.constants` and is documented in `docs/google-health/scopes.md`. Examples:

- `googlehealth.activity_and_fitness.readonly` — steps, distance, exercise, floors, altitude
- `googlehealth.health_metrics_and_measurements.readonly` — heart rate, weight, body fat, SpO2
- `googlehealth.sleep.readonly` — sleep stages and sessions
- `googlehealth.location.readonly` — exercise GPS
- `googlehealth.profile.readonly` — DOB and gender, used by `compute_basal_calories` for
  a real Mifflin-St Jeor BMR instead of a median fallback

`DEFAULT_SCOPES` (overridable via `GOOGLE_HEALTH_DEFAULT_SCOPES`) requests
activity_and_fitness, health_metrics_and_measurements, profile, and sleep,
all readonly. **Existing connections must disconnect and reconnect** to pick
up a newly added scope — a stored refresh token doesn't gain scopes
retroactively, so `get_profile` will keep 403ing for anyone who connected
before this scope was added.

## Storage

This app does **not** define `Record` / `Workout` tables — those live in `django-healthdatamodel`. The `googlehealth.ingest` module maps Google Health API responses to `healthdatamodel.schemas.RecordInput` and `WorkoutInput`, then calls `healthdatamodel.ingest.ingest_records` to persist them. Read the data back with `healthdatamodel.query.*` (see that project's docs).

The models defined here are `GoogleHealthConnection` (per-user OAuth tokens, granted scopes, connection status, and last sync timestamp) and `GoogleHealthOAuthState` (short-lived single-use state rows for the mobile flow).

## Documentation

The Google Health API documentation is vendored as Markdown under `docs/google-health/` so it's grep-able offline:

- `get-started.md` — overview, benefits, getting started paths
- `migration.md` — Fitbit Web API → Google Health API migration guide
- `data-types.md` — every data type with operations and scopes
- `scopes.md` — OAuth scopes
- `webhooks.md` — subscriber registration, endpoint verification, notification payloads
- `codelabs-make-your-first-api-call.md` — end-to-end OAuth + first API call
- `reference-rest.md` — REST resource index
- `migration-parity-tool.md` — parity tool reference
- `support.md` — issue tracker and forum links

## Try it on your own data

The repo includes a runnable demo Django project at `demo/` that takes you
through the full OAuth flow and syncs your Google Health data into
`healthdatamodel`. The same OAuth setup also unlocks the
`@pytest.mark.live` integration tests.

### 1. Set up a Google Cloud OAuth client (one-time)

Walkthrough in `docs/google-health/codelabs-make-your-first-api-call.md`. The
specifics that matter for the demo:

- **Application type:** Web application.
- **Authorized redirect URI:** `http://localhost:8000/google-health/callback/`
  (exact match — Google compares byte-for-byte, including the trailing slash).
- Under **Audience**, set publishing status to **Testing** and add your
  Google account as a **Test user**.
- Under **Data Access**, add the scopes you want. A good starter set:
  `googlehealth.activity_and_fitness.readonly`,
  `googlehealth.health_metrics_and_measurements.readonly`,
  `googlehealth.profile.readonly`,
  `googlehealth.sleep.readonly`.

**One real-world prerequisite:** the Google Health API serves data from a
Fitbit profile. Install the Fitbit mobile app, sign in with the same Google
account, and (optionally) log a manual activity so there's something to fetch.
Without this, even authenticated calls return `400 The account is not linked
to Google Health.` (See issue
[#2](https://github.com/andyreagan/django-google-health/issues/2) for the
follow-up around backfilling identity after the link is created.)

### 2. Run the demo

```
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
export GOOGLE_HEALTH_CLIENT_ID=...
export GOOGLE_HEALTH_CLIENT_SECRET=...
# oauthlib refuses non-HTTPS redirect URIs by default. Fine for local dev:
export OAUTHLIB_INSECURE_TRANSPORT=1
uv run python manage.py runserver
```

Open <http://localhost:8000/>, sign in with the superuser you just created,
then:

- Click **Connect Google Health** → consent on Google → land back on the
  homepage with a `GoogleHealthConnection` saved for your user.
- Pick a window + resolution and click **Sync now** to fetch and persist
  records.
- Browse the resulting rows at `/admin/healthdatamodel/record/`
  (and `.../workout/`).

If you'd rather drive sync from the terminal:

```
uv run python manage.py sync_google_health --user <your-username> --days 7
```

### 3. (Optional) Enable the live integration tests

A handful of tests are marked `@pytest.mark.live` and hit the real
`health.googleapis.com`. They self-skip unless three env vars are set.
After step 2 has run at least once, the demo's OAuth round-trip has already
deposited a long-lived `refresh_token` in `db.sqlite3` — reuse it:

```
export GOOGLE_HEALTH_TEST_CLIENT_ID=$GOOGLE_HEALTH_CLIENT_ID
export GOOGLE_HEALTH_TEST_CLIENT_SECRET=$GOOGLE_HEALTH_CLIENT_SECRET
export GOOGLE_HEALTH_TEST_REFRESH_TOKEN=$(sqlite3 db.sqlite3 \
    "SELECT refresh_token FROM googlehealth_googlehealthconnection LIMIT 1;")
uv run pytest tests/ -v -m live
```

The default `pytest` run still skips them.

A separate scheduled workflow (`.github/workflows/live.yml`) runs these
tests nightly against the real API, gated on three repo secrets of the
same names: `GOOGLE_HEALTH_TEST_CLIENT_ID`, `GOOGLE_HEALTH_TEST_CLIENT_SECRET`,
and `GOOGLE_HEALTH_TEST_REFRESH_TOKEN`. Until those secrets are configured
on the repo (and on forks, where they're never available), the job checks
for them and exits neutrally instead of failing.

**Token-expiry caveat (as of 2026-07-22):** while the Google Cloud OAuth
consent screen for this app is in "testing" mode, Google expires refresh
tokens after 7 days regardless of use — the 6-month idle-expiry behavior
people usually expect only kicks in once the consent screen is verified
and published to production. Until then, `GOOGLE_HEALTH_TEST_REFRESH_TOKEN`
needs re-minting roughly weekly (repeat the `sqlite3` command above after a
fresh OAuth round-trip). If the nightly job starts failing with
`invalid_grant`, re-mint the token before assuming the code regressed.

## Development

```
uv sync --group dev
uv run pytest tests/ -v
uv run pre-commit run --all-files
```
