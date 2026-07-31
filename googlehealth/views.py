"""HTTP views for OAuth + webhook notifications.

The session OAuth views (``connect`` / ``callback`` / ``disconnect``) cover
the web-callback flow used in admin / dev / testing.

``mobile_callback`` covers the backend-owned mobile flow: an authenticated
project-local API endpoint calls :func:`googlehealth.oauth.start_mobile_flow`
to mint the consent URL, the app opens it in a system browser, and Google
redirects back here — anonymously, so identity is resolved from the stored
:class:`googlehealth.models.GoogleHealthOAuthState` row instead of a session.
The result is signalled to the app via a 302 to its deep link. (Mobile apps
that already hold a token dict can instead POST it to a project-local
endpoint that calls :func:`googlehealth.oauth.ingest_tokens`.)

The ``notification_receiver`` view satisfies Google Health's webhook
handshake and emits a :data:`googlehealth.signals.notification_received` signal
for every authenticated notification.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import oauth, webhooks
from .signals import mobile_connected, notification_received
from .constants import DEFAULT_SCOPES  # noqa: F401 — re-exported (moved to constants)
from .models import GoogleHealthConnection, consume

log = logging.getLogger(__name__)

SESSION_KEY = "googlehealth_oauth_flow"


def _scopes() -> list[str]:
    return list(getattr(settings, "GOOGLE_HEALTH_DEFAULT_SCOPES", DEFAULT_SCOPES))


def _success_url() -> str:
    return getattr(settings, "GOOGLE_HEALTH_CONNECT_SUCCESS_URL", "/admin/")


@login_required
@require_http_methods(["GET"])
def connect(request: HttpRequest) -> HttpResponse:
    auth_url, flow_state = oauth.build_authorization_url(scopes=_scopes())
    request.session[SESSION_KEY] = flow_state.model_dump()
    return redirect(auth_url)


@login_required
@require_http_methods(["GET"])
def callback(request: HttpRequest) -> HttpResponse:
    code = request.GET.get("code")
    received_state = request.GET.get("state")
    error = request.GET.get("error")
    if error:
        return HttpResponseBadRequest(f"OAuth error: {error}")
    if not code:
        return HttpResponseBadRequest("Missing authorization code")

    stashed = request.session.pop(SESSION_KEY, None)
    if not stashed:
        return HttpResponseBadRequest("No OAuth flow in progress")
    flow_state = oauth.OAuthFlowState.model_validate(stashed)

    try:
        tokens = oauth.exchange_code(
            code=code,
            scopes=flow_state.scopes,
            code_verifier=flow_state.code_verifier,
            expected_state=flow_state.state,
            received_state=received_state,
        )
    except oauth.StateMismatchError:
        return HttpResponseBadRequest("OAuth state mismatch")

    oauth.ingest_tokens(customer=request.user, tokens=tokens)
    return redirect(_success_url())


def _app_redirect(deeplink: str, status: str, reason: str = "") -> HttpResponse:
    """Build a 302 to the app deep-link with status and optional reason.

    Uses HttpResponse directly rather than Django's redirect() shortcut because
    redirect() rejects non-http(s) schemes with DisallowedRedirect.
    """
    url = f"{deeplink}?status={status}"
    if reason:
        url += f"&reason={reason}"
    response = HttpResponse(status=302)
    response["Location"] = url
    return response


@require_http_methods(["GET"])
def mobile_callback(request: HttpRequest) -> HttpResponse:
    """Public Google OAuth callback for the backend-owned mobile connect flow.

    Google redirects here after the user approves or denies consent. The
    browser session is anonymous (no login, no cookies), so the customer is
    resolved by consuming the :class:`~googlehealth.models.GoogleHealthOAuthState`
    row created by :func:`googlehealth.oauth.start_mobile_flow` — single-use
    and TTL-bounded.

    The outcome is reported to the app via a 302 to the deep link stored on
    the state row:  ``<deeplink>?status=success|denied|error[&reason=...]``.
    When the state can't be resolved (unknown / expired / replayed), the
    redirect falls back to ``settings.GOOGLE_HEALTH_APP_DEEPLINK``; if that is
    unset too, a plain 400 is returned.

    On success, tokens are persisted via :func:`googlehealth.oauth.ingest_tokens`
    and the :data:`googlehealth.signals.mobile_connected` signal fires so the
    project can flip app-side state (activate the data source, kick off a
    first sync, …). A receiver that raises sends ``status=error`` to the app.
    """
    code = request.GET.get("code")
    received_state = request.GET.get("state")
    error = request.GET.get("error")

    # Consume the state early so rec.deeplink is available on all error paths.
    # For unresolvable states we fall back to the server-side default.
    rec = consume(received_state) if received_state else None
    fallback = getattr(settings, "GOOGLE_HEALTH_APP_DEEPLINK", "")
    deeplink = rec.deeplink if rec is not None else fallback
    if not deeplink:
        log.warning(
            "mobile_callback: unresolvable state and no GOOGLE_HEALTH_APP_DEEPLINK"
        )
        return HttpResponseBadRequest("Unknown or expired OAuth state")

    if error:
        log.warning("mobile_callback received error from Google: %s", error)
        if error == "access_denied":
            return _app_redirect(deeplink, "denied")
        return _app_redirect(deeplink, "error", "google_error")

    if not code or not received_state:
        log.warning("mobile_callback missing code or state params")
        return _app_redirect(deeplink, "error", "state_invalid")

    if rec is None:
        log.warning(
            "mobile_callback: state not found, expired, or already used: %s…",
            received_state[:8],
        )
        return _app_redirect(deeplink, "error", "state_invalid")

    try:
        tokens = oauth.exchange_code(
            code=code,
            scopes=rec.scopes,
            code_verifier=rec.code_verifier,
            expected_state=rec.state,
            received_state=received_state,
        )
        connection = oauth.ingest_tokens(customer=rec.customer, tokens=tokens)
        mobile_connected.send(sender=None, customer=rec.customer, connection=connection)
    except oauth.StateMismatchError:
        log.error("mobile_callback: state mismatch for customer %s", rec.customer_id)
        return _app_redirect(deeplink, "error", "state_invalid")
    except Exception:
        log.exception(
            "mobile_callback: token exchange/ingest failed for customer %s",
            rec.customer_id,
        )
        return _app_redirect(deeplink, "error", "exchange_failed")

    log.info("Google Health connection established for customer %s", rec.customer_id)
    return _app_redirect(deeplink, "success")


@login_required
@require_POST
def disconnect(request: HttpRequest) -> HttpResponse:
    try:
        connection = GoogleHealthConnection.objects.get(customer=request.user)
    except GoogleHealthConnection.DoesNotExist:
        return redirect(_success_url())
    oauth.revoke(connection)
    return redirect(_success_url())


@csrf_exempt
@require_POST
def notification_receiver(request: HttpRequest) -> HttpResponse:
    """Receive Google Health webhook POSTs.

    Two distinct request shapes share this endpoint:

    1. **Verification handshake** (``{"type": "verification"}``, User-Agent
       ``Google-Health-API-Webhooks-Verifier``). Auth-bearing requests must get
       a 200; unauthenticated ones must get a 401. This is what unblocks
       subscriber create/update.

    2. **Notifications**. Validate the ``Authorization`` header against
       ``settings.GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION``, emit the
       ``notification_received`` signal, and return ``204``. Any heavy lifting
       belongs in the signal handler (which should hand off to a queue).
    """
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("invalid JSON body")

    auth_header = request.META.get("HTTP_AUTHORIZATION")
    auth_ok = webhooks.authorization_matches(auth_header)

    if webhooks.is_verification_payload(payload):
        return HttpResponse(status=200) if auth_ok else HttpResponse(status=401)

    if not auth_ok:
        return HttpResponse(status=401)

    notification_received.send(sender=None, payload=payload)
    return HttpResponse(status=204)
