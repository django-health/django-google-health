# Notes for Claude (and human contributors)

This project is a reusable Django library — small, focused, with a real
shipping cadence to PyPI. Slice-sized changes; commit messages with the
"why"; tests for every behavior; live calibration over docs guessing.

## Debugging the Google Health API

The published Google Health docs and the v4 discovery doc disagree with
reality in multiple places — see the calibration findings in
`googlehealth/ingest.py` (HK_* identifiers, `_SLEEP_STAGE_MAP`,
`_UNFILTERABLE_DATA_TYPES`, `ROLLUP_ONLY_DATA_TYPES`). When a sync fails or
a mapper looks wrong:

1. Pull the access token from `db.sqlite3` and hit the endpoint directly
   with `httpx` before guessing at code fixes:

   ```python
   import os, django, httpx

   os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")
   django.setup()
   from googlehealth.models import GoogleHealthConnection

   conn = GoogleHealthConnection.objects.first()
   hdr = {"Authorization": f"Bearer {conn.access_token}"}
   # then make whatever GET/POST you need against health.googleapis.com
   ```

2. The discovery doc at
   `https://health.googleapis.com/$discovery/rest?version=v4` is ground
   truth for request/response schemas. Grep the `*Request` / `*Response` /
   `*Value` schemas there before trusting the human-facing docs.

3. Promote findings into code comments + tests, not just commit messages,
   so future probes don't repeat the same dead ends.

## Upstream contributions to django-healthdatamodel

The sister repo `andyreagan/django-healthdatamodel` is where the storage
layer lives. When a change there unblocks this project, you have end-to-end
release autonomy as long as CI is green:

1. Open the PR.
2. Wait for CI green.
3. Bump version in `pyproject.toml` (minor for additive, patch for fix).
4. Squash-merge.
5. Tag `v<X>` on `main`, push the tag — triggers PyPI publish via OIDC.
6. Bump the floor in this repo's `pyproject.toml`, swap any local stopgaps
   for the upstream API, commit + push.

Both repos publish to PyPI via trusted publishing — no manual upload.

## Out of scope for this project

These look like reasonable improvements but the maintainer has explicitly
deferred or rejected them:

- **Token encryption at rest.** Production deployment uses Postgres with
  encryption-at-rest at the storage layer. Don't add Fernet /
  django-cryptography fields to `GoogleHealthConnection`.
- **Signup view in the demo.** `createsuperuser` is the way. The demo
  exists so the maintainer can test against their own Google account; it
  isn't a hosted SaaS shell.
