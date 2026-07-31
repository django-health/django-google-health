"""Tests for googlehealth.ingest — mappers + sync_user orchestration."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
import respx
from healthdatamodel.constants import DataSource
from healthdatamodel.models import Record, Workout, WorkoutMetadataEntry
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, SleepValue
from httpx import Response

from googlehealth import ingest, oauth
from googlehealth.client import GoogleHealthClient
from googlehealth.constants import (
    API_BASE_URL,
    API_VERSION,
    DATA_TYPE_DAILY_RESTING_HEART_RATE,
    DATA_TYPE_EXERCISE,
    DATA_TYPE_HEART_RATE,
    DATA_TYPE_SLEEP,
    DATA_TYPE_STEPS,
    DATA_TYPE_TOTAL_CALORIES,
    DATA_TYPE_WEIGHT,
    OAUTH_TOKEN_URL,
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SOURCE_NAME,
)
from googlehealth.models import GoogleHealthConnection
from googlehealth.oauth import OAuthError


def _dp_url(data_type: str) -> str:
    return f"{API_BASE_URL}/{API_VERSION}/users/me/dataTypes/{data_type}/dataPoints"


# Pure mapper tests ----------------------------------------------------------


def test_map_steps():
    dp = {
        "name": "users/123/dataTypes/steps/dataPoints/p1",
        "steps": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:15:00Z",
            },
            "stepCount": "1234",
            "updateTime": "2026-05-01T10:20:00Z",
        },
    }
    rec = ingest.map_steps(dp)
    assert rec.recordId == "p1"
    assert rec.type == str(ActivityMetric.STEPS)
    assert rec.value == "1234"
    assert rec.unit == "count"
    assert rec.sourceName == SOURCE_NAME
    assert rec.startDate == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert rec.endDate == datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc)


def test_map_total_calories_uses_active_calories_type():
    dp = {
        "name": "users/x/dataTypes/total-calories/dataPoints/c1",
        "totalCalories": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:15:00Z",
            },
            "caloriesKcal": 42.5,
            "updateTime": "2026-05-01T10:20:00Z",
        },
    }
    rec = ingest.map_total_calories(dp)
    assert rec.type == str(ActivityMetric.ACTIVE_CALORIES)
    assert rec.value == "42.5"
    assert rec.unit == "kcal"


def test_map_heart_rate_point_in_time():
    dp = {
        "name": "users/x/dataTypes/heart-rate/dataPoints/h1",
        "heartRate": {
            "time": "2026-05-01T10:00:00Z",
            "beatsPerMinute": 72,
            "updateTime": "2026-05-01T10:00:01Z",
        },
    }
    rec = ingest.map_heart_rate(dp)
    assert rec.type == ingest.HK_HEART_RATE
    assert rec.value == "72"
    assert rec.startDate == rec.endDate


def test_map_weight():
    dp = {
        "name": "users/x/dataTypes/weight/dataPoints/w1",
        "weight": {
            "time": "2026-05-01T08:00:00Z",
            "weightKg": 75.4,
            "updateTime": "2026-05-01T08:00:01Z",
        },
    }
    rec = ingest.map_weight(dp)
    assert rec.type == ingest.HK_BODY_MASS
    assert rec.value == "75.4"
    assert rec.unit == "kg"


def test_map_sleep_session_decomposes_stages():
    dp = {
        "name": "users/x/dataTypes/sleep/dataPoints/s1",
        "sleep": {
            "interval": {
                "startTime": "2026-05-01T03:00:00Z",
                "endTime": "2026-05-01T07:00:00Z",
            },
            "stages": [
                {
                    "interval": {
                        "startTime": "2026-05-01T03:00:00Z",
                        "endTime": "2026-05-01T04:00:00Z",
                    },
                    "stage": "LIGHT",
                },
                {
                    "interval": {
                        "startTime": "2026-05-01T04:00:00Z",
                        "endTime": "2026-05-01T05:30:00Z",
                    },
                    "stage": "DEEP",
                },
                {
                    "interval": {
                        "startTime": "2026-05-01T05:30:00Z",
                        "endTime": "2026-05-01T06:30:00Z",
                    },
                    "stage": "REM",
                },
                {
                    "interval": {
                        "startTime": "2026-05-01T06:30:00Z",
                        "endTime": "2026-05-01T07:00:00Z",
                    },
                    "stage": "AWAKE",
                },
                {
                    "interval": {
                        "startTime": "2026-05-01T07:00:00Z",
                        "endTime": "2026-05-01T07:01:00Z",
                    },
                    "stage": "WEIRD_NEW_STAGE",  # unknown stages are skipped
                },
            ],
            "updateTime": "2026-05-01T07:05:00Z",
        },
    }
    records = ingest.map_sleep_session(dp)
    assert len(records) == 4
    assert [r.value for r in records] == [
        SleepValue.ASLEEP_CORE,
        SleepValue.ASLEEP_DEEP,
        SleepValue.ASLEEP_REM,
        SleepValue.AWAKE,
    ]
    assert all(r.type == SLEEP_TYPE for r in records)


def test_map_distance():
    dp = {
        "name": "users/x/dataTypes/distance/dataPoints/d1",
        "distance": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:15:00Z",
            },
            "distanceMillimeters": 1_609_344,  # 1 mile
            "updateTime": "2026-05-01T10:16:00Z",
        },
    }
    rec = ingest.map_distance(dp)
    assert rec.type == ingest.HK_DISTANCE_WALKING_RUNNING
    assert float(rec.value) == pytest.approx(1609.344)
    assert rec.unit == "m"


def test_map_altitude():
    dp = {
        "name": "users/x/dataTypes/altitude/dataPoints/a1",
        "altitude": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:30:00Z",
            },
            "elevationGainMillimeters": 12_500,  # 12.5 m
            "updateTime": "2026-05-01T10:31:00Z",
        },
    }
    rec = ingest.map_altitude(dp)
    assert rec.type == ingest.HK_ALTITUDE_GAIN
    assert float(rec.value) == pytest.approx(12.5)
    assert rec.unit == "m"


def test_map_floors():
    dp = {
        "name": "users/x/dataTypes/floors/dataPoints/f1",
        "floors": {
            "interval": {
                "startTime": "2026-05-01T08:00:00Z",
                "endTime": "2026-05-01T08:15:00Z",
            },
            "floorsClimbed": 3,
            "updateTime": "2026-05-01T08:16:00Z",
        },
    }
    rec = ingest.map_floors(dp)
    assert rec.type == ingest.HK_FLIGHTS_CLIMBED
    assert rec.value == "3"
    assert rec.unit == "count"


def test_map_active_zone_minutes():
    dp = {
        "name": "users/x/dataTypes/active-zone-minutes/dataPoints/azm1",
        "activeZoneMinutes": {
            "interval": {
                "startTime": "2026-05-01T07:00:00Z",
                "endTime": "2026-05-01T08:00:00Z",
            },
            "minutes": 22,
            "updateTime": "2026-05-01T08:01:00Z",
        },
    }
    rec = ingest.map_active_zone_minutes(dp)
    assert rec.type == ingest.HK_ACTIVE_ZONE_MINUTES
    assert rec.value == "22"
    assert rec.unit == "min"


def test_map_daily_resting_heart_rate():
    dp = {
        "name": "users/x/dataTypes/daily-resting-heart-rate/dataPoints/drhr1",
        "dailyRestingHeartRate": {
            "interval": {
                "startTime": "2026-05-01T00:00:00Z",
                "endTime": "2026-05-02T00:00:00Z",
            },
            "beatsPerMinute": 58,
            "updateTime": "2026-05-02T00:01:00Z",
        },
    }
    rec = ingest.map_daily_resting_heart_rate(dp)
    assert rec.type == ingest.HK_RESTING_HEART_RATE
    assert rec.value == "58"


def test_map_daily_oxygen_saturation():
    dp = {
        "name": "users/x/dataTypes/daily-oxygen-saturation/dataPoints/dox1",
        "dailyOxygenSaturation": {
            "interval": {
                "startTime": "2026-05-01T00:00:00Z",
                "endTime": "2026-05-02T00:00:00Z",
            },
            "averagePercentage": 96.5,
            "updateTime": "2026-05-02T00:01:00Z",
        },
    }
    rec = ingest.map_daily_oxygen_saturation(dp)
    assert rec.type == ingest.HK_OXYGEN_SATURATION
    assert rec.value == "96.5"
    assert rec.unit == "%"


def test_map_body_fat():
    dp = {
        "name": "users/x/dataTypes/body-fat/dataPoints/bf1",
        "bodyFat": {
            "time": "2026-05-01T08:00:00Z",
            "percentage": 18.2,
            "updateTime": "2026-05-01T08:00:01Z",
        },
    }
    rec = ingest.map_body_fat(dp)
    assert rec.type == ingest.HK_BODY_FAT_PERCENTAGE
    assert rec.value == "18.2"
    assert rec.startDate == rec.endDate


def test_map_height():
    dp = {
        "name": "users/x/dataTypes/height/dataPoints/h1",
        "height": {
            "time": "2026-05-01T08:00:00Z",
            "heightMeters": 1.78,
            "updateTime": "2026-05-01T08:00:01Z",
        },
    }
    rec = ingest.map_height(dp)
    assert rec.type == ingest.HK_HEIGHT
    assert rec.value == "1.78"
    assert rec.unit == "m"
    assert rec.startDate == rec.endDate


def test_default_data_types_includes_all_mapped_types():
    """Ensure the default sweep covers every mapper we expose."""
    assert set(ingest.DEFAULT_DATA_TYPES) >= {
        DATA_TYPE_STEPS,
        DATA_TYPE_HEART_RATE,
        DATA_TYPE_WEIGHT,
        DATA_TYPE_SLEEP,
        DATA_TYPE_EXERCISE,
    }
    # total-calories and floors are excluded from DEFAULT_DATA_TYPES because
    # Google rejects `list` for them — they need a dailyRollUp path we haven't
    # built yet. They're still mapped (the mapper functions exist) so the
    # consumer can wire them in once that path lands.
    assert DATA_TYPE_TOTAL_CALORIES in ingest.ROLLUP_ONLY_DATA_TYPES
    # And the new ones from slice 7.
    from googlehealth.constants import (
        DATA_TYPE_ACTIVE_ZONE_MINUTES,
        DATA_TYPE_ALTITUDE,
        DATA_TYPE_BODY_FAT,
        DATA_TYPE_DAILY_OXYGEN_SATURATION,
        DATA_TYPE_DAILY_RESTING_HEART_RATE,
        DATA_TYPE_DISTANCE,
        DATA_TYPE_FLOORS,
    )

    assert {
        DATA_TYPE_DISTANCE,
        DATA_TYPE_ALTITUDE,
        DATA_TYPE_ACTIVE_ZONE_MINUTES,
        DATA_TYPE_DAILY_RESTING_HEART_RATE,
        DATA_TYPE_DAILY_OXYGEN_SATURATION,
        DATA_TYPE_BODY_FAT,
    } <= set(ingest.DEFAULT_DATA_TYPES)
    assert DATA_TYPE_FLOORS in ingest.ROLLUP_ONLY_DATA_TYPES


def test_map_exercise():
    dp = {
        "name": "users/x/dataTypes/exercise/dataPoints/e1",
        "dataSource": {"recordingMethod": "MANUAL", "platform": "FITBIT"},
        "exercise": {
            "interval": {
                "startTime": "2026-02-23T13:10:00Z",
                "endTime": "2026-02-23T13:25:00Z",
            },
            "exerciseType": "WALKING",
            "metricsSummary": {
                "caloriesKcal": 16,
                "distanceMillimeters": 1609344,
                "steps": "2038",
                "averageHeartRateBeatsPerMinute": "81",
            },
            "displayName": "Walk",
            "updateTime": "2026-02-24T01:19:22.450466Z",
        },
    }
    workout = ingest.map_exercise(dp)
    assert workout.recordId == "e1"
    assert workout.workoutActivityType == "WALKING"
    assert workout.duration == 15 * 60  # 15-minute walk
    assert workout.caloriesBurned == 16.0
    assert workout.distance is not None
    assert abs(workout.distance - 1.609344) < 1e-6
    assert workout.distanceUnit == "km"
    keys = {entry.key for entry in workout.metadataEntry or []}
    assert {"steps", "average_heart_rate_bpm"} <= keys


# sync_user integration ------------------------------------------------------


def _page(data_points: list[dict]) -> dict:
    return {"dataPoints": data_points, "nextPageToken": ""}


@respx.mock
def test_sync_user_persists_each_data_type(connection, customer):
    steps_dp = {
        "name": "users/me/dataTypes/steps/dataPoints/sp1",
        "steps": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:15:00Z",
            },
            "stepCount": "500",
            "updateTime": "2026-05-01T10:16:00Z",
        },
    }
    calories_dp = {
        "name": "users/me/dataTypes/total-calories/dataPoints/c1",
        "totalCalories": {
            "interval": {
                "startTime": "2026-05-01T10:00:00Z",
                "endTime": "2026-05-01T10:15:00Z",
            },
            "caloriesKcal": 35,
            "updateTime": "2026-05-01T10:16:00Z",
        },
    }
    hr_dp = {
        "name": "users/me/dataTypes/heart-rate/dataPoints/hr1",
        "heartRate": {
            "time": "2026-05-01T10:05:00Z",
            "beatsPerMinute": 80,
            "updateTime": "2026-05-01T10:05:01Z",
        },
    }
    weight_dp = {
        "name": "users/me/dataTypes/weight/dataPoints/w1",
        "weight": {
            "time": "2026-05-01T08:00:00Z",
            "weightKg": 70.0,
            "updateTime": "2026-05-01T08:00:01Z",
        },
    }
    sleep_dp = {
        "name": "users/me/dataTypes/sleep/dataPoints/s1",
        "sleep": {
            "interval": {
                "startTime": "2026-05-01T03:00:00Z",
                "endTime": "2026-05-01T07:00:00Z",
            },
            "stages": [
                {
                    "interval": {
                        "startTime": "2026-05-01T03:00:00Z",
                        "endTime": "2026-05-01T04:00:00Z",
                    },
                    "stage": "LIGHT",
                },
                {
                    "interval": {
                        "startTime": "2026-05-01T04:00:00Z",
                        "endTime": "2026-05-01T05:00:00Z",
                    },
                    "stage": "DEEP",
                },
            ],
            "updateTime": "2026-05-01T07:01:00Z",
        },
    }
    exercise_dp = {
        "name": "users/me/dataTypes/exercise/dataPoints/e1",
        "exercise": {
            "interval": {
                "startTime": "2026-05-01T17:00:00Z",
                "endTime": "2026-05-01T17:30:00Z",
            },
            "exerciseType": "RUNNING",
            "metricsSummary": {"caloriesKcal": 200, "distanceMillimeters": 5_000_000},
            "updateTime": "2026-05-01T17:30:01Z",
        },
    }

    for data_type, dp in [
        (DATA_TYPE_STEPS, steps_dp),
        (DATA_TYPE_TOTAL_CALORIES, calories_dp),
        (DATA_TYPE_HEART_RATE, hr_dp),
        (DATA_TYPE_WEIGHT, weight_dp),
        (DATA_TYPE_SLEEP, sleep_dp),
        (DATA_TYPE_EXERCISE, exercise_dp),
    ]:
        respx.get(_dp_url(data_type)).mock(return_value=Response(200, json=_page([dp])))

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 2, tzinfo=timezone.utc)

    result = ingest.sync_user(
        connection,
        start=start,
        end=end,
        data_types=[
            DATA_TYPE_STEPS,
            DATA_TYPE_TOTAL_CALORIES,
            DATA_TYPE_HEART_RATE,
            DATA_TYPE_WEIGHT,
            DATA_TYPE_SLEEP,
            DATA_TYPE_EXERCISE,
        ],
        compute_basal=False,
    )

    # Per-type counts
    assert result.counts[DATA_TYPE_STEPS] == 1
    assert result.counts[DATA_TYPE_TOTAL_CALORIES] == 1
    assert result.counts[DATA_TYPE_HEART_RATE] == 1
    assert result.counts[DATA_TYPE_WEIGHT] == 1
    assert result.counts[DATA_TYPE_SLEEP] == 2  # two stages
    assert result.counts[DATA_TYPE_EXERCISE] == 1
    assert result.total == 7

    # Records landed in healthdatamodel
    records = Record.objects.filter(customer=customer)
    assert records.count() == 6  # 4 samples + 2 sleep stage records
    assert {r.source for r in records} == {DataSource.GOOGLE_HEALTH}
    assert {r.sourceName for r in records} == {SOURCE_NAME}

    # Workout landed via healthdatamodel.ingest_workouts (calories + distance
    # → metadata entries upstream).
    workout = Workout.objects.get(customer=customer)
    assert workout.workoutActivityType == "RUNNING"
    assert workout.duration == 1800
    metadata_keys = set(
        WorkoutMetadataEntry.objects.filter(workout=workout).values_list(
            "key", flat=True
        )
    )
    assert {"caloriesBurned", "distance"} <= metadata_keys

    # Connection got a sync timestamp
    connection.refresh_from_db()
    assert connection.last_sync_at is not None


@respx.mock
def test_sync_user_respects_data_types_argument(connection):
    respx.get(_dp_url(DATA_TYPE_STEPS)).mock(return_value=Response(200, json=_page([])))

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        compute_basal=False,
    )

    assert list(result.counts.keys()) == [DATA_TYPE_STEPS]


@respx.mock
def test_sync_user_handles_expired_token(customer):
    """End-to-end: stale token → proactive refresh → ingest flow continues."""
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
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="g1",
        access_token="ya29.stale",
        refresh_token="1//refresh",
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )
    respx.get(_dp_url(DATA_TYPE_STEPS)).mock(return_value=Response(200, json=_page([])))

    ingest.sync_user(
        conn,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        compute_basal=False,
    )

    conn.refresh_from_db()
    assert conn.access_token == "ya29.fresh"


# ---------------------------------------------------------------------------
# compute_basal_calories
# ---------------------------------------------------------------------------


def _seed_height_weight(customer, *, height_m: float, weight_kg: float, when: datetime):
    """Seed Record rows so compute_basal_calories has weight + height to read."""
    from healthdatamodel.models import Record

    common = {
        "customer": customer,
        "startDate": when,
        "endDate": when,
        "creationDate": when,
        "admin_create_date": when,
        "sourceName": "seed",
        "source": DataSource.GOOGLE_HEALTH,
    }
    Record.objects.create(
        **common, type=ingest.HK_HEIGHT, value=str(height_m), unit="m"
    )
    Record.objects.create(
        **common, type=ingest.HK_BODY_MASS, value=str(weight_kg), unit="kg"
    )


@pytest.mark.django_db
def test_compute_basal_calories_uses_mifflin_when_height_weight_available(connection):
    from healthdatamodel.models import Record
    from healthdatamodel.query import ActivityMetric

    customer = connection.customer
    seed_when = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _seed_height_weight(customer, height_m=1.80, weight_kg=80.0, when=seed_when)

    from healthdatamodel.bmr import age_from_dob, calculate_bmr

    dob = date(1990, 1, 1)
    profile = {"birthday": dob.isoformat(), "gender": "MALE"}
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 3, tzinfo=timezone.utc)

    count = ingest.compute_basal_calories(
        connection, start=start, end=end, profile=profile
    )

    assert count == 3  # May 1, 2, 3
    records = Record.objects.filter(
        customer=customer, type=str(ActivityMetric.BASAL_CALORIES)
    ).order_by("startDate")
    assert records.count() == 3
    expected = calculate_bmr(age_from_dob(dob), "M", 80.0, 180.0)
    for rec in records:
        assert float(rec.value) == pytest.approx(expected, rel=1e-6)
    assert {r.unit for r in records} == {"kcal"}


@pytest.mark.django_db
def test_compute_basal_calories_falls_back_to_lookup_when_records_missing(connection):
    from healthdatamodel.models import Record
    from healthdatamodel.query import ActivityMetric

    profile = {"birthday": "1990-05-15", "gender": "MALE"}
    count = ingest.compute_basal_calories(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        profile=profile,
    )
    assert count == 1
    rec = Record.objects.get(
        customer=connection.customer, type=str(ActivityMetric.BASAL_CALORIES)
    )
    # get_bmr's lookup table for a 35yo male yields a positive BMR, not the
    # 2000 default — we don't pin the exact value (the tables drift over time).
    assert float(rec.value) > 1000.0


@pytest.mark.django_db
def test_compute_basal_calories_returns_default_when_profile_blank(connection):
    from healthdatamodel.bmr import DEFAULT_BMR
    from healthdatamodel.models import Record
    from healthdatamodel.query import ActivityMetric

    count = ingest.compute_basal_calories(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        profile={},
    )
    assert count == 1
    rec = Record.objects.get(
        customer=connection.customer, type=str(ActivityMetric.BASAL_CALORIES)
    )
    assert float(rec.value) == DEFAULT_BMR


@pytest.mark.django_db
def test_compute_basal_calories_handles_civil_date_dob(connection):
    """Google's civil-date {year, month, day} shape should parse like ISO strings."""
    count = ingest.compute_basal_calories(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        profile={"birthday": {"year": 1990, "month": 5, "day": 15}, "gender": "FEMALE"},
    )
    assert count == 1


