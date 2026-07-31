"""Tests for the backend-owned mobile OAuth flow.

Covers the three pieces end-to-end at the unit level:

* ``models.consume`` — single-use + expiry invariants of the state store.
* ``oauth.start_mobile_flow`` — consent-URL helper: state persistence,
  deep-link validation, defaults from settings, stale-row cleanup.
* ``views.mobile_callback`` — the public callback: deep-link result codes,
  state resolution, token ingest, and the ``mobile_connected`` signal.
"""

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.utils import timezone

from googlehealth import oauth
from googlehealth.constants import SCOPE_PROFILE_READONLY, SCOPE_SLEEP_READONLY
from googlehealth.models import (
    ConnectionStatus,
    GoogleHealthConnection,
    GoogleHealthOAuthState,
    consume,
)
from googlehealth.schemas import GoogleTokens
from googlehealth.signals import mobile_connected

CALLBACK_URL = "/google-health/mobile/callback/"


def _make_state(
    customer,
    *,
    state="teststate123",
    deeplink="demoapp://google-health",
    expired=False,
    consumed=False,
):
    now = timezone.now()
    return GoogleHealthOAuthState.objects.create(
        state=state,
        code_verifier="verifier",
        scopes=[SCOPE_SLEEP_READONLY],
        deeplink=deeplink,
        customer=customer,
        expires_at=now - timedelta(minutes=1)
        if expired
        else now + timedelta(minutes=10),
        consumed_at=now if consumed else None,
    )


class TestConsume:
    def test_returns_record_and_marks_consumed(self, customer):
        _make_state(customer)
        rec = consume("teststate123")
        assert rec is not None
        assert rec.state == "teststate123"
        assert rec.consumed_at is not None

    def test_single_use_second_call_returns_none(self, customer):
        _make_state(customer)
        assert consume("teststate123") is not None
        assert consume("teststate123") is None

    def test_expired_state_returns_none_and_row_survives(self, customer):
        _make_state(customer, expired=True)
        assert consume("teststate123") is None
        # Expired rows are not deleted — they just aren't returned.
        assert GoogleHealthOAuthState.objects.filter(state="teststate123").exists()

    def test_already_consumed_returns_none(self, customer):
        _make_state(customer, consumed=True)
        assert consume("teststate123") is None

    def test_unknown_state_returns_none(self, db):
        assert consume("doesnotexist") is None


class TestStartMobileFlow:
    def test_returns_consent_url_and_persists_state(self, customer):
        url = oauth.start_mobile_flow(customer, deeplink="demoapp://google-health")

        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        params = parse_qs(urlparse(url).query)
        rec = GoogleHealthOAuthState.objects.get(customer=customer)
        assert params["state"] == [rec.state]
        assert params["access_type"] == ["offline"]
        assert rec.code_verifier
        assert rec.deeplink == "demoapp://google-health"
        assert rec.consumed_at is None
        assert rec.expires_at > timezone.now() + timedelta(minutes=9)

    def test_deeplink_defaults_to_settings(self, customer):
        oauth.start_mobile_flow(customer)
        rec = GoogleHealthOAuthState.objects.get(customer=customer)
        assert rec.deeplink == "demoapp://google-health"

    @override_settings(GOOGLE_HEALTH_APP_DEEPLINK="")
    def test_no_deeplink_anywhere_raises(self, customer):
        with pytest.raises(ImproperlyConfigured):
            oauth.start_mobile_flow(customer)

    def test_http_deeplink_rejected(self, customer):
        # Open-redirect guard: the callback 302s wherever the row points.
        with pytest.raises(ValueError):
            oauth.start_mobile_flow(customer, deeplink="https://evil.example.com/")

    def test_default_scopes_include_profile_readonly(self, customer):
        # compute_basal_calories needs profile.readonly for a real BMR; a
        # mobile scope set without it 403s later during sync.
        oauth.start_mobile_flow(customer)
        rec = GoogleHealthOAuthState.objects.get(customer=customer)
        assert SCOPE_PROFILE_READONLY in rec.scopes

    def test_stale_pending_rows_cleaned_up_consumed_rows_kept(self, customer):
        _make_state(customer, state="pending_old")
        _make_state(customer, state="consumed_old", consumed=True)

        oauth.start_mobile_flow(customer)

        assert not GoogleHealthOAuthState.objects.filter(state="pending_old").exists()
        assert GoogleHealthOAuthState.objects.filter(state="consumed_old").exists()
        assert GoogleHealthOAuthState.objects.filter(customer=customer).count() == 2

    @override_settings(GOOGLE_HEALTH_MOBILE_STATE_TTL_MINUTES=2)
    def test_ttl_from_settings(self, customer):
        oauth.start_mobile_flow(customer)
        rec = GoogleHealthOAuthState.objects.get(customer=customer)
        assert rec.expires_at < timezone.now() + timedelta(minutes=3)


