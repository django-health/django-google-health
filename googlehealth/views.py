"""HTTP views for OAuth + webhook notifications.

The OAuth views (``connect`` / ``callback`` / ``disconnect``) cover the
web-callback flow used in admin / dev / testing. Mobile clients should POST
tokens (or an auth code) to a project-local endpoint that calls
:func:`googlehealth.oauth.ingest_tokens` or :func:`googlehealth.oauth.exchange_code`.

The ``notification_receiver`` view satisfies Google Health's webhook
handshake and emits a :data:`googlehealth.signals.notification_received` signal
for every authenticated notification.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import oauth, webhooks
from .signals import notification_received
from .constants import (
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SCOPE_HEALTH_METRICS_AND_MEASUREMENTS_READONLY,
    SCOPE_PROFILE_READONLY,
    SCOPE_SLEEP_READONLY,
)
from .models import GoogleHealthConnection

SESSION_KEY = "googlehealth_oauth_flow"

# Profile readonly is included so compute_basal_calories can read real DOB +
# gender for Mifflin-St Jeor instead of silently falling back to a median
# lookup table (issue #3). Settings readonly is deliberately left out until
# something actually reads unit/timezone prefs.
DEFAULT_SCOPES: tuple[str, ...] = (
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SCOPE_HEALTH_METRICS_AND_MEASUREMENTS_READONLY,
    SCOPE_PROFILE_READONLY,
    SCOPE_SLEEP_READONLY,
)


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
