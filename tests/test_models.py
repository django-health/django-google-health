from datetime import datetime, timedelta, timezone

import pytest

from googlehealth.constants import SCOPE_ACTIVITY_AND_FITNESS_READONLY
from googlehealth.models import ConnectionStatus, GoogleHealthConnection

pytestmark = pytest.mark.django_db


def test_create_connection(customer):
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="2515055256096816351",
        access_token="ya29.access",
        refresh_token="1//refresh",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )

    assert conn.status == ConnectionStatus.ACTIVE
    assert conn.scopes == [SCOPE_ACTIVITY_AND_FITNESS_READONLY]
    assert customer.google_health_connection == conn


def test_status_choices_include_disconnected_and_revoked():
    values = {c.value for c in ConnectionStatus}
    assert values == {"active", "disconnected", "revoked", "unlinked"}