@respx.mock
def test_sync_user_with_compute_basal_calls_profile_and_persists(connection):
    """Wired through sync_user: the basal step runs after the main loop."""
    from healthdatamodel.models import Record
    from healthdatamodel.query import ActivityMetric

    customer = connection.customer
    seed_when = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _seed_height_weight(customer, height_m=1.65, weight_kg=60.0, when=seed_when)

    respx.get(_dp_url(DATA_TYPE_STEPS)).mock(return_value=Response(200, json=_page([])))
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/profile").mock(
        return_value=Response(200, json={"birthday": "1995-01-01", "gender": "F"})
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        compute_basal=True,
    )

    assert result.counts[DATA_TYPE_STEPS] == 0
    assert result.counts["basal-calories"] == 2  # 2026-05-01, 2026-05-02
    assert (
        Record.objects.filter(
            customer=customer, type=str(ActivityMetric.BASAL_CALORIES)
        ).count()
        == 2
    )


def _rollup_url(data_type: str) -> str:
    return (
        f"{API_BASE_URL}/{API_VERSION}/users/me/dataTypes/{data_type}/dataPoints:rollUp"
    )


@respx.mock
def test_sync_user_with_resolution_uses_rollup_endpoint(connection):
    """resolution_minutes=15 → rollUp path; mappers translate the
    type-specific value fields into Records of the right HK type + unit."""
    from healthdatamodel.models import Record
    from healthdatamodel.query import ActivityMetric

    customer = connection.customer
    steps_route = respx.post(_rollup_url(DATA_TYPE_STEPS)).mock(
        return_value=Response(
            200,
            json={
                "rollupDataPoints": [
                    {
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-01T00:15:00Z",
                        "steps": {"countSum": "120"},
                    },
                    {
                        "startTime": "2026-05-01T00:15:00Z",
                        "endTime": "2026-05-01T00:30:00Z",
                        "steps": {"countSum": "240"},
                    },
                ],
                "nextPageToken": "",
            },
        )
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        resolution_minutes=15,
        compute_basal=False,
    )

    # rollUp endpoint was used (not list)
    body = json.loads(steps_route.calls.last.request.content)
    assert body["windowSize"] == "900s"
    assert body["range"]["startTime"].endswith("Z")

    # Two rollup windows → two Records
    assert result.counts[DATA_TYPE_STEPS] == 2
    records = Record.objects.filter(customer=customer, type=str(ActivityMetric.STEPS))
    assert records.count() == 2
    assert {r.value for r in records} == {"120", "240"}


