import json
from datetime import date

import httpx
import pytest

from sheetsync import (
    AsyncioLock,
    Column,
    RowState,
    SheetsClient,
    SheetsyncError,
    TableSchema,
    app_cells,
    app_hash,
    plan_sync,
)
from sheetsync.codecs import DEFAULT_CODECS, NumberFormat

SID = "sheet123"
GID = 42
HEADER = ["ID", "Qty", "Note", "Day", "Price", "Flag"]
SERIAL = (date(2026, 1, 1) - date(1899, 12, 30)).days
DAY_FORMAT = {"type": "DATE", "pattern": "dd.mm.yyyy"}
QTY_FORMAT = {"type": "NUMBER"}  # no pattern -> no "pattern" key


class FakeCredentials:
    valid = True
    token = "tok"


def make_schema(**kw) -> TableSchema:
    return TableSchema(
        columns=[
            Column(field="id", header="ID"),
            Column(field="qty", header="Qty", codec="int", format=NumberFormat(type="NUMBER")),
            Column(field="note", header="Note", owner="sheet"),
            Column(field="day", header="Day", codec="date", format=NumberFormat(type="DATE", pattern="dd.mm.yyyy")),
            Column(field="price", header="Price", codec="float"),
            Column(field="flag", header="Flag", codec="bool"),
        ],
        key_field="id",
        **kw,
    )


def sheet_row(key, qty=1, note="n", day=SERIAL, price=1.5, flag=True):
    return [key, qty, note, day, price, flag]


def rec(key, qty=1, day=date(2026, 1, 1), price=1.5, flag=True):
    return {"id": key, "qty": qty, "day": day, "price": price, "flag": flag}


def world(rows, *, tags=None, grid_rows=100, **schema_kw):
    """A Table over a fake tab ("It's", gid 42); `writes` collects every non-read request as (path, json)."""
    meta = {
        "properties": {"timeZone": "Europe/Moscow", "locale": "ru_RU"},
        "sheets": [
            {"properties": {"sheetId": GID, "title": "It's", "gridProperties": {"rowCount": grid_rows}}},
        ],
    }
    found = {
        "matchedDeveloperMetadata": [
            {"developerMetadata": {"metadataKey": f"k:{k}", "location": {"dimensionRange": {"startIndex": r - 1}}}}
            for r, v in (tags or {}).items()
            for k in ([v] if isinstance(v, str) else v)
        ]
    }
    writes: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/v4/spreadsheets/{SID}")
        if path == "/developerMetadata:search":
            return httpx.Response(200, json=found)
        if path == "/values:batchGet":
            return httpx.Response(200, json={"valueRanges": [{"values": [HEADER, *rows]}]})
        if request.method == "GET":
            return httpx.Response(200, json=meta)
        writes.append((path, json.loads(request.content)))
        return httpx.Response(200, json={})

    client = SheetsClient(credentials=FakeCredentials(), transport=httpx.MockTransport(handler))
    return client.table(SID, GID, make_schema(**schema_kw)), writes


async def apply(table, records, states=None, **kw):
    plan = plan_sync(table.schema, await table.read(), records, states or {})
    return await table.apply(plan, **kw)


def hash_of(table, row):
    indexes = {c.field: i for i, c in enumerate(table.schema.columns)}
    return app_hash(app_cells(table.schema, indexes, row))


def lookup(key):
    return {
        "developerMetadataLookup": {
            "metadataKey": f"k:{key}",
            "locationType": "ROW",
            "metadataLocation": {"sheetId": GID},
        }
    }


def kinds(batch):
    return [next(iter(r)) for r in batch["requests"]]


# --- tag edge cases --------------------------------------------------------------------------------------------------


async def test_multi_tag_row_is_never_written_through_a_data_filter():
    table, writes = world([sheet_row("a"), sheet_row("b")], tags={2: "a", 3: ["a", "b"]})
    snap = await table.read()
    assert snap.rows == {} and snap.duplicates == {"a": [2, 3], "b": [3]}
    plan = plan_sync(table.schema, snap, {"a": rec("a", qty=9), "b": rec("b", qty=9)}, {})
    assert (plan.creates, plan.updates) == ({}, {})
    await table.apply(plan)
    assert not any(path.startswith("/values:") for path, _ in writes)


