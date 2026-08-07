from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import responses
import respx
from httpx import Response

from googlehealth.constants import (
    API_BASE_URL,
    API_VERSION,
    OAUTH_REVOKE_URL,
    OAUTH_TOKEN_URL,
    SCOPE_PROFILE_READONLY,
    SCOPE_SETTINGS_READONLY,
)
from googlehealth.models import ConnectionStatus, GoogleHealthConnection
from googlehealth.views import DEFAULT_SCOPES, SESSION_KEY

pytestmark = pytest.mark.django_db


def test_default_scopes_include_profile_but_not_settings():
    """compute_basal_calories needs profile.readonly for real DOB/gender

    (issue #3); nothing reads unit/timezone settings yet, so that scope
    stays opt-in."""
    assert SCOPE_PROFILE_READONLY in DEFAULT_SCOPES
    assert SCOPE_SETTINGS_READONLY not in DEFAULT_SCOPES


def test_connect_requires_login(client):
    response = client.get("/google-health/connect/")
    assert response.status_code == 302
    assert "/accounts/login/" in response.url or "next=" in response.url


def test_connect_redirects_to_google_and_stashes_state(client, customer):
    client.force_login(customer)
    response = client.get("/google-health/connect/")

    assert response.status_code == 302
    assert response.url.startswith("https://accounts.google.com/")
    stashed = client.session[SESSION_KEY]
    params = {k: v[0] for k, v in parse_qs(urlparse(response.url).query).items()}
    assert stashed["state"] == params["state"]
    assert stashed["code_verifier"] is not None


@respx.mock
@responses.activate
def test_callback_exchanges_code_and_persists_connection(client, customer):
    client.force_login(customer)
    # Prime the session by calling /connect/.
    client.get("/google-health/connect/")
    state = client.session[SESSION_KEY]["state"]

    responses.add(
        responses.POST,
        OAUTH_TOKEN_URL,
        json={
            "access_token": "ya29.cb",
            "expires_in": 3600,
            "refresh_token": "1//cb",
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
        },
    )
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/identity").mock(
        return_value=Response(200, json={"healthUserId": "callback-user"})
    )

    response = client.get(f"/google-health/callback/?code=abc&state={state}")

    assert response.status_code == 302
    assert response.url == "/admin/"
    conn = GoogleHealthConnection.objects.get(customer=customer)
    assert conn.google_user_id == "callback-user"
    assert conn.access_token == "ya29.cb"


def test_callback_returns_400_on_oauth_error(client, customer):
    client.force_login(customer)
    response = client.get("/google-health/callback/?error=access_denied")
    assert response.status_code == 400


def test_callback_returns_400_on_state_mismatch(client, customer):
    client.force_login(customer)
    client.get("/google-health/connect/")  # stashes a state
    response = client.get("/google-health/callback/?code=abc&state=wrong-state")
    assert response.status_code == 400


def test_callback_returns_400_when_no_flow_in_session(client, customer):
    client.force_login(customer)
    response = client.get("/google-health/callback/?code=abc&state=whatever")
    assert response.status_code == 400


@respx.mock
def test_disconnect_revokes_and_redirects(client, customer, connection):
    client.force_login(customer)
    respx.post(OAUTH_REVOKE_URL).mock(return_value=Response(200))

    response = client.post("/google-health/disconnect/")

    assert response.status_code == 302
    connection.refresh_from_db()
    assert connection.status == ConnectionStatus.REVOKED


def test_disconnect_noop_when_no_connection(client, customer):
    client.force_login(customer)
    response = client.post("/google-health/disconnect/")
    assert response.status_code == 302


# --- demo home view: connection-state rendering ---------------------------


@pytest.mark.urls("demo.urls")
def test_home_active_connection_shows_sync(client, customer, connection):
    """An active connection with a live token offers Sync now."""
    client.force_login(customer)
    body = client.get("/").content.decode()
    assert "Sync now" in body
    assert "valid" in body  # token status
    assert "expired" not in body


@pytest.mark.urls("demo.urls")
def test_home_revoked_connection_hides_sync_and_offers_reauth(
    client, customer, connection
):
    """After disconnect (status=revoked) there must be no Sync button — only re-auth.

    Regression: the page used to branch on connection-exists, so a revoked
    connection still rendered "Sync now".
    """
    connection.status = ConnectionStatus.REVOKED
    connection.save(update_fields=["status"])

    client.force_login(customer)
    body = client.get("/").content.decode()
    assert "Sync now" not in body
    assert "Re-authorize" in body
    assert "revoked" in body


@pytest.mark.urls("demo.urls")
def test_home_expired_token_warns_and_offers_reauth(client, customer, connection):
    """An active connection with an expired access token surfaces the expiry
    and offers re-authorization (status=active alone was misleading)."""
    connection.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    connection.save(update_fields=["token_expires_at"])

    client.force_login(customer)
    body = client.get("/").content.decode()
    assert "expired" in body
    assert "Re-authorize" in body


@pytest.mark.urls("demo.urls")
def test_home_no_connection_shows_connect(client, customer):
    client.force_login(customer)
    body = client.get("/").content.decode()
    assert "Connect Google Health" in body
    assert "Sync now" not in body
