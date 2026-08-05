import os
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from googlehealth.client import GoogleHealthAPIError, GoogleHealthClient
from googlehealth.constants import (
    API_BASE_URL,
    API_VERSION,
    DATA_TYPE_EXERCISE,
    DATA_TYPE_STEPS,
    OAUTH_TOKEN_URL,
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
)
from googlehealth.models import GoogleHealthConnection

DATAPOINTS_URL = f"{API_BASE_URL}/{API_VERSION}/users/me/dataTypes/exercise/dataPoints"
STEPS_DATAPOINTS_URL = (
    f"{API_BASE_URL}/{API_VERSION}/users/me/dataTypes/steps/dataPoints"
)
IDENTITY_URL = f"{API_BASE_URL}/{API_VERSION}/users/me/identity"


@pytest.fixture
def no_sleep():
    """Disable sleeps so backoff tests run instantly."""
    return lambda _: None


@respx.mock
def test_list_data_points_happy_path(connection, no_sleep):
    respx.get(DATAPOINTS_URL).mock(
        return_value=Response(
            200, json={"dataPoints": [{"name": "p1"}], "nextPageToken": ""}
        )
    )

    with GoogleHealthClient(connection, sleep=no_sleep) as client:
        page = client.list_data_points(DATA_TYPE_EXERCISE)

    assert page == {"dataPoints": [{"name": "p1"}], "nextPageToken": ""}
    call = respx.calls.last
    assert call.request.headers["Authorization"] == f"Bearer {connection.access_token}"


@respx.mock
def test_list_data_points_passes_filter_and_paging_params(connection, no_sleep):
    route = respx.get(DATAPOINTS_URL).mock(
        return_value=Response(200, json={"dataPoints": []})
    )

    with GoogleHealthClient(connection, sleep=no_sleep) as client:
        client.list_data_points(
            DATA_TYPE_EXERCISE,
            filter='exercise.interval.civil_start_time >= "2026-02-22T00:00:00"',
            page_size=100,
            page_token="abc",
        )

    qs = dict(route.calls.last.request.url.params)
    assert qs["pageSize"] == "100"
    assert qs["pageToken"] == "abc"
    assert "civil_start_time" in qs["filter"]


@respx.mock
def test_iter_data_points_walks_pages(connection, no_sleep):
    respx.get(STEPS_DATAPOINTS_URL).mock(
        side_effect=[
            Response(
                200, json={"dataPoints": [{"id": 1}, {"id": 2}], "nextPageToken": "p2"}
            ),
            Response(200, json={"dataPoints": [{"id": 3}], "nextPageToken": ""}),
        ]
    )

    with GoogleHealthClient(connection, sleep=no_sleep) as client:
        ids = [dp["id"] for dp in client.iter_data_points(DATA_TYPE_STEPS)]

    assert ids == [1, 2, 3]


@respx.mock
def test_proactive_refresh_when_token_expired(customer, no_sleep):
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="g1",
        access_token="ya29.stale",
        refresh_token="1//refresh",
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "ya29.fresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": SCOPE_ACTIVITY_AND_FITNESS_READONLY,
            },
        )
    )
    route = respx.get(IDENTITY_URL).mock(
        return_value=Response(200, json={"googleUserId": "g1"})
    )

    with GoogleHealthClient(conn, sleep=no_sleep) as client:
        client.get_identity()

    assert route.calls.last.request.headers["Authorization"] == "Bearer ya29.fresh"
    conn.refresh_from_db()
    assert conn.access_token == "ya29.fresh"


@respx.mock
def test_401_triggers_refresh_and_single_retry(connection, no_sleep):
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "ya29.fresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": SCOPE_ACTIVITY_AND_FITNESS_READONLY,
            },
        )
    )
    respx.get(IDENTITY_URL).mock(
        side_effect=[
            Response(401, json={"error": {"message": "invalid_token"}}),
            Response(200, json={"googleUserId": "g1"}),
        ]
    )

    with GoogleHealthClient(connection, sleep=no_sleep) as client:
        result = client.get_identity()

    assert result == {"googleUserId": "g1"}
    connection.refresh_from_db()
    assert connection.access_token == "ya29.fresh"


@respx.mock
def test_repeated_401_raises_after_one_refresh(connection, no_sleep):
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "ya29.fresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": SCOPE_ACTIVITY_AND_FITNESS_READONLY,
            },
        )
    )
    respx.get(IDENTITY_URL).mock(
        return_value=Response(401, json={"error": {"message": "invalid_token"}})
    )

    with (
        GoogleHealthClient(connection, sleep=no_sleep) as client,
        pytest.raises(GoogleHealthAPIError) as exc,
    ):
        client.get_identity()

    assert exc.value.status_code == 401


@respx.mock
def test_429_retries_then_succeeds(connection):
    sleeps: list[float] = []
    respx.get(IDENTITY_URL).mock(
        side_effect=[
            Response(429, headers={"Retry-After": "2"}, json={}),
            Response(200, json={"googleUserId": "g1"}),
        ]
    )

    with GoogleHealthClient(connection, sleep=sleeps.append) as client:
        client.get_identity()

    assert sleeps == [2.0]


@respx.mock
def test_5xx_retries_with_exponential_backoff_then_fails(connection):
    sleeps: list[float] = []
    respx.get(IDENTITY_URL).mock(return_value=Response(503, json={}))

    with (
        GoogleHealthClient(
            connection, sleep=sleeps.append, max_retries=3, backoff_seconds=0.5
        ) as client,
        pytest.raises(GoogleHealthAPIError) as exc,
    ):
        client.get_identity()

    assert exc.value.status_code == 503
    assert sleeps == [0.5, 1.0, 2.0]  # base * 2^attempt for attempts 0,1,2


@respx.mock
def test_non_retryable_4xx_raises_immediately_with_message(connection, no_sleep):
    respx.get(IDENTITY_URL).mock(
        return_value=Response(400, json={"error": {"message": "bad filter"}})
    )

    with (
        GoogleHealthClient(connection, sleep=no_sleep) as client,
        pytest.raises(GoogleHealthAPIError) as exc,
    ):
        client.get_identity()

    assert exc.value.status_code == 400
    assert "bad filter" in str(exc.value)


@respx.mock
def test_daily_roll_up_posts_body(connection, no_sleep):
    url = (
        f"{API_BASE_URL}/{API_VERSION}/users/me/dataTypes/steps/dataPoints:dailyRollUp"
    )
    route = respx.post(url).mock(return_value=Response(200, json={"rollUps": []}))

    body = {"windowSize": "1d", "startTime": "2026-05-01", "endTime": "2026-05-07"}
    with GoogleHealthClient(connection, sleep=no_sleep) as client:
        client.daily_roll_up(DATA_TYPE_STEPS, body)

    assert route.calls.last.request.method == "POST"


@pytest.mark.live
def test_list_exercises_against_real_api(db):
    """List exercise data points from real ``health.googleapis.com``.

    Set ``GOOGLE_HEALTH_TEST_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN}`` to enable.
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
    customer = User.objects.create_user(username="live-client-test")
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="placeholder",
        access_token="placeholder",
        refresh_token=refresh_token,
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )

    with GoogleHealthClient(conn) as client:
        page = client.list_data_points(DATA_TYPE_EXERCISE, page_size=5)

    assert "dataPoints" in page
