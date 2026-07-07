"""Google OAuth 2.0 helpers.

Thin layer on top of ``google-auth-oauthlib``. The public API is:

* :func:`build_authorization_url` — produce the consent URL for the web-callback flow.
* :func:`exchange_code` — server-side code → token exchange (with optional PKCE).
* :func:`ingest_tokens` — persist tokens obtained externally (e.g. a mobile app that
  did the OAuth dance and POSTs the resulting token dict to your backend, mirroring
  the wellrider pattern).
* :func:`refresh_access_token` — refresh a stored connection's access token.
* :func:`revoke` — revoke at Google and mark the connection revoked.
* :func:`get_credentials` — build a ``google.oauth2.credentials.Credentials`` for use by
  ``googlehealth.client``.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Google lets users grant a subset of requested scopes; oauthlib treats that as an
# error by default. Relax before importing requests-oauthlib (transitively via
# google-auth-oauthlib below).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import httpx  # noqa: E402
from django.conf import settings  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402

from .constants import (  # noqa: E402
    API_BASE_URL,
    API_VERSION,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
)
from .models import ConnectionStatus, GoogleHealthConnection  # noqa: E402
from .schemas import GoogleTokens, OAuthFlowState  # noqa: E402

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """Base for OAuth-related errors raised by this module."""


class StateMismatchError(OAuthError):
    """Raised when the ``state`` returned from Google doesn't match what we stashed."""


def _client_config() -> dict[str, dict[str, Any]]:
    return {
        "web": {
            "client_id": settings.GOOGLE_HEALTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_HEALTH_CLIENT_SECRET,
            "auth_uri": OAUTH_AUTHORIZATION_URL,
            "token_uri": OAUTH_TOKEN_URL,
            "redirect_uris": [settings.GOOGLE_HEALTH_REDIRECT_URI],
        }
    }


def _build_flow(scopes: list[str], state: str | None = None) -> Flow:
    flow = Flow.from_client_config(_client_config(), scopes=scopes, state=state)
    flow.redirect_uri = settings.GOOGLE_HEALTH_REDIRECT_URI
    return flow


def build_authorization_url(
    *,
    scopes: list[str],
    state: str | None = None,
    prompt: str = "consent",
) -> tuple[str, OAuthFlowState]:
    """Build the consent URL and the state to round-trip via the session.

    Always uses PKCE (S256). ``access_type=offline`` + ``prompt=consent`` together
    guarantee a refresh token even on repeat consents — Google omits ``refresh_token``
    from the response otherwise.
    """
    flow = _build_flow(scopes, state=state)
    code_verifier = secrets.token_urlsafe(64)
    flow.code_verifier = code_verifier
    auth_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt=prompt,
    )
    return auth_url, OAuthFlowState(
        state=returned_state, code_verifier=code_verifier, scopes=scopes
    )


def exchange_code(
    *,
    code: str,
    scopes: list[str],
    code_verifier: str | None = None,
    expected_state: str | None = None,
    received_state: str | None = None,
) -> GoogleTokens:
    """Exchange an authorization code for tokens.

    Pass ``expected_state`` and ``received_state`` to enforce CSRF protection at this
    layer; pass neither to skip (e.g. when the upstream view already validated).
    """
    if expected_state is not None and received_state != expected_state:
        raise StateMismatchError("OAuth state mismatch")
    flow = _build_flow(scopes, state=expected_state)
    if code_verifier is not None:
        flow.code_verifier = code_verifier
    token_response = flow.fetch_token(code=code)
    return GoogleTokens.model_validate(token_response)


def ingest_tokens(
    *,
    customer: AbstractBaseUser,
    tokens: GoogleTokens | dict[str, Any],
    google_user_id: str | None = None,
    now: datetime | None = None,
    client_id: str | None = None,
) -> GoogleHealthConnection:
    """Persist tokens onto a ``GoogleHealthConnection`` (create or update).

    This is the entry point for the "mobile app already did the OAuth dance and is
    shipping us the token dict" pattern. ``google_user_id`` is fetched via
    ``users.getIdentity`` if not provided.

    ``client_id`` is the OAuth client that minted the tokens, when it differs
    from ``settings.GOOGLE_HEALTH_CLIENT_ID`` (e.g. a platform-specific iOS or
    Android public client). Google only honors refresh grants presented by the
    issuing client, so it is stored per connection and used by
    :func:`refresh_access_token`. Omit it for tokens from the default client.
    """
    parsed = (
        tokens
        if isinstance(tokens, GoogleTokens)
        else GoogleTokens.model_validate(tokens)
    )
    if google_user_id is None:
        try:
            google_user_id = _fetch_google_user_id(parsed.access_token)
        except (httpx.HTTPError, OAuthError) as exc:
            # The OAuth flow itself succeeded; identity is only needed for webhook
            # routing. Store empty and let a later step resolve it (e.g. a manual
            # call to _fetch_google_user_id once the API issue is sorted).
            log.warning(
                "users.getIdentity failed (%s) — storing empty google_user_id", exc
            )
            google_user_id = ""

    connection, _ = GoogleHealthConnection.objects.update_or_create(
        customer=customer,
        defaults={
            "google_user_id": google_user_id,
            "client_id": client_id or "",
            "access_token": parsed.access_token,
            "refresh_token": parsed.refresh_token or "",
            "token_expires_at": parsed.expires_at(now=now),
            "scopes": parsed.scopes,
            "status": ConnectionStatus.ACTIVE,
        },
    )
    return connection


