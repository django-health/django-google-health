import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import responses
import respx
from httpx import Response

from googlehealth import oauth
from googlehealth.constants import (
    API_BASE_URL,
    API_VERSION,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SCOPE_SLEEP_READONLY,
)
from googlehealth.models import ConnectionStatus, GoogleHealthConnection
from googlehealth.schemas import GoogleTokens

SCOPES = [SCOPE_ACTIVITY_AND_FITNESS_READONLY, SCOPE_SLEEP_READONLY]


def _auth_url_params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_build_authorization_url_uses_pkce():
    url, flow_state = oauth.build_authorization_url(scopes=SCOPES)
    params = _auth_url_params(url)

    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["code_challenge_method"] == "S256"
    assert "code_challenge" in params
    assert flow_state.code_verifier is not None
    assert flow_state.state == params["state"]
    assert flow_state.scopes == SCOPES


@responses.activate
def test_exchange_code_happy_path():
    responses.add(
        responses.POST,
        OAUTH_TOKEN_URL,
        json={
            "access_token": "ya29.new",
            "expires_in": 3600,
            "refresh_token": "1//new-refresh",
            "token_type": "Bearer",
            "scope": " ".join(SCOPES),
        },
    )

    tokens = oauth.exchange_code(code="auth-code-abc", scopes=SCOPES)

    assert isinstance(tokens, GoogleTokens)
    assert tokens.access_token == "ya29.new"
    assert tokens.refresh_token == "1//new-refresh"
    assert tokens.scopes == SCOPES


def test_exchange_code_state_mismatch_raises_without_http_call():
    with pytest.raises(oauth.StateMismatchError):
        oauth.exchange_code(
            code="x",
            scopes=SCOPES,
            expected_state="abc",
            received_state="xyz",
        )


@respx.mock
def test_ingest_tokens_creates_connection_and_fetches_user_id(customer):
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity").mock(
        return_value=Response(200, json={"googleUserId": "999-google-id"})
    )

    tokens = GoogleTokens(
        access_token="ya29.x",
        expires_in=3600,
        refresh_token="1//y",
        scope=" ".join(SCOPES),
    )

    conn = oauth.ingest_tokens(customer=customer, tokens=tokens)

    assert conn.google_user_id == "999-google-id"
    assert conn.access_token == "ya29.x"
    assert conn.refresh_token == "1//y"
    assert conn.scopes == SCOPES
    assert conn.status == ConnectionStatus.ACTIVE
    assert GoogleHealthConnection.objects.count() == 1


def test_ingest_tokens_skips_identity_fetch_when_user_id_supplied(customer):
    tokens = GoogleTokens(
        access_token="ya29.x", expires_in=3600, refresh_token="1//y", scope=SCOPES[0]
    )
    # No respx mock set up — proves we don't hit the network.
    conn = oauth.ingest_tokens(
        customer=customer, tokens=tokens, google_user_id="passed-in-id"
    )
    assert conn.google_user_id == "passed-in-id"


@respx.mock
def test_ingest_tokens_updates_existing_connection(customer, connection):
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity").mock(
        return_value=Response(200, json={"googleUserId": connection.google_user_id})
    )
    tokens = GoogleTokens(
        access_token="ya29.rotated",
        expires_in=3600,
        refresh_token="1//rotated",
        scope=SCOPES[0],
    )

    updated = oauth.ingest_tokens(customer=customer, tokens=tokens)

    assert updated.pk == connection.pk
    assert updated.access_token == "ya29.rotated"
    assert GoogleHealthConnection.objects.count() == 1


@respx.mock
def test_refresh_access_token(connection):
    route = respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "ya29.refreshed",
                "expires_in": 3600,
                "scope": " ".join(SCOPES),
                "token_type": "Bearer",
            },
        )
    )

    refreshed = oauth.refresh_access_token(connection)

    assert refreshed.access_token == "ya29.refreshed"
    assert refreshed.refresh_token == "1//initial-refresh"  # unchanged
    assert refreshed.token_expires_at > datetime.now(timezone.utc) + timedelta(
        minutes=50
    )
    # Default (settings) client: the web client's secret must be presented.
    body = {
        k: v[0] for k, v in parse_qs(route.calls[0].request.content.decode()).items()
    }
    assert body["client_id"] == "test-client-id"
    assert body["client_secret"] == "test-client-secret"


