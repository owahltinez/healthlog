"""The Google Health requests Healthlog makes."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from google.oauth2.credentials import Credentials

from healthlog.auth import get_credentials
from healthlog.datatypes import FOOD_ID, DataType, by_id
from healthlog.models import HeightLog, MealLog, WeightLog, point_time

API_BASE_URL = "https://health.googleapis.com/v4"


class GoogleHealthError(Exception):
    pass


class GoogleHealthClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        base_url: str = API_BASE_URL,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        credentials = self.credentials or get_credentials()
        if not credentials or not credentials.token:
            raise GoogleHealthError(
                "Not authenticated. Run 'healthlog auth login'."
            )
        return {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

    @contextmanager
    def _client(self) -> Generator[httpx.Client]:
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self.transport
            ) as client:
                yield client
        except httpx.HTTPStatusError as exc:
            message = _error_message(exc.response)
            raise GoogleHealthError(
                f"Google Health returned {exc.response.status_code}: {message}"
            ) from exc
        except httpx.RequestError as exc:
            raise GoogleHealthError(
                f"Google Health request failed: {exc}"
            ) from exc

    def log_meal(self, meal: MealLog) -> MealLog:
        url = f"{self.base_url}/users/me/dataTypes/nutrition-log/dataPoints"
        with self._client() as client:
            response = client.post(
                url, json=meal.to_api_payload(), headers=self._headers()
            )
            response.raise_for_status()
            data = response.json()
            saved = MealLog.from_api_payload(data.get("response", data))
            if saved.grams is None:
                saved.grams = meal.grams
            return saved

    def get_meal(self, point_id: str) -> MealLog:
        url = (
            f"{self.base_url}/users/me/dataTypes/nutrition-log/dataPoints/"
            f"{point_id}"
        )
        with self._client() as client:
            response = client.get(url, headers=self._headers())
            response.raise_for_status()
            return MealLog.from_api_payload(response.json())

    def delete_meal(self, point_id: str) -> None:
        url = (
            f"{self.base_url}/users/me/dataTypes/"
            "nutrition-log/dataPoints:batchDelete"
        )
        name = f"users/me/dataTypes/nutrition-log/dataPoints/{point_id}"
        with self._client() as client:
            response = client.post(
                url,
                json={"names": [name]},
                headers=self._headers(),
            )
            response.raise_for_status()

    def log_weight(self, weight: WeightLog) -> WeightLog:
        url = f"{self.base_url}/users/me/dataTypes/weight/dataPoints"
        with self._client() as client:
            response = client.post(
                url, json=weight.to_api_payload(), headers=self._headers()
            )
            response.raise_for_status()
            data = response.json()
            return WeightLog.from_api_payload(data.get("response", data))

    def log_height(self, height: HeightLog) -> HeightLog:
        url = f"{self.base_url}/users/me/dataTypes/height/dataPoints"
        with self._client() as client:
            response = client.post(
                url, json=height.to_api_payload(), headers=self._headers()
            )
            response.raise_for_status()
            data = response.json()
            return HeightLog.from_api_payload(data.get("response", data))

    def delete_point(self, data_type: DataType, point_id: str) -> None:
        """Delete one data point of any type this tool can write."""
        url = (
            f"{self.base_url}/users/me/dataTypes/"
            f"{data_type.id}/dataPoints:batchDelete"
        )
        name = f"users/me/dataTypes/{data_type.id}/dataPoints/{point_id}"
        with self._client() as client:
            response = client.post(
                url, json={"names": [name]}, headers=self._headers()
            )
            response.raise_for_status()

    def latest(self, data_type: DataType) -> dict[str, Any] | None:
        """The newest data point of a type, however old that is.

        A range read answers "what was recorded then". For a value that
        changes rarely, the question is "what is it", and a range finding
        nothing reads as nothing ever recorded.
        """
        url = f"{self.base_url}/users/me/dataTypes/{data_type.id}/dataPoints"
        with self._client() as client:
            response = client.get(
                url,
                params={"pageSize": min(5, data_type.page_size)},
                headers=self._headers(),
            )
            response.raise_for_status()
            page = response.json().get("dataPoints") or []

        # Points arrive newest first, but taking the newest of the page costs
        # one comparison and does not lean on that holding for every type.
        dated = [
            (moment, point)
            for point in page
            if (moment := point_time(point.get(data_type.payload_key) or {}))
        ]
        return max(dated, key=lambda pair: pair[0])[1] if dated else None

    def points(
        self,
        data_type: DataType,
        start: datetime,
        end: datetime,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Raw data points whose stated time falls in a half-open interval.

        The API spells its time filter differently for nearly every type, and
        rejects the wrong spelling outright, so the range is applied here. It
        does order points newest first, so a page holding nothing new enough
        ends the walk rather than reading a whole history to find four meals.
        """
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        url = f"{self.base_url}/users/me/dataTypes/{data_type.id}/dataPoints"
        params: dict[str, Any] = {"pageSize": data_type.page_size}
        collected: list[dict[str, Any]] = []

        with self._client() as client:
            while True:
                response = client.get(
                    url, params=params, headers=self._headers()
                )
                response.raise_for_status()
                body = response.json()
                page = body.get("dataPoints") or []

                stale = 0
                for point in page:
                    moment = point_time(point.get(data_type.payload_key) or {})
                    if moment is None:
                        continue
                    moment = moment.astimezone(UTC)
                    if moment < start:
                        stale += 1
                    elif moment < end:
                        collected.append(point)
                        if limit and len(collected) >= limit:
                            return collected

                token = body.get("nextPageToken")
                if not token or (page and stale == len(page)):
                    return collected
                params["pageToken"] = token

    def history(self, start: datetime, end: datetime) -> list[MealLog]:
        """Read nutrition logs within a half-open UTC interval."""
        return [
            MealLog.from_api_payload(point)
            for point in self.points(by_id(FOOD_ID), start, end)
        ]


def _error_message(response: httpx.Response) -> str:
    try:
        return str(
            response.json().get("error", {}).get("message") or response.text
        )
    except ValueError:
        return response.text
