"""HTTP client for the Google Health REST API.

Sync-only wrapper around ``httpx.Client``. Built from a
:class:`~googlehealth.models.GoogleHealthConnection` (the typical path) or
directly from a ``google.oauth2.credentials.Credentials`` object.

Responsibilities:

* Authorization: inject ``Bearer <access_token>``.
* Token freshness: refresh proactively when the stored expiry is within
  ``leeway_seconds``, and retry once on a 401 to absorb clock skew or external
  token invalidation.
* Resilience: retry 3× with exponential backoff on 429 and 5xx; honor
  ``Retry-After``.
* Pagination: :meth:`iter_data_points` walks ``nextPageToken`` and yields raw
  data points one at a time.

Method names mirror the REST surface
(`/health/reference/rest <https://developers.google.com/health/reference/rest>`_)
so callers can cross-reference Google's docs without translation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator, Mapping

import httpx

from . import oauth
from .constants import API_BASE_URL, API_VERSION

if TYPE_CHECKING:
    from .models import GoogleHealthConnection


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


def _rfc3339(dt: datetime) -> str:
    """Serialize a datetime as RFC3339 with a literal ``Z`` suffix, the format
    Google's API expects for ``Interval.startTime`` / ``endTime``."""
    return (
        dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        + "Z"
    )


class GoogleHealthAPIError(Exception):
    """Non-retryable error returned by the Google Health API."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class GoogleHealthClient:
    """Thin REST client. Use as a context manager so the underlying httpx
    session is closed deterministically.
    """

    def __init__(
        self,
        connection: GoogleHealthConnection,
        *,
        base_url: str = API_BASE_URL,
        api_version: str = API_VERSION,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff_seconds: float = BASE_BACKOFF_SECONDS,
        sleep: callable = time.sleep,
    ):
        self.connection = connection
        self._base = f"{base_url.rstrip('/')}/{api_version}"
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout)

    # context manager ------------------------------------------------------

    def __enter__(self) -> GoogleHealthClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # core request loop ----------------------------------------------------

    def _ensure_fresh_token(self) -> None:
        if self.connection.is_token_expired():
            oauth.refresh_access_token(self.connection)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.connection.access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        self._ensure_fresh_token()
        url = f"{self._base}/{path.lstrip('/')}"
        retried_after_401 = False
        attempt = 0

        while True:
            response = self._http.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._auth_headers(),
            )

            if response.status_code == 401 and not retried_after_401:
                # Either clock skew or the token was invalidated externally —
                # force a refresh and retry once.
                oauth.refresh_access_token(self.connection)
                retried_after_401 = True
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._sleep(self._compute_backoff(response, attempt))
                attempt += 1
                continue

            if response.status_code >= 400:
                payload = _safe_json(response)
                message = _extract_error_message(payload, response.text)
                raise GoogleHealthAPIError(response.status_code, message, payload)

            if not response.content:
                return {}
            return response.json()

    def _compute_backoff(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff * (2**attempt)

    # resource methods -----------------------------------------------------

    def get_identity(self) -> dict[str, Any]:
        return self._request("GET", "users/me/identity")

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "users/me/profile")

    def get_settings(self) -> dict[str, Any]:
        return self._request("GET", "users/me/settings")

    def list_data_points(
        self,
        data_type: str,
        *,
        filter: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """One page of ``GET /users/me/dataTypes/{data_type}/dataPoints``.

        Use :meth:`iter_data_points` to walk all pages.
        """
        params: dict[str, Any] = {}
        if filter is not None:
            params["filter"] = filter
        if page_size is not None:
            params["pageSize"] = page_size
        if page_token is not None:
            params["pageToken"] = page_token
        return self._request(
            "GET",
            f"users/me/dataTypes/{data_type}/dataPoints",
            params=params or None,
        )

    def iter_data_points(
        self,
        data_type: str,
        *,
        filter: str | None = None,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate every data point across pages."""
        page_token: str | None = None
        while True:
            page = self.list_data_points(
                data_type,
                filter=filter,
                page_size=page_size,
                page_token=page_token,
            )
            yield from page.get("dataPoints", [])
            page_token = page.get("nextPageToken") or None
            if not page_token:
                return

    def daily_roll_up(self, data_type: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp",
            json=body,
        )

    def roll_up(self, data_type: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"users/me/dataTypes/{data_type}/dataPoints:rollUp",
            json=body,
        )

    def iter_roll_up(
        self,
        data_type: str,
        *,
        start: datetime,
        end: datetime,
        window_seconds: int,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate ``rollupDataPoints`` across pages.

        ``window_seconds`` is the aggregation window size; values up to one day
        (86400) work for most types. Yields each ``RollupDataPoint`` as a dict.
        """
        body: dict[str, Any] = {
            "range": {
                "startTime": _rfc3339(start),
                "endTime": _rfc3339(end),
            },
            "windowSize": f"{window_seconds}s",
        }
        if page_size is not None:
            body["pageSize"] = page_size
        while True:
            page = self.roll_up(data_type, body)
            yield from page.get("rollupDataPoints", [])
            page_token = page.get("nextPageToken") or None
            if not page_token:
                return
            body = {**body, "pageToken": page_token}


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and "message" in error:
            return str(error["message"])
        if isinstance(error, str):
            return error
    return fallback or "(no body)"
