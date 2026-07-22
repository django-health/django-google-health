from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import models


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
