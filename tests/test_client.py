import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from tenacity import RetryCallState

from sheetsync import ApiError, SheetsClient
from sheetsync.client import BASE_URL, _Retryable, retry_wait

SID = "sheet123"


class FakeCredentials:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.token = "tok"
        self.refreshes = 0

    def refresh(self, request) -> None:
        self.refreshes += 1
        self.valid = True


def make_client(handler, **kwargs) -> tuple[SheetsClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = SheetsClient(
        credentials=kwargs.pop("credentials", FakeCredentials()),
        transport=httpx.MockTransport(wrapped),
        backoff=0,
        **kwargs,
    )
    return client, seen


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def test_get_spreadsheet():
    client, seen = make_client(ok)
    async with client:
        assert await client.get_spreadsheet(SID, "sheets.properties") == {"ok": True}
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url.copy_with(query=None)) == BASE_URL + SID
    assert req.url.params["fields"] == "sheets.properties"
    assert req.headers["Authorization"] == "Bearer tok"


async def test_values_batch_get_params():
    client, seen = make_client(ok)
    async with client:
        await client.values_batch_get(SID, ["A1:B2", "C3:D4"])
    url = seen[0].url
    assert url.path.endswith(f"/{SID}/values:batchGet")
    assert url.params.get_list("ranges") == ["A1:B2", "C3:D4"]
    assert url.params["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert url.params["dateTimeRenderOption"] == "SERIAL_NUMBER"
    assert url.params["majorDimension"] == "ROWS"


@pytest.mark.parametrize(
    ("method", "path", "arg", "body"),
    [
        ("values_batch_update", "/values:batchUpdate", [{"range": "A1", "values": [[1]]}], "data"),
        ("values_batch_update_by_data_filter", "/values:batchUpdateByDataFilter", [{"dataFilter": {}}], "data"),
    ],
)
async def test_values_writes_use_raw(method, path, arg, body):
    client, seen = make_client(ok)
    async with client:
        await getattr(client, method)(SID, arg)
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path.endswith(f"/{SID}{path}")
    assert json.loads(req.content) == {"valueInputOption": "RAW", body: arg}


async def test_batch_update_and_metadata_search():
    client, seen = make_client(ok)
    async with client:
        await client.batch_update(SID, [{"addSheet": {}}])
        await client.search_developer_metadata(SID, [{"developerMetadataLookup": {}}])
    assert seen[0].url.path.endswith(f"/{SID}:batchUpdate")
    assert json.loads(seen[0].content) == {"requests": [{"addSheet": {}}]}
    assert seen[1].url.path.endswith(f"/{SID}/developerMetadata:search")
    assert json.loads(seen[1].content) == {"dataFilters": [{"developerMetadataLookup": {}}]}


async def test_retries_429_then_success():
    responses = iter([httpx.Response(429, text="slow"), httpx.Response(503), httpx.Response(200, json={"n": 1})])
    client, seen = make_client(lambda request: next(responses))
    async with client:
        assert await client.get_spreadsheet(SID, "f") == {"n": 1}
    assert len(seen) == 3


async def test_retries_transport_error():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={})

    client, _ = make_client(handler)
    async with client:
        assert await client.get_spreadsheet(SID, "f") == {}
    assert len(calls) == 2


async def test_non_retryable_raises_immediately_with_body():
    client, seen = make_client(lambda request: httpx.Response(400, text="bad range"))
    async with client:
        with pytest.raises(ApiError) as info:
            await client.get_spreadsheet(SID, "f")
    assert (info.value.status, info.value.body) == (400, "bad range")
    assert len(seen) == 1


async def test_exhaustion_raises_api_error():
    client, seen = make_client(lambda request: httpx.Response(500, text="down"), max_attempts=3)
    async with client:
        with pytest.raises(ApiError) as info:
            await client.get_spreadsheet(SID, "f")
    assert (info.value.status, info.value.body) == (500, "down")
    assert len(seen) == 3


async def test_transport_exhaustion_status_zero():
    def handler(request):
        raise httpx.ConnectError("no route")

    client, seen = make_client(handler, max_attempts=2)
    async with client:
        with pytest.raises(ApiError) as info:
            await client.get_spreadsheet(SID, "f")
    assert (info.value.status, info.value.body) == (0, "no route")
    assert len(seen) == 2


async def test_retry_after_header_is_parsed():
    responses = iter([httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={})])
    client, seen = make_client(lambda request: next(responses))
    async with client:
        await client.get_spreadsheet(SID, "f")
    assert len(seen) == 2


def _state(exc: Exception, attempt: int) -> RetryCallState:
    return SimpleNamespace(outcome=SimpleNamespace(exception=lambda: exc), attempt_number=attempt)  # type: ignore[return-value]


def test_wait_honours_retry_after():
    err = ApiError(429, "")
    assert retry_wait(1.0)(_state(_Retryable(err, 7.5), 1)) == 7.5


def test_wait_exponential_with_jitter_capped():
    wait = retry_wait(1.0)
    err = _Retryable(ApiError(500, ""))
    for attempt, low in [(1, 1.0), (2, 2.0), (3, 4.0)]:
        assert low <= wait(_state(err, attempt)) <= 2 * low
    assert wait(_state(err, 20)) == 32.0
    assert retry_wait(0)(_state(err, 3)) == 0


async def test_concurrent_calls_refresh_once():
    creds = FakeCredentials(valid=False)
    client, _ = make_client(ok, credentials=creds)
    async with client:
        await asyncio.gather(*(client.get_spreadsheet(SID, "f") for _ in range(5)))
    assert creds.refreshes == 1


def test_requires_exactly_one_credential_source():
    with pytest.raises(ValueError):
        SheetsClient()
    with pytest.raises(ValueError):
        SheetsClient({"a": 1}, credentials=FakeCredentials())