async def test_blank_tagged_rows_stay_data_rows():
    table, writes = world([sheet_row("a"), ["", "", "", "", "", ""]], tags={2: "a", 3: "b", 4: "c"})
    snap = await table.read()
    assert snap.last_data_row == 4
    states = {"b": RowState("b", 3, "x"), "c": RowState("c", 4, "x")}
    plan = plan_sync(table.schema, snap, {"a": rec("a"), "b": rec("b"), "c": rec("c"), "d": rec("d")}, states)
    assert plan.missing_in_sheet == []
    assert list(plan.creates) == ["d"]
    await table.apply(plan)
    batch = next(body for path, body in writes if path == ":batchUpdate")
    assert batch["requests"][0]["updateCells"]["start"]["rowIndex"] == 4  # new row 5, not on tagged row 3/4


# --- value writes ----------------------------------------------------------------------------------------------------


async def test_update_by_data_filter_skips_sheet_columns():
    table, writes = world([sheet_row("a")], tags={2: "a"})
    result = await apply(table, {"a": rec("a", qty=5)}, repair_formats=False)
    cells = ["a", 5, None, SERIAL, 1.5, True]  # the sheet-owned Note position is null = leave alone
    assert writes == [
        (
            "/values:batchUpdateByDataFilter",
            {
                "valueInputOption": "RAW",
                "data": [{"dataFilter": lookup("a"), "majorDimension": "ROWS", "values": [cells]}],
            },
        )
    ]
    assert result.states == {"a": RowState("a", 2, hash_of(table, cells))}
    assert result.removed == []


async def test_untagged_row_updated_by_a1_range():
    table, writes = world([sheet_row("a")], identity="key_column")
    await apply(table, {"a": rec("a", price=2.5)}, repair_formats=False)
    assert writes == [
        (
            "/values:batchUpdate",
            {
                "valueInputOption": "RAW",
                "data": [{"range": "'It''s'!A2", "values": [["a", 1, None, SERIAL, 2.5, True]]}],
            },
        )
    ]


# --- creates ---------------------------------------------------------------------------------------------------------


async def test_create_appends_rows_and_splits_runs_by_owner_and_format():
    table, writes = world([sheet_row("a")], tags={2: "a"}, grid_rows=3)
    records = {"a": rec("a"), "b": rec("b", price=None, flag=False), "c": rec("c", qty=7)}
    result = await apply(table, records, repair_formats=False)
    [(path, batch)] = writes
    assert path == ":batchUpdate"
    # grid has 3 rows, data ends at 2, two creates -> 1 missing row
    assert batch["requests"][0] == {"appendDimension": {"sheetId": GID, "dimension": "ROWS", "length": 1}}
    runs = [r["updateCells"] for r in batch["requests"][1:5]]
    # id | qty (formatted) | -- note is the sheet's -- | day (formatted) | price, flag
    assert [(r["start"], r["fields"]) for r in runs] == [
        ({"sheetId": GID, "rowIndex": 2, "columnIndex": 0}, "userEnteredValue"),
        ({"sheetId": GID, "rowIndex": 2, "columnIndex": 1}, "userEnteredValue,userEnteredFormat.numberFormat"),
        ({"sheetId": GID, "rowIndex": 2, "columnIndex": 3}, "userEnteredValue,userEnteredFormat.numberFormat"),
        ({"sheetId": GID, "rowIndex": 2, "columnIndex": 4}, "userEnteredValue"),
    ]
    assert runs[0]["rows"] == [{"values": [{"userEnteredValue": {"stringValue": s}}]} for s in "bc"]
    assert runs[1]["rows"][1] == {
        "values": [{"userEnteredValue": {"numberValue": 7}, "userEnteredFormat": {"numberFormat": QTY_FORMAT}}]
    }
    assert runs[2]["rows"][0] == {
        "values": [{"userEnteredValue": {"numberValue": SERIAL}, "userEnteredFormat": {"numberFormat": DAY_FORMAT}}]
    }
    assert runs[3]["rows"] == [
        {"values": [{}, {"userEnteredValue": {"boolValue": False}}]},  # "" clears: no userEnteredValue
        {"values": [{"userEnteredValue": {"numberValue": 1.5}}, {"userEnteredValue": {"boolValue": True}}]},
    ]
    tags = [r["createDeveloperMetadata"]["developerMetadata"] for r in batch["requests"][5:]]
    assert tags == [
        {
            "metadataKey": f"k:{k}",
            "location": {
                "dimensionRange": {"sheetId": GID, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row}
            },
            "visibility": "DOCUMENT",
        }
        for k, row in (("b", 3), ("c", 4))
    ]
    assert len(batch["requests"]) == 7
    assert (result.states["b"].row_number, result.states["c"].row_number) == (3, 4)
    assert result.states["b"].app_hash == hash_of(table, ["b", 1, None, SERIAL, "", False])