@respx.mock
def test_sync_user_with_resolution_falls_back_to_list_for_unsupported_types(connection):
    """Sleep / exercise / height / daily-* don't support rollUp — when a
    resolution is set, those types should silently fall back to the native list."""
    respx.get(_dp_url(DATA_TYPE_SLEEP)).mock(return_value=Response(200, json=_page([])))

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_SLEEP],
        resolution_minutes=15,
        compute_basal=False,
    )

    assert result.counts[DATA_TYPE_SLEEP] == 0


@respx.mock
def test_sync_user_resolution_expands_to_rollup_only_types(connection):
    """When data_types is None + resolution_minutes is set, total-calories +
    floors join the default sweep (the list endpoint rejects them)."""

    # Mock every data type's rollup endpoint as empty; only assert that
    # total-calories was queried at all.
    for dt in ingest.DEFAULT_DATA_TYPES:
        if dt in ingest._ROLLUP_UNSUPPORTED_DATA_TYPES:
            respx.get(_dp_url(dt)).mock(return_value=Response(200, json=_page([])))
        else:
            respx.post(_rollup_url(dt)).mock(
                return_value=Response(200, json={"rollupDataPoints": []})
            )
    for dt in ingest.ROLLUP_ONLY_DATA_TYPES:
        respx.post(_rollup_url(dt)).mock(
            return_value=Response(
                200,
                json={
                    "rollupDataPoints": [
                        {
                            "startTime": "2026-05-01T00:00:00Z",
                            "endTime": "2026-05-02T00:00:00Z",
                            "totalCalories": {"kcalSum": 2200.5},
                            "floors": {"countSum": "12"},
                        }
                    ],
                    "nextPageToken": "",
                },
            )
        )
    # Sleep uses list (it's unsupported for rollup)
    respx.get(_dp_url(DATA_TYPE_SLEEP)).mock(return_value=Response(200, json=_page([])))

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        resolution_minutes=1440,
        compute_basal=False,
    )

    assert "total-calories" in result.counts
    assert "floors" in result.counts
    # Each ROLLUP_ONLY type fetch returned one rollup point with both blocks
    # populated — the relevant mapper produced one Record.
    assert result.counts["total-calories"] == 1
    assert result.counts["floors"] == 1