def _connection_client(connection: GoogleHealthConnection) -> tuple[str, str | None]:
    """Resolve the (client_id, client_secret) pair for a connection's tokens.

    A connection carrying its own ``client_id`` was minted by a platform
    client (iOS/Android). Those are public clients: Google's token endpoint
    accepts refresh grants from them with no secret, and sending the *web*
    client's secret alongside a foreign client_id would be rejected — so the
    secret is only attached for the default (settings) client.
    """
    client_id = connection.client_id or settings.GOOGLE_HEALTH_CLIENT_ID
    if client_id == settings.GOOGLE_HEALTH_CLIENT_ID:
        return client_id, settings.GOOGLE_HEALTH_CLIENT_SECRET
    return client_id, None


def refresh_access_token(connection: GoogleHealthConnection) -> GoogleHealthConnection:
    """Refresh the connection's access token in place using its stored refresh token.

    Talks to the token endpoint directly (httpx) rather than via
    ``google.oauth2.credentials.Credentials.refresh``: google-auth puts
    ``client_secret`` in the request body unconditionally, which breaks
    secretless refresh for connections minted by public (iOS/Android) clients.
    """
    client_id, client_secret = _connection_client(connection)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": connection.refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        body["client_secret"] = client_secret
    response = httpx.post(OAUTH_TOKEN_URL, data=body, timeout=10.0)
    if response.status_code >= 400:
        raise OAuthError(
            f"token refresh returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    if "access_token" not in payload:
        # Some intermediaries (corporate proxies) return 200 with an error body.
        raise OAuthError(f"token refresh returned no access token: {payload!r}")
    tokens = GoogleTokens.model_validate(payload)
    connection.access_token = tokens.access_token
    connection.token_expires_at = tokens.expires_at()
    update_fields = ["access_token", "token_expires_at"]
    if tokens.refresh_token:
        # Google may rotate the refresh token; keep the newest one.
        connection.refresh_token = tokens.refresh_token
        update_fields.append("refresh_token")
    connection.save(update_fields=update_fields)
    return connection


def revoke(connection: GoogleHealthConnection) -> None:
    """Revoke the connection at Google and mark it ``REVOKED`` locally.

    Best-effort: a non-2xx from Google still flips the local status — the user-facing
    intent (disconnect) shouldn't be blocked by a transient Google error.
    """
    token = connection.refresh_token or connection.access_token
    if token:
        try:
            httpx.post(OAUTH_REVOKE_URL, data={"token": token}, timeout=10.0)
        except httpx.HTTPError:
            pass
    connection.status = ConnectionStatus.REVOKED
    connection.save(update_fields=["status"])


def get_credentials(connection: GoogleHealthConnection) -> Credentials:
    """Build a ``google.oauth2.credentials.Credentials`` for use with google-auth.

    Prefer :func:`refresh_access_token` for refreshing: it persists the result
    and, unlike ``creds.refresh(Request())``, handles connections minted by
    public (secretless) platform clients correctly.
    """
    client_id, client_secret = _connection_client(connection)
    return Credentials(
        token=connection.access_token,
        refresh_token=connection.refresh_token or None,
        token_uri=OAUTH_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(connection.scopes),
    )


def _fetch_google_user_id(access_token: str) -> str:
    """Call ``users.getIdentity`` to resolve the Google Health user ID for a token."""
    response = httpx.get(
        f"{API_BASE_URL}/{API_VERSION}/users/me/identity",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if response.status_code >= 400:
        # raise_for_status drops the response body; we want it visible.
        raise OAuthError(
            f"users.getIdentity returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    user_id = payload.get("googleUserId") or payload.get("healthUserId")
    if not user_id:
        raise OAuthError(f"users.getIdentity returned no user id: {payload!r}")
    return str(user_id)