async def test_key_column_create_has_no_metadata_and_enough_grid():
    table, writes = world([sheet_row("a")], identity="key_column", grid_rows=10)
    await apply(table, {"a": rec("a"), "b": rec("b")}, repair_formats=False)
    [(_, batch)] = writes
    assert set(kinds(batch)) == {"updateCells"}


async def test_retag_untagged_rows_found_by_key_column():
    rows = [sheet_row("a"), sheet_row("b", qty=2)]
    table, writes = world(rows, tags={3: "b"}, identity="metadata+key_column")
    await apply(table, {"a": rec("a"), "b": rec("b", qty=2), "c": rec("c")}, repair_formats=False)
    [(_, batch)] = writes
    assert kinds(batch).count("updateCells") == 4
    tagged = [
        (
            r["createDeveloperMetadata"]["developerMetadata"]["metadataKey"],
            r["createDeveloperMetadata"]["developerMetadata"]["location"]["dimensionRange"]["startIndex"],
        )
        for r in batch["requests"]
        if "createDeveloperMetadata" in r
    ]
    assert tagged == [("k:c", 3), ("k:a", 1)]  # the create, then the untagged unchanged row; tagged "b" is left alone


async def test_untagged_update_is_written_by_range_then_tagged():
    table, writes = world([sheet_row("a")], identity="metadata+key_column")
    await apply(table, {"a": rec("a", qty=9)}, repair_formats=False)
    assert [p for p, _ in writes] == ["/values:batchUpdate", ":batchUpdate"]
    assert kinds(writes[1][1]) == ["createDeveloperMetadata"]


# --- format repair ---------------------------------------------------------------------------------------------------