def test_sync_user_rejects_negative_resolution(connection):
    with pytest.raises(ValueError, match="resolution_minutes"):
        ingest.sync_user(
            connection,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            data_types=[DATA_TYPE_STEPS],
            resolution_minutes=0,
            compute_basal=False,
        )


@pytest.mark.live
def test_sync_user_against_real_api(db):
    """Sync the last 24h for the test refresh token. Self-skips without env vars."""
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
    customer = User.objects.create_user(username="live-sync-test")
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="placeholder",
        access_token="placeholder",
        refresh_token=refresh_token,
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )
    # Resolve google_user_id before sync (would otherwise be set by an initial
    # ingest_tokens call; not exercised here).
    oauth.refresh_access_token(conn)
    with GoogleHealthClient(conn) as client:
        identity = client.get_identity()
    conn.google_user_id = identity.get("googleUserId") or identity.get("healthUserId")
    conn.save(update_fields=["google_user_id"])

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    result = ingest.sync_user(conn, start=start, end=end, data_types=[DATA_TYPE_STEPS])
    assert DATA_TYPE_STEPS in result.counts


# Server-side filters, failure isolation, active-energy conversion ------------
# (all behaviors verified against the live API on 2026-07-30)


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (
            DATA_TYPE_HEART_RATE,
            'heart_rate.sample_time.civil_time >= "2026-05-01T00:00:00"',
        ),
        (DATA_TYPE_WEIGHT, 'weight.sample_time.civil_time >= "2026-05-01T00:00:00"'),
        (
            DATA_TYPE_DAILY_RESTING_HEART_RATE,
            'daily_resting_heart_rate.date >= "2026-05-01"',
        ),
        (DATA_TYPE_STEPS, 'steps.interval.civil_start_time >= "2026-05-01T00:00:00"'),
        (DATA_TYPE_SLEEP, None),
    ],
)
def test_build_filter_member_per_data_type_family(data_type, expected):
    """Samples filter on sample_time.civil_time, daily aggregates on date,
    interval types on interval.civil_start_time; only Sleep is unfilterable.
    An unfiltered sample fetch walks the account's ENTIRE history."""
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 8, tzinfo=timezone.utc)
    assert ingest._build_filter(data_type, start, end) == expected