@pytest.mark.django_db
class TestMobileCallback:
    def test_access_denied_redirects_denied(self, customer):
        _make_state(customer)
        resp = Client().get(
            CALLBACK_URL, {"error": "access_denied", "state": "teststate123"}
        )
        assert resp.status_code == 302
        assert resp["Location"] == "demoapp://google-health?status=denied"

    def test_other_google_error_redirects_google_error(self, customer):
        _make_state(customer)
        resp = Client().get(
            CALLBACK_URL, {"error": "server_error", "state": "teststate123"}
        )
        assert resp.status_code == 302
        assert "status=error" in resp["Location"]
        assert "reason=google_error" in resp["Location"]

    def test_missing_state_uses_fallback_deeplink(self, customer):
        resp = Client().get(CALLBACK_URL, {"code": "abc"})
        assert resp.status_code == 302
        assert resp["Location"].startswith("demoapp://google-health?")
        assert "reason=state_invalid" in resp["Location"]

    @override_settings(GOOGLE_HEALTH_APP_DEEPLINK="")
    def test_unresolvable_state_without_fallback_is_400(self, customer):
        resp = Client().get(CALLBACK_URL, {"code": "abc", "state": "notexist"})
        assert resp.status_code == 400

    def test_unknown_state_redirects_state_invalid(self, customer):
        resp = Client().get(CALLBACK_URL, {"code": "abc", "state": "notexist"})
        assert "reason=state_invalid" in resp["Location"]

    def test_expired_state_redirects_state_invalid(self, customer):
        _make_state(customer, expired=True)
        resp = Client().get(CALLBACK_URL, {"code": "abc", "state": "teststate123"})
        assert "reason=state_invalid" in resp["Location"]

    def test_deeplink_from_state_row_used_in_redirect(self, customer):
        _make_state(customer, deeplink="androidapp://google-health")
        resp = Client().get(
            CALLBACK_URL, {"error": "access_denied", "state": "teststate123"}
        )
        assert resp["Location"].startswith("androidapp://")

    @patch("googlehealth.oauth._fetch_google_user_id", return_value="guser1")
    @patch("googlehealth.oauth.exchange_code")
    def test_success_ingests_tokens_fires_signal_and_consumes_state(
        self, mock_exchange, _fetch, customer
    ):
        mock_exchange.return_value = GoogleTokens(
            access_token="tok", expires_in=3600, refresh_token="ref", scope=""
        )
        _make_state(customer)
        received = []

        def _receiver(sender, **kwargs):
            received.append(kwargs)

        mobile_connected.connect(_receiver)
        try:
            resp = Client().get(
                CALLBACK_URL, {"code": "authcode", "state": "teststate123"}
            )
        finally:
            mobile_connected.disconnect(_receiver)

        assert resp.status_code == 302
        assert resp["Location"] == "demoapp://google-health?status=success"

        # Real ingest_tokens ran: connection persisted with the exchanged tokens.
        connection = GoogleHealthConnection.objects.get(customer=customer)
        assert connection.status == ConnectionStatus.ACTIVE
        assert connection.access_token == "tok"
        assert connection.refresh_token == "ref"

        assert len(received) == 1
        assert received[0]["customer"] == customer
        assert received[0]["connection"] == connection

        # State row is consumed — a replay of the same URL is state_invalid.
        replay = Client().get(
            CALLBACK_URL, {"code": "authcode", "state": "teststate123"}
        )
        assert "reason=state_invalid" in replay["Location"]

        # exchange_code was called with the PKCE verifier + CSRF state pair.
        kwargs = mock_exchange.call_args.kwargs
        assert kwargs["code"] == "authcode"
        assert kwargs["code_verifier"] == "verifier"
        assert kwargs["expected_state"] == "teststate123"
        assert kwargs["received_state"] == "teststate123"

    @patch(
        "googlehealth.oauth.exchange_code", side_effect=oauth.StateMismatchError("nope")
    )
    def test_state_mismatch_redirects_state_invalid(self, _exc, customer):
        _make_state(customer)
        resp = Client().get(CALLBACK_URL, {"code": "abc", "state": "teststate123"})
        assert "reason=state_invalid" in resp["Location"]

    @patch("googlehealth.oauth.exchange_code", side_effect=Exception("boom"))
    def test_exchange_failure_redirects_exchange_failed(self, _exc, customer):
        _make_state(customer)
        resp = Client().get(CALLBACK_URL, {"code": "abc", "state": "teststate123"})
        assert "reason=exchange_failed" in resp["Location"]

    @patch("googlehealth.oauth._fetch_google_user_id", return_value="guser1")
    @patch("googlehealth.oauth.exchange_code")
    def test_raising_signal_receiver_redirects_exchange_failed(
        self, mock_exchange, _fetch, customer
    ):
        mock_exchange.return_value = GoogleTokens(
            access_token="tok", expires_in=3600, refresh_token="ref", scope=""
        )
        _make_state(customer)

        def _boom(sender, **kwargs):
            raise RuntimeError("receiver failure")

        mobile_connected.connect(_boom)
        try:
            resp = Client().get(
                CALLBACK_URL, {"code": "authcode", "state": "teststate123"}
            )
        finally:
            mobile_connected.disconnect(_boom)

        assert "reason=exchange_failed" in resp["Location"]
