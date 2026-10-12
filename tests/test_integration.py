"""Opt-in round trip against a real spreadsheet: SHEETSYNC_IT=1, SHEETSYNC_SA_JSON, SHEETSYNC_SPREADSHEET_ID."""

import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from sheetsync import DEFAULT_CODECS, AsyncioLock, SheetsClient, TableSchema, plan_sync

SA_JSON = os.environ.get("SHEETSYNC_SA_JSON")
SPREADSHEET_ID = os.environ.get("SHEETSYNC_SPREADSHEET_ID")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("SHEETSYNC_IT") == "1" and SA_JSON and SPREADSHEET_ID),
    reason="set SHEETSYNC_IT=1, SHEETSYNC_SA_JSON and SHEETSYNC_SPREADSHEET_ID to run against a real spreadsheet",
)

DAY_FORMAT = {"type": "DATE", "pattern": "dd.mm.yyyy"}
SCHEMA = {
    "columns": [
        {"field": "id", "header": "ID"},
        {"field": "qty", "header": "Qty", "codec": "int"},
        {"field": "day", "header": "Day", "codec": "date", "format": DAY_FORMAT},
        {"field": "note", "header": "Note", "owner": "sheet"},
    ],
    "key_field": "id",
}
HEADER = ["ID", "Qty", "Day", "Note"]
QTY_COL, DAY_COL = 1, 2  # 0-based sheet columns


@pytest.fixture
async def scratch():
    """(client, tab title, gid) of a fresh tab with the header row; the tab is deleted afterwards."""
    async with SheetsClient(json.loads(Path(SA_JSON).read_text())) as client:
        title = f"sheetsync-it-{uuid4().hex[:8]}"
        reply = await client.batch_update(SPREADSHEET_ID, [{"addSheet": {"properties": {"title": title}}}])
        gid = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
        try:
            await client.values_batch_update(SPREADSHEET_ID, [{"range": f"'{title}'!A1", "values": [HEADER]}])
            yield client, title, gid
        finally:
            await client.batch_update(SPREADSHEET_ID, [{"deleteSheet": {"sheetId": gid}}])


async def number_format(client: SheetsClient, gid: int, row_index: int, col_index: int) -> dict | None:
    # a field mask makes spreadsheets.get return grid data (includeGridData is ignored when `fields` is set)
    fields = "sheets(properties(sheetId),data(rowData(values(userEnteredFormat(numberFormat)))))"
    meta = await client.get_spreadsheet(SPREADSHEET_ID, fields)
    sheet = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == gid)
    cell = sheet["data"][0]["rowData"][row_index]["values"][col_index]
    return cell.get("userEnteredFormat", {}).get("numberFormat")


async def test_create_update_drift_delete_roundtrip(scratch):
    client, title, gid = scratch
    schema = TableSchema.from_json(SCHEMA, DEFAULT_CODECS)
    table = client.table(SPREADSHEET_ID, gid, schema)
    lock = AsyncioLock()
    records = {
        "a": {"id": "a", "qty": 1, "day": date(2026, 10, 12)},
        "b": {"id": "b", "qty": 2, "day": date(2026, 10, 13)},
    }

    # create (reconcile: read -> plan -> apply under one lock)
    async with lock:
        plan = plan_sync(schema, await table.read(), records, {})
        assert set(plan.creates) == {"a", "b"}
        states = (await table.apply(plan)).states

    # read: tags resolve, values decode, display formats are on the sheet
    snap = await table.read()
    assert {k: r.values["qty"] for k, r in snap.rows.items()} == {"a": 1, "b": 2}
    assert snap.rows["a"].values["day"] == date(2026, 10, 12)
    assert not snap.errors and not snap.duplicates and not snap.orphans
    assert await number_format(client, gid, 1, DAY_COL) == DAY_FORMAT
    assert await number_format(client, gid, 2, DAY_COL) == DAY_FORMAT

    # update via push
    records["a"]["qty"] = 5
    states["a"] = await table.push("a", records["a"], lock=lock)
    assert (await table.read()).rows["a"].values["qty"] == 5

    # manual edit of an app cell (row 3 = "b"), made through the API like a person would in the UI
    await client.values_batch_update(SPREADSHEET_ID, [{"range": f"'{title}'!B3", "values": [[99]]}])

    # drift: reported as (sheet value, app value), and the app value is written back
    async with lock:
        plan = plan_sync(schema, await table.read(), records, states)
        assert plan.drift == {"b": {"qty": (99, 2)}}
        assert set(plan.updates) == {"b"}
        states = (await table.apply(plan)).states
    assert (await table.read()).rows["b"].values["qty"] == 2

    # delete: row gone, the other row stays
    await table.delete("a", states["a"], lock=lock)
    snap = await table.read()
    assert set(snap.rows) == {"b"}
    assert snap.rows["b"].row_number == 2