@respx.mock
def test_sync_user_sends_sample_time_filter_for_heart_rate(connection):
    route = respx.get(_dp_url(DATA_TYPE_HEART_RATE)).mock(
        return_value=Response(200, json=_page([]))
    )

    ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_HEART_RATE],
        compute_basal=False,
    )

    params = route.calls.last.request.url.params
    assert (
        params["filter"] == 'heart_rate.sample_time.civil_time >= "2026-05-01T00:00:00"'
    )


@respx.mock
def test_sync_user_basal_rate_converts_total_calories_to_active(connection, customer):
    """With basal_rate set, total-calories windows are stored as ACTIVE energy:
    pro-rata basal subtracted, floored at 0 (Apple ActiveEnergyBurned semantics
    — a sedentary window is 0 active kcal, not its BMR share)."""
    respx.post(_rollup_url(DATA_TYPE_TOTAL_CALORIES)).mock(
        return_value=Response(
            200,
            json={
                "rollupDataPoints": [
                    {
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-01T00:15:00Z",
                        "totalCalories": {"kcalSum": 25.0},
                    },
                    {
                        "startTime": "2026-05-01T00:15:00Z",
                        "endTime": "2026-05-01T00:30:00Z",
                        "totalCalories": {"kcalSum": 5.0},
                    },
                ],
                "nextPageToken": "",
            },
        )
    )

    ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_TOTAL_CALORIES],
        resolution_minutes=15,
        compute_basal=False,
        basal_rate=960.0,  # 960 kcal/day -> exactly 10 kcal per 900s window
    )

    records = Record.objects.filter(
        customer=customer, type=str(ActivityMetric.ACTIVE_CALORIES)
    ).order_by("startDate")
    assert [r.value for r in records] == ["15.000", "0.000"]


