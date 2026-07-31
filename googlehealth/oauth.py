"""Google OAuth 2.0 helpers.

Thin layer on top of ``google-auth-oauthlib``. The public API is:

* :func:`build_authorization_url` — produce the consent URL for the web-callback flow.
* :func:`start_mobile_flow` — produce the consent URL for the backend-owned mobile
  flow, persisting the state → customer binding for the public
  :func:`googlehealth.views.mobile_callback` to consume.
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
import re
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

# Google lets users grant a subset of requested scopes; oauthlib treats that as an
# error by default. Relax before importing requests-oauthlib (transitively via
# google-auth-oauthlib below).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import ValidationError

from .constants import (
    API_BASE_URL,
    API_VERSION,
    DEFAULT_SCOPES,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
)
from .models import (
    ConnectionStatus,
    GoogleHealthConnection,
    GoogleHealthOAuthState,
)
from .schemas import GoogleTokens, OAuthFlowState

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """Base for OAuth-related errors raised by this module."""


class StateMismatchError(OAuthError):
    """Raised when the ``state`` returned from Google doesn't match what we stashed."""


DEFAULT_HTTP_TIMEOUT = 10.0


def _http_timeout() -> float:
    """Timeout (seconds) for every outbound OAuth call.

    Override with ``settings.GOOGLE_HEALTH_HTTP_TIMEOUT``. This matters most on
    the token exchange: it runs inside the public mobile callback, i.e. on a
    request a user's browser is blocking on, and Google's token endpoint is
    reachable through whatever corporate proxy sits in front of it (see
    :func:`refresh_access_token`'s notes on middleboxes returning HTML).
    """
    return float(getattr(settings, "GOOGLE_HEALTH_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT))


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


DEFAULT_MOBILE_STATE_TTL_MINUTES = 10

# Matches a private app scheme followed by ':' — RFC 3986 scheme grammar minus
# the http(s) family. Anchored, so "//host" and "/path" (which browsers resolve
# against the current origin, turning the callback's 302 into a redirect off the
# host serving it) don't match.
_APP_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_DISALLOWED_SCHEMES = frozenset(
    {"http", "https", "javascript", "data", "file", "vbscript"}
)
# Storage cap; also the model field's max_length.
MAX_DEEPLINK_LENGTH = 512


def validate_deeplink(deeplink: str) -> str:
    """Validate an app deep link for use in the callback's ``Location`` header.

    The value ends up in a redirect header verbatim, so this is deliberately a
    scheme *allowlist* (anything matching :data:`_APP_SCHEME_RE` and not in
    :data:`_DISALLOWED_SCHEMES`) rather than a denylist of the bad prefixes —
    a lexical "doesn't start with http://" check lets through ``//host``, which
    a browser resolves as ``https://host``.

    Projects can widen or narrow the accepted schemes with
    ``settings.GOOGLE_HEALTH_ALLOWED_DEEPLINK_SCHEMES`` (a list of scheme names
    without the colon); by default any non-web scheme is accepted.

    Raises ``ValueError`` with a specific message for each rejection so the
    caller can surface a useful client error.
    """
    if any(ch in deeplink for ch in "\r\n\x00"):
        # Django would raise BadHeaderError later, from a code path outside the
        # callback's try block — reject it at the door instead.
        raise ValueError("deeplink must not contain control characters")
    if len(deeplink) > MAX_DEEPLINK_LENGTH:
        raise ValueError(f"deeplink must be at most {MAX_DEEPLINK_LENGTH} characters")
    match = _APP_SCHEME_RE.match(deeplink)
    if not match:
        raise ValueError(
            "deeplink must be an absolute URI with an app scheme, e.g. myapp://path"
        )
    scheme = match.group(0)[:-1].lower()
    allowed = getattr(settings, "GOOGLE_HEALTH_ALLOWED_DEEPLINK_SCHEMES", None)
    if allowed is not None:
        if scheme not in {s.lower() for s in allowed}:
            raise ValueError(
                f"deeplink scheme {scheme!r} is not in GOOGLE_HEALTH_ALLOWED_DEEPLINK_SCHEMES"
            )
    elif scheme in _DISALLOWED_SCHEMES:
        raise ValueError(f"deeplink must use a private app scheme, not {scheme!r}")
    return deeplink


def start_mobile_flow(
    customer: AbstractBaseUser,
    *,
    deeplink: str | None = None,
    scopes: list[str] | None = None,
    ttl_minutes: int | None = None,
    prompt: str = "consent",
) -> str:
    """Begin the backend-owned mobile OAuth flow and return the consent URL.

    Call this from an authenticated project-local API endpoint and hand the
    returned URL to the mobile app, which opens it in a system browser
    (ASWebAuthenticationSession / Chrome Custom Tab — do **not** follow it as
    a redirect). Google then sends the user to the public
    :func:`googlehealth.views.mobile_callback`, which resolves the customer
    from the persisted state row and deep-links the result back to the app.

    ``deeplink`` is where the callback 302s the finished user
    (``<deeplink>?status=...``); defaults to
    ``settings.GOOGLE_HEALTH_APP_DEEPLINK``. It must be an absolute URI with a
    private app scheme — see :func:`validate_deeplink`, which rejects web
    schemes, scheme-relative values, control characters and over-long input so
    the callback can't be turned into a redirect off your own host.
    ``scopes`` defaults to ``settings.GOOGLE_HEALTH_DEFAULT_SCOPES``
    (falling back to :data:`googlehealth.constants.DEFAULT_SCOPES`);
    ``ttl_minutes`` to ``settings.GOOGLE_HEALTH_MOBILE_STATE_TTL_MINUTES``
    (falling back to 10).

    Any still-pending state rows for ``customer`` are deleted first so a
    retry or mid-flow abandonment doesn't leave orphan rows behind.
    """
    deeplink = deeplink or getattr(settings, "GOOGLE_HEALTH_APP_DEEPLINK", "")
    if not deeplink:
        raise ImproperlyConfigured(
            "start_mobile_flow needs a deep link: pass deeplink= or set "
            "settings.GOOGLE_HEALTH_APP_DEEPLINK"
        )
    validate_deeplink(deeplink)
    if scopes is None:
        scopes = list(getattr(settings, "GOOGLE_HEALTH_DEFAULT_SCOPES", DEFAULT_SCOPES))
    if ttl_minutes is None:
        ttl_minutes = getattr(
            settings,
            "GOOGLE_HEALTH_MOBILE_STATE_TTL_MINUTES",
            DEFAULT_MOBILE_STATE_TTL_MINUTES,
        )

    GoogleHealthOAuthState.objects.filter(
        customer=customer, consumed_at__isnull=True
    ).delete()

    auth_url, flow_state = build_authorization_url(scopes=scopes, prompt=prompt)
    GoogleHealthOAuthState.objects.create(
        state=flow_state.state,
        code_verifier=flow_state.code_verifier or "",
        scopes=flow_state.scopes,
        deeplink=deeplink,
        customer=customer,
        expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
    )
    return auth_url


def exchange_code(
    *,
    code: str,
    scopes: list[str],
    code_verifier: str | None = None,
    expected_state: str | None = None,
    received_state: str | None = None,
    timeout: float | None = None,
) -> GoogleTokens:
    """Exchange an authorization code for tokens.

    Pass ``expected_state`` and ``received_state`` to enforce CSRF protection at this
    layer; pass neither to skip (e.g. when the upstream view already validated).

    ``timeout`` bounds the token-endpoint request (defaults to
    ``settings.GOOGLE_HEALTH_HTTP_TIMEOUT``, else 10s). Without it
    ``requests_oauthlib`` would block indefinitely, hanging the caller — which
    for the mobile flow is a user-facing redirect.
    """
    if expected_state is not None and received_state != expected_state:
        raise StateMismatchError("OAuth state mismatch")
    flow = _build_flow(scopes, state=expected_state)
    if code_verifier is not None:
        flow.code_verifier = code_verifier
    token_response = flow.fetch_token(
        code=code, timeout=timeout if timeout is not None else _http_timeout()
    )
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
    response = httpx.post(OAUTH_TOKEN_URL, data=body, timeout=_http_timeout())
    if response.status_code >= 400:
        raise OAuthError(
            f"token refresh returned HTTP {response.status_code}: {response.text}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        # Intercepting proxies can also return 200 with an HTML block page.
        raise OAuthError(
            f"token refresh returned non-JSON body: {response.text[:200]!r}"
        ) from exc
    if "access_token" not in payload:
        # Some intermediaries (corporate proxies) return 200 with an error body.
        raise OAuthError(f"token refresh returned no access token: {payload!r}")
    try:
        tokens = GoogleTokens.model_validate(payload)
    except ValidationError as exc:
        raise OAuthError(f"token refresh returned malformed body: {payload!r}") from exc
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
            httpx.post(OAUTH_REVOKE_URL, data={"token": token}, timeout=_http_timeout())
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
        timeout=_http_timeout(),
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
