"""Scopes, data type identifiers, and service URLs for the Google Health API."""

OAUTH_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

API_BASE_URL = "https://health.googleapis.com"
API_VERSION = "v4"

SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth"

SCOPE_ACTIVITY_AND_FITNESS = f"{SCOPE_PREFIX}.activity_and_fitness"
SCOPE_ACTIVITY_AND_FITNESS_READONLY = f"{SCOPE_PREFIX}.activity_and_fitness.readonly"
SCOPE_HEALTH_METRICS_AND_MEASUREMENTS = (
    f"{SCOPE_PREFIX}.health_metrics_and_measurements"
)
SCOPE_HEALTH_METRICS_AND_MEASUREMENTS_READONLY = (
    f"{SCOPE_PREFIX}.health_metrics_and_measurements.readonly"
)
SCOPE_LOCATION_READONLY = f"{SCOPE_PREFIX}.location.readonly"
SCOPE_NUTRITION = f"{SCOPE_PREFIX}.nutrition"
SCOPE_NUTRITION_READONLY = f"{SCOPE_PREFIX}.nutrition.readonly"
SCOPE_PROFILE = f"{SCOPE_PREFIX}.profile"
SCOPE_PROFILE_READONLY = f"{SCOPE_PREFIX}.profile.readonly"
SCOPE_SETTINGS = f"{SCOPE_PREFIX}.settings"
SCOPE_SETTINGS_READONLY = f"{SCOPE_PREFIX}.settings.readonly"
SCOPE_SLEEP = f"{SCOPE_PREFIX}.sleep"
SCOPE_SLEEP_READONLY = f"{SCOPE_PREFIX}.sleep.readonly"

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

ALL_READ_SCOPES = (
    SCOPE_ACTIVITY_AND_FITNESS_READONLY,
    SCOPE_HEALTH_METRICS_AND_MEASUREMENTS_READONLY,
    SCOPE_LOCATION_READONLY,
    SCOPE_NUTRITION_READONLY,
    SCOPE_PROFILE_READONLY,
    SCOPE_SETTINGS_READONLY,
    SCOPE_SLEEP_READONLY,
)

# Data type identifiers used in REST endpoint paths (kebab-case).
DATA_TYPE_ACTIVE_MINUTES = "active-minutes"
DATA_TYPE_ACTIVE_ZONE_MINUTES = "active-zone-minutes"
DATA_TYPE_ACTIVITY_LEVEL = "activity-level"
DATA_TYPE_ALTITUDE = "altitude"
DATA_TYPE_BODY_FAT = "body-fat"
DATA_TYPE_DISTANCE = "distance"
DATA_TYPE_EXERCISE = "exercise"
DATA_TYPE_FLOORS = "floors"
DATA_TYPE_HEART_RATE = "heart-rate"
DATA_TYPE_HEART_RATE_VARIABILITY = "heart-rate-variability"
DATA_TYPE_HEIGHT = "height"
DATA_TYPE_HYDRATION_LOG = "hydration-log"
DATA_TYPE_OXYGEN_SATURATION = "oxygen-saturation"
DATA_TYPE_SLEEP = "sleep"
DATA_TYPE_STEPS = "steps"
DATA_TYPE_TOTAL_CALORIES = "total-calories"
DATA_TYPE_WEIGHT = "weight"
DATA_TYPE_HEIGHT = "height"
DATA_TYPE_DAILY_RESTING_HEART_RATE = "daily-resting-heart-rate"
DATA_TYPE_DAILY_OXYGEN_SATURATION = "daily-oxygen-saturation"

# Human-readable, stored in Record.sourceName. The machine identifier is
# ``healthdatamodel.constants.DataSource.GOOGLE_HEALTH`` (added in 0.4.0).
SOURCE_NAME = "Google Health"
