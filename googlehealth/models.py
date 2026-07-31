from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone as dj_timezone


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"
    REVOKED = "revoked", "Revoked"


class GoogleHealthConnection(models.Model):
    """Per-user OAuth state for the Google Health API.

    Health records persist through django-healthdatamodel; this model only
    holds the credentials needed to fetch them.
    """

    customer = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_health_connection",
    )
    google_user_id = models.CharField(max_length=128, db_index=True)
    # OAuth client that issued these tokens. Google binds refresh grants to the
    # issuing client, so multi-client setups (e.g. iOS/Android public clients
    # alongside the backend's web client) must refresh with this id. Empty means
    # the default client (settings.GOOGLE_HEALTH_CLIENT_ID).
    client_id = models.CharField(max_length=255, blank=True, default="")
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()
    scopes = models.JSONField(default=list)
    status = models.CharField(
        max_length=32,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.ACTIVE,
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Google Health connection"
        verbose_name_plural = "Google Health connections"

    def __str__(self) -> str:
        return (
            f"GoogleHealthConnection(customer={self.customer_id}, status={self.status})"
        )

    def is_token_expired(
        self, *, leeway_seconds: int = 60, now: datetime | None = None
    ) -> bool:
        """True if the access token is at or past ``token_expires_at - leeway``.

        The leeway buys time for an in-flight request to complete with the same
        token without bumping into Google's 1-hour cutoff.
        """
        anchor = now or datetime.now(timezone.utc)
        return anchor >= self.token_expires_at - timedelta(seconds=leeway_seconds)


class GoogleHealthOAuthState(models.Model):
    """Short-lived server-side state tying an OAuth ``state`` param to a customer.

    Backs the mobile connect flow: created by
    :func:`googlehealth.oauth.start_mobile_flow` (called from your
    authenticated API endpoint), consumed exactly once by the public
    :func:`googlehealth.views.mobile_callback`. The two only need to share a
    database — they can live in separate deployments (e.g. a Lambda API and a
    Kubernetes web tier).

    Rows are single-use and expire after the TTL passed to
    ``start_mobile_flow`` (default 10 minutes); ``consume`` enforces both.
    """

    state = models.CharField(max_length=128, unique=True)
    code_verifier = models.CharField(max_length=256)
    scopes = models.JSONField(default=list)
    # Deep-link base the callback 302s back to:
    #   <deeplink>?status=success|denied|error[&reason=...]
    deeplink = models.CharField(max_length=512)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_health_oauth_states",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Google Health OAuth state"
        verbose_name_plural = "Google Health OAuth states"

    def __str__(self) -> str:
        return (
            f"GoogleHealthOAuthState("
            f"state={self.state[:8]}…, "
            f"customer={self.customer_id}, "
            f"consumed={self.consumed_at is not None})"
        )


def consume(state: str) -> GoogleHealthOAuthState | None:
    """Atomically consume a state record (single-use + expiry).

    Finds an unconsumed, unexpired record matching ``state``, marks it
    consumed, and returns it. Returns None if the state is unknown, already
    consumed, or expired. SELECT FOR UPDATE prevents replay races when two
    callbacks arrive simultaneously (e.g. an accidental double-tap).
    """
    with transaction.atomic():
        rec = (
            GoogleHealthOAuthState.objects.select_for_update()
            .filter(
                state=state,
                consumed_at__isnull=True,
                expires_at__gt=dj_timezone.now(),
            )
            .select_related("customer")
            .first()
        )
        if rec:
            rec.consumed_at = dj_timezone.now()
            rec.save(update_fields=["consumed_at"])
        return rec