@respx.mock
def test_refresh_access_token_public_client_omits_secret(connection):
    """A connection minted by a platform (iOS/Android) client refreshes with its
    own client_id and NO secret — Google rejects a foreign client's secret."""
    connection.client_id = "ios-client-id.apps.googleusercontent.com"
    connection.save(update_fields=["client_id"])
    route = respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "ya29.ios-refreshed",
                "expires_in": 3600,
                "refresh_token": "1//rotated",
                "token_type": "Bearer",
            },
        )
    )

    refreshed = oauth.refresh_access_token(connection)

    body = {
        k: v[0] for k, v in parse_qs(route.calls[0].request.content.decode()).items()
    }
    assert body["client_id"] == "ios-client-id.apps.googleusercontent.com"
    assert "client_secret" not in body
    assert refreshed.access_token == "ya29.ios-refreshed"
    # Rotated refresh token is persisted.
    connection.refresh_from_db()
    assert connection.refresh_token == "1//rotated"


@respx.mock
def test_refresh_access_token_raises_on_error_body(connection):
    """A 200 whose body carries no access token (e.g. an intercepting proxy's
    block page) must raise, not persist garbage."""
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(200, json={"block_message": "blocked"})
    )

    with pytest.raises(oauth.OAuthError):
        oauth.refresh_access_token(connection)

    connection.refresh_from_db()
    assert connection.access_token == "ya29.initial-access"  # untouched


@respx.mock
def test_refresh_access_token_raises_on_non_json_body(connection):
    """A 200 with a non-JSON body (e.g. an intercepting proxy's HTML block page)
    must raise OAuthError, not JSONDecodeError."""
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            text="<html><body>Access denied by proxy</body></html>",
            headers={"content-type": "text/html"},
        )
    )

    with pytest.raises(oauth.OAuthError):
        oauth.refresh_access_token(connection)

    connection.refresh_from_db()
    assert connection.access_token == "ya29.initial-access"  # untouched


@respx.mock
def test_refresh_access_token_raises_on_malformed_json_body(connection):
    """A 200 that has an access_token but is otherwise malformed (missing
    expires_in) must raise OAuthError, not pydantic ValidationError."""
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "ya29.partial"})
    )

    with pytest.raises(oauth.OAuthError):
        oauth.refresh_access_token(connection)

    connection.refresh_from_db()
    assert connection.access_token == "ya29.initial-access"  # untouched


@respx.mock
def test_ingest_tokens_stores_issuing_client_id(customer):
    tokens = GoogleTokens(
        access_token="ya29.x", expires_in=3600, refresh_token="1//y", scope=SCOPES[0]
    )
    conn = oauth.ingest_tokens(
        customer=customer,
        tokens=tokens,
        google_user_id="uid",
        client_id="android-client-id.apps.googleusercontent.com",
    )
    assert conn.client_id == "android-client-id.apps.googleusercontent.com"


def test_get_credentials_uses_connection_client(connection):
    connection.client_id = "ios-client-id.apps.googleusercontent.com"
    creds = oauth.get_credentials(connection)
    assert creds.client_id == "ios-client-id.apps.googleusercontent.com"
    assert creds.client_secret is None


def test_get_credentials_defaults_to_settings_client(connection):
    creds = oauth.get_credentials(connection)
    assert creds.client_id == "test-client-id"
    assert creds.client_secret == "test-client-secret"


@respx.mock
def test_revoke_marks_connection_revoked(connection):
    respx.post(OAUTH_REVOKE_URL).mock(return_value=Response(200))

    oauth.revoke(connection)
    connection.refresh_from_db()

    assert connection.status == ConnectionStatus.REVOKED


