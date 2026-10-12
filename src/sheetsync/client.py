import asyncio
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import google.auth.transport.requests
import httpx
from google.oauth2 import service_account
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from sheetsync.codecs import DEFAULT_CODECS, Codec
from sheetsync.errors import ApiError

if TYPE_CHECKING:
    from sheetsync.schema import TableSchema
    from sheetsync.table import Table

BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets/"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_WAIT = 32.0
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # the request never left


class _Retryable(Exception):
    def __init__(self, error: ApiError, retry_after: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.retry_after = retry_after


def retry_wait(backoff: float) -> Callable[[RetryCallState], float]:
    """`Retry-After` seconds if the failed attempt carried it, else exponential backoff with jitter (cap 32 s)."""

    def wait(state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            return exc.retry_after
        delay = backoff * 2 ** (state.attempt_number - 1)
        return min(MAX_WAIT, delay * (1 + random.random()))

    return wait


def _retry_after(response: httpx.Response) -> float | None:
    """`Retry-After` in seconds; accepts both the delta-seconds and the HTTP-date form."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when.replace(tzinfo=when.tzinfo or UTC) - datetime.now(UTC)).total_seconds())


class SheetsClient:
    def __init__(
        self,
        service_account_info: Mapping | None = None,
        *,
        credentials: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 5,
        backoff: float = 1.0,
    ) -> None:
        if (service_account_info is None) == (credentials is None):
            raise ValueError("pass exactly one of service_account_info or credentials")
        if credentials is None:
            credentials = service_account.Credentials.from_service_account_info(
                dict(service_account_info),  # type: ignore[arg-type]
                scopes=[SCOPE],
            )
        self._credentials = credentials
        self._http = httpx.AsyncClient(transport=transport)
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._refresh_lock = asyncio.Lock()

    async def __aenter__(self) -> "SheetsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def table(
        self, spreadsheet_id: str, gid: int, schema: "TableSchema", codecs: Mapping[str, Codec] = DEFAULT_CODECS
    ) -> "Table":
        from sheetsync.table import Table

        return Table(self, spreadsheet_id, gid, schema, codecs)

    async def get_spreadsheet(self, spreadsheet_id: str, fields: str) -> dict:
        return await self._request("GET", spreadsheet_id, params={"fields": fields})

    async def values_batch_get(self, spreadsheet_id: str, ranges: list[str]) -> dict:
        params = [("ranges", r) for r in ranges] + [
            ("valueRenderOption", "UNFORMATTED_VALUE"),
            ("dateTimeRenderOption", "SERIAL_NUMBER"),
            ("majorDimension", "ROWS"),
        ]
        return await self._request("GET", f"{spreadsheet_id}/values:batchGet", params=params)

    async def values_batch_update(self, spreadsheet_id: str, data: list[dict]) -> dict:
        body = {"valueInputOption": "RAW", "data": data}
        return await self._request("POST", f"{spreadsheet_id}/values:batchUpdate", json=body)

    async def values_batch_update_by_data_filter(self, spreadsheet_id: str, data: list[dict]) -> dict:
        body = {"valueInputOption": "RAW", "data": data}
        return await self._request("POST", f"{spreadsheet_id}/values:batchUpdateByDataFilter", json=body)

    async def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> dict:
        # structural requests are positional, not idempotent: replaying one after a lost response corrupts the sheet
        return await self._request(
            "POST", f"{spreadsheet_id}:batchUpdate", json={"requests": requests}, idempotent=False
        )

    async def search_developer_metadata(self, spreadsheet_id: str, data_filters: list[dict]) -> dict:
        return await self._request(
            "POST", f"{spreadsheet_id}/developerMetadata:search", json={"dataFilters": data_filters}
        )

    async def _token(self) -> str:
        async with self._refresh_lock:
            if not self._credentials.valid:
                await asyncio.to_thread(self._credentials.refresh, google.auth.transport.requests.Request())
        return self._credentials.token

    async def _attempt(self, method: str, url: str, params: Any, json: Any, idempotent: bool) -> dict:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._http.request(method, url, params=params, json=json, headers=headers)
        except httpx.TransportError as exc:
            if idempotent or isinstance(exc, PRE_SEND_ERRORS):
                raise _Retryable(ApiError(0, str(exc))) from exc
            raise ApiError(0, str(exc)) from exc
        if response.is_success:
            return response.json()
        error = ApiError(response.status_code, response.text)
        if response.status_code in RETRY_STATUSES and (idempotent or response.status_code == 429):
            raise _Retryable(error, _retry_after(response))
        raise error

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None, idempotent: bool = True
    ) -> dict:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(self._max_attempts),
            wait=retry_wait(self._backoff),
            reraise=True,
        )
        try:
            return await retrying(self._attempt, method, BASE_URL + path, params, json, idempotent)
        except _Retryable as exc:
            raise exc.error from exc