@respx.mock
def test_sync_user_without_basal_rate_keeps_raw_totals(connection, customer):
    """Back-compat: no basal_rate -> totals stored unmodified."""
    respx.post(_rollup_url(DATA_TYPE_TOTAL_CALORIES)).mock(
        return_value=Response(
            200,
            json={
                "rollupDataPoints": [
                    {
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-01T00:15:00Z",
                        "totalCalories": {"kcalSum": 25.0},
                    },
                ],
                "nextPageToken": "",
            },
        )
    )

    ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_TOTAL_CALORIES],
        resolution_minutes=15,
        compute_basal=False,
    )

    record = Record.objects.get(
        customer=customer, type=str(ActivityMetric.ACTIVE_CALORIES)
    )
    assert record.value == "25.0"


@respx.mock
def test_sync_user_skips_unmappable_points(connection, customer):
    """One malformed point must not abort the data type (previously a single
    point missing its timestamp killed the whole customer sync)."""
    good = {
        "name": "users/me/dataTypes/heart-rate/dataPoints/ok",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-05-01T10:00:00Z"},
            "beatsPerMinute": 62,
        },
    }
    bad = {
        "name": "users/me/dataTypes/heart-rate/dataPoints/broken",
        "heartRate": {"beatsPerMinute": 60},  # no sampleTime/time -> mapper raises
    }
    respx.get(_dp_url(DATA_TYPE_HEART_RATE)).mock(
        return_value=Response(200, json=_page([bad, good]))
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_HEART_RATE],
        compute_basal=False,
    )

    assert result.counts[DATA_TYPE_HEART_RATE] == 1
    record = Record.objects.get(customer=customer, type=ingest.HK_HEART_RATE)
    assert record.value == "62"