@respx.mock
def test_revoke_swallows_http_errors(connection):
    respx.post(OAUTH_REVOKE_URL).mock(return_value=Response(400, json={"error": "x"}))

    oauth.revoke(connection)
    connection.refresh_from_db()

    assert connection.status == ConnectionStatus.REVOKED


def test_is_token_expired_true_when_past_expiry(connection):
    connection.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert connection.is_token_expired() is True


def test_is_token_expired_false_when_well_in_future(connection):
    connection.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert connection.is_token_expired() is False


def test_is_token_expired_respects_leeway(connection):
    connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert connection.is_token_expired(leeway_seconds=60) is True
    assert connection.is_token_expired(leeway_seconds=10) is False


@pytest.mark.live
def test_refresh_against_real_google(db):
    """Refresh a real refresh token against ``oauth2.googleapis.com``.

    Set ``GOOGLE_HEALTH_TEST_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN}`` in env to enable.
    """
    refresh_token = os.getenv("GOOGLE_HEALTH_TEST_REFRESH_TOKEN")
    client_id = os.getenv("GOOGLE_HEALTH_TEST_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_HEALTH_TEST_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        pytest.skip("set GOOGLE_HEALTH_TEST_* env vars to enable")

    from django.conf import settings
    from django.contrib.auth import get_user_model

    settings.GOOGLE_HEALTH_CLIENT_ID = client_id
    settings.GOOGLE_HEALTH_CLIENT_SECRET = client_secret

    User = get_user_model()
    customer = User.objects.create_user(username="live-test")
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="placeholder",
        access_token="placeholder",
        refresh_token=refresh_token,
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[],
    )

    refreshed = oauth.refresh_access_token(conn)

    assert refreshed.access_token.startswith("ya29.")
    assert refreshed.token_expires_at > datetime.now(timezone.utc) + timedelta(
        minutes=50
    )


# ACCOUNT_NOT_LINKED handling (#26) -------------------------------------------
# Real error body captured 2026-08-07 from enterprise Google accounts that
# cannot hold Fitbit profiles: OAuth consent succeeds, every API call 400s.


def _unlinked_body():
    from tests.conftest import load_fixture

    return load_fixture("error_account_not_linked.json")


def _tokens():
    return GoogleTokens(
        access_token="ya29.x",
        expires_in=3600,
        refresh_token="1//y",
        scope=" ".join(SCOPES),
    )


@respx.mock
def test_ingest_tokens_marks_unlinked_account(customer):
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity").mock(
        return_value=Response(400, json=_unlinked_body())
    )

    conn = oauth.ingest_tokens(customer=customer, tokens=_tokens())

    assert conn.status == ConnectionStatus.UNLINKED
    assert conn.google_user_id == ""
    # Tokens are still stored — a reconnect with the right account overwrites.
    assert conn.access_token == "ya29.x"


@respx.mock
def test_ingest_tokens_transient_identity_failure_stays_active(customer):
    """A 500 (or network error) is not evidence of an unlinked account —
    keep today's behavior: active with an empty google_user_id."""
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity").mock(
        return_value=Response(500, json={"error": {"code": 500}})
    )

    conn = oauth.ingest_tokens(customer=customer, tokens=_tokens())

    assert conn.status == ConnectionStatus.ACTIVE
    assert conn.google_user_id == ""


@respx.mock
def test_ingest_tokens_reconnect_after_unlinked_restores_active(customer):
    identity = respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity")
    identity.mock(return_value=Response(400, json=_unlinked_body()))
    conn = oauth.ingest_tokens(customer=customer, tokens=_tokens())
    assert conn.status == ConnectionStatus.UNLINKED

    identity.mock(return_value=Response(200, json={"googleUserId": "right-account"}))
    conn = oauth.ingest_tokens(customer=customer, tokens=_tokens())

    assert conn.status == ConnectionStatus.ACTIVE
    assert conn.google_user_id == "right-account"
    assert GoogleHealthConnection.objects.count() == 1