async def test_repair_formats_repeat_cell_per_formatted_column():
    table, writes = world([sheet_row("a"), sheet_row("b")], identity="key_column")
    await apply(table, {"a": rec("a"), "b": rec("b")})
    [(_, batch)] = writes
    assert batch["requests"] == [
        {
            "repeatCell": {
                "range": {
                    "sheetId": GID,
                    "startRowIndex": 1,
                    "endRowIndex": 3,
                    "startColumnIndex": col,
                    "endColumnIndex": col + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": fmt}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }
        for col, fmt in ((1, QTY_FORMAT), (3, DAY_FORMAT))
    ]


async def test_no_repair_no_structural_call():
    table, writes = world([sheet_row("a")], identity="key_column")
    await apply(table, {"a": rec("a")}, repair_formats=False)
    assert writes == []


# --- deletes ---------------------------------------------------------------------------------------------------------


def stale(key):
    return RowState(key, None, "old")


async def test_delete_rows_bottom_up_after_creates_and_renumbers_states():
    rows = [sheet_row(k) for k in "abcd"]
    table, writes = world(rows, identity="key_column")
    result = await apply(
        table,
        {"a": rec("a"), "d": rec("d"), "e": rec("e")},
        {"b": stale("b"), "c": stale("c"), "z": stale("z")},
        repair_formats=False,
    )
    [(_, batch)] = writes
    assert kinds(batch) == ["updateCells"] * 4 + ["deleteDimension"] * 2  # `z` is not in the sheet: no request
    assert [r["deleteDimension"]["range"]["startIndex"] for r in batch["requests"][4:]] == [3, 2]
    assert batch["requests"][0]["updateCells"]["start"]["rowIndex"] == 5  # create goes after the last data row (5)
    assert result.removed == ["b", "c", "z"]
    assert {k: s.row_number for k, s in result.states.items()} == {"a": 2, "d": 3, "e": 4}


async def test_clear_policy_blanks_app_cells_and_drops_tag():
    rows = [sheet_row(k) for k in "abc"]
    table, writes = world(rows, tags={2: "a", 3: "b", 4: "c"}, on_app_delete="clear")
    result = await apply(table, {"a": rec("a"), "c": rec("c")}, {"b": stale("b")}, repair_formats=False)
    [(_, batch)] = writes
    start = lambda col: {"sheetId": GID, "rowIndex": 2, "columnIndex": col}  # noqa: E731
    assert batch["requests"] == [
        {"updateCells": {"rows": [{"values": values}], "start": start(col), "fields": "userEnteredValue"}}
        for col, values in ((0, [{}, {}]), (3, [{}, {}, {}]))
    ] + [{"deleteDeveloperMetadata": {"dataFilter": lookup("b")}}]
    assert {k: s.row_number for k, s in result.states.items()} == {"a": 2, "c": 4}  # nothing shifts
    assert result.removed == ["b"]


# --- push / delete ---------------------------------------------------------------------------------------------------


class RecordingLock:
    entered = 0

    async def __aenter__(self):
        self.entered += 1

    async def __aexit__(self, *exc):
        return None


async def test_push_creates_when_missing():
    table, writes = world([sheet_row("a")], tags={2: "a"})
    lock = RecordingLock()
    state = await table.push("b", rec("b"), lock=lock)
    assert state == RowState("b", 3, hash_of(table, ["b", 1, None, SERIAL, 1.5, True]))
    assert lock.entered == 1
    [(path, batch)] = writes
    assert path == ":batchUpdate"
    assert kinds(batch) == ["updateCells"] * 4 + ["createDeveloperMetadata"]  # no format repair on push


async def test_push_updates_when_present_and_skips_when_equal():
    table, writes = world([sheet_row("a")], tags={2: "a"})
    state = await table.push("a", rec("a", qty=3), lock=AsyncioLock())
    assert [p for p, _ in writes] == ["/values:batchUpdateByDataFilter"]
    assert state.row_number == 2 and state.app_hash == hash_of(table, ["a", 3, None, SERIAL, 1.5, True])
    writes.clear()
    state = await table.push("a", rec("a"))
    assert writes == []
    assert state.app_hash == hash_of(table, sheet_row("a"))


async def test_push_duplicate_key_raises():
    table, writes = world([sheet_row("a"), sheet_row("a")], identity="key_column")
    with pytest.raises(SheetsyncError, match="duplicated"):
        await table.push("a", rec("a"))
    assert writes == []


async def test_delete_removes_row_and_ignores_absent_key():
    table, writes = world([sheet_row("a"), sheet_row("b")], identity="key_column")
    lock = RecordingLock()
    await table.delete("b", stale("b"), lock=lock)
    assert lock.entered == 1
    [(path, batch)] = writes
    assert (path, batch["requests"]) == (
        ":batchUpdate",
        [{"deleteDimension": {"range": {"sheetId": GID, "dimension": "ROWS", "startIndex": 2, "endIndex": 3}}}],
    )
    writes.clear()
    await table.delete("zzz", stale("zzz"))
    assert writes == []


class SkipCodec:
    def decode(self, raw, ctx):
        return raw

    def encode(self, value, ctx):
        return None  # leave the sheet cell alone


async def test_state_hash_of_skipped_app_cell_uses_the_sheet_cell():
    table, writes = world([["a", 1, "n", SERIAL, 1.5, True]])
    schema = TableSchema(
        columns=[Column(field="id", header="ID"), Column(field="qty", header="Qty", codec="skip")],
        key_field="id",
        identity="key_column",
    )
    table = table.client.table(SID, GID, schema, {**DEFAULT_CODECS, "skip": SkipCodec()})
    plan = plan_sync(schema, await table.read(), {"a": {"id": "a", "qty": 5}}, {}, codecs=table.codecs)
    assert plan.updates == {}  # sheet keeps 1, nothing to write
    result = await table.apply(plan)
    assert result.states["a"].app_hash == app_hash(["a", 1])


async def test_empty_tag_row_survives_a_stale_state_for_empty_key():
    table, writes = world([sheet_row("a"), sheet_row("x")], tags={2: "a", 3: ""})
    snap = await table.read()
    assert list(snap.rows) == ["a"] and snap.orphans == [3]
    plan = plan_sync(table.schema, snap, {"a": rec("a")}, {"": RowState("", 3, "x")})
    result = await table.apply(plan)
    assert "" not in result.states
    assert not any(k in ("deleteDimension", "updateCells") for _, b in writes for k in kinds(b) if "requests" in b)


async def test_written_row_hash_mixes_written_and_skipped_cells():
    table, _ = world([["a", 1, "n", SERIAL, 1.5, True]])
    schema = TableSchema(
        columns=[
            Column(field="id", header="ID"),
            Column(field="qty", header="Qty", codec="skip"),
            Column(field="price", header="Price", codec="float"),
        ],
        key_field="id",
        identity="key_column",
    )
    table = table.client.table(SID, GID, schema, {**DEFAULT_CODECS, "skip": SkipCodec()})
    record = {"id": "a", "qty": 99, "price": 2.5}
    plan = plan_sync(schema, await table.read(), {"a": record}, {}, codecs=table.codecs)
    assert "a" in plan.updates
    result = await table.apply(plan)
    assert result.states["a"].app_hash == app_hash(["a", 1, 2.5])  # qty stays 1 in the sheet, price becomes 2.5