@respx.mock
def test_sync_user_isolates_per_type_failures(connection, customer):
    """One data type failing (bad filter, schema drift, non-retryable error)
    is recorded in result.errors; the remaining types still sync."""
    respx.get(_dp_url(DATA_TYPE_STEPS)).mock(
        return_value=Response(400, json={"error": {"message": "boom"}})
    )
    respx.get(_dp_url(DATA_TYPE_WEIGHT)).mock(
        return_value=Response(200, json=_page([]))
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS, DATA_TYPE_WEIGHT],
        compute_basal=False,
    )

    assert DATA_TYPE_STEPS in result.errors
    assert DATA_TYPE_STEPS not in result.counts
    assert result.counts[DATA_TYPE_WEIGHT] == 0
    # the sync completed and stamped the connection
    connection.refresh_from_db()
    assert connection.last_sync_at is not None


@respx.mock
def test_sync_user_compute_basal_failure_recorded_not_raised(connection, customer):
    """compute_basal is an enrichment: a profile 403 (token minted without the
    profile scope) must not discard an otherwise-complete sync."""
    respx.get(_dp_url(DATA_TYPE_STEPS)).mock(return_value=Response(200, json=_page([])))
    respx.get(f"{API_BASE_URL}/{API_VERSION}/users/me/profile").mock(
        return_value=Response(403, json={"error": {"message": "insufficient scopes"}})
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        compute_basal=True,
    )

    assert result.counts[DATA_TYPE_STEPS] == 0
    assert "basal-calories" in result.errors
    connection.refresh_from_db()
    assert connection.last_sync_at is not None


