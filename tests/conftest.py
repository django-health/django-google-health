import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from googlehealth.constants import (
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SCOPE_SLEEP_READONLY,
)
from googlehealth.models import GoogleHealthConnection


@pytest.fixture
def customer(db):
    User = get_user_model()
    return User.objects.create_user(username="test-customer")


@pytest.fixture
def connection(customer):
    return GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="2515055256096816351",
        access_token="ya29.initial-access",
        refresh_token="1//initial-refresh",
        token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY, SCOPE_SLEEP_READONLY],
    )


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Real payloads captured from the live API — ground truth for shapes."""
    return json.loads((FIXTURES_DIR / name).read_text())