@respx.mock
def test_sync_user_oauth_error_still_aborts(customer):
    """Credential failures affect every request — they must abort the sync,
    not degrade into one errors[] entry per data type."""
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=Response(400, json={"error": "invalid_grant"})
    )
    conn = GoogleHealthConnection.objects.create(
        customer=customer,
        google_user_id="g-expired",
        access_token="ya29.stale",
        refresh_token="1//dead",
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        scopes=[SCOPE_ACTIVITY_AND_FITNESS_READONLY],
    )

    with pytest.raises(OAuthError):
        ingest.sync_user(
            conn,
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            data_types=[DATA_TYPE_STEPS, DATA_TYPE_WEIGHT],
            compute_basal=False,
        )


@respx.mock
def test_sync_user_collect_records_returns_mapped_records(connection):
    """collect_records=True surfaces the mapped RecordInputs on the result so
    callers can compute derived stats from the in-memory payload instead of
    reading the (very large) Record table back."""
    respx.post(_rollup_url(DATA_TYPE_STEPS)).mock(
        return_value=Response(
            200,
            json={
                "rollupDataPoints": [
                    {
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-01T00:15:00Z",
                        "steps": {"countSum": "120"},
                    },
                ],
                "nextPageToken": "",
            },
        )
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_STEPS],
        resolution_minutes=15,
        compute_basal=False,
        collect_records=True,
    )

    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.type == str(ActivityMetric.STEPS)
    assert rec.value == "120"
    assert rec.startDate == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


@respx.mock
def test_sync_user_records_empty_by_default(connection):
    """Without collect_records, no in-memory payload is retained."""
    respx.get(_dp_url(DATA_TYPE_HEART_RATE)).mock(
        return_value=Response(200, json=_page([]))
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_HEART_RATE],
        compute_basal=False,
    )

    assert result.records == []


@respx.mock
def test_collect_records_is_window_bounded(connection, customer):
    """Sleep's native fetch is unbounded (no server-side filter) — an old
    account can return years of history. All of it is ingested, but only
    records overlapping the sync window are retained on result.records, so
    the in-memory payload has a hard ceiling."""
    in_window = {
        "name": "users/me/dataTypes/sleep/dataPoints/s-new",
        "sleep": {
            "interval": {
                "startTime": "2026-05-01T03:00:00Z",
                "endTime": "2026-05-01T07:00:00Z",
            },
            "stages": [
                {
                    "interval": {
                        "startTime": "2026-05-01T03:00:00Z",
                        "endTime": "2026-05-01T07:00:00Z",
                    },
                    "stage": "DEEP",
                },
            ],
        },
    }
    ancient = {
        "name": "users/me/dataTypes/sleep/dataPoints/s-old",
        "sleep": {
            "interval": {
                "startTime": "2023-01-01T03:00:00Z",
                "endTime": "2023-01-01T07:00:00Z",
            },
            "stages": [
                {
                    "interval": {
                        "startTime": "2023-01-01T03:00:00Z",
                        "endTime": "2023-01-01T07:00:00Z",
                    },
                    "stage": "DEEP",
                },
            ],
        },
    }
    respx.get(_dp_url(DATA_TYPE_SLEEP)).mock(
        return_value=Response(200, json=_page([ancient, in_window]))
    )

    result = ingest.sync_user(
        connection,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_types=[DATA_TYPE_SLEEP],
        compute_basal=False,
        collect_records=True,
    )

    # Both ingested (backup store keeps full history)...
    assert result.counts[DATA_TYPE_SLEEP] == 2
    assert Record.objects.filter(customer=customer, type=SLEEP_TYPE).count() == 2
    # ...but only the in-window record is retained in memory.
    assert len(result.records) == 1
    assert result.records[0].startDate == datetime(
        2026, 5, 1, 3, 0, tzinfo=timezone.utc
    )
