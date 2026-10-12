import json
from datetime import date
from zoneinfo import ZoneInfo

import httpx
import pytest

from sheetsync import (
    DEFAULT_CODECS,
    Column,
    PublicCsvTable,
    RowDecodeError,
    SchemaError,
    SheetsClient,
    SourceError,
    Table,
    TableSchema,
    build_snapshot,
)
from sheetsync.codecs import NumberFormat

TZ = ZoneInfo("Europe/Moscow")
SID = "sheet123"


def make_schema(**kw) -> TableSchema:
    return TableSchema(
        columns=[
            Column(field="id", header="ID", codec="str"),
            Column(field="qty", header="Qty", codec="int"),
            Column(field="note", header="Note", owner="sheet"),
        ],
        key_field="id",
        **kw,
    )


HEADER = ["ID", "Qty", "Note"]


def snap(rows, tags=None, grid_rows=100, strict=False, **schema_kw):
    return build_snapshot(make_schema(**schema_kw), DEFAULT_CODECS, TZ, rows, tags or {}, grid_rows, strict=strict)


def test_key_column_rows_and_raw():
    s = snap([HEADER, ["a", 1, "x"], ["b", 2]], identity="key_column")
    assert list(s.rows) == ["a", "b"]
    a = s.rows["a"]
    assert (a.row_number, a.values, a.raw, a.tagged) == (2, {"id": "a", "qty": 1, "note": "x"}, ("a", 1, "x"), False)
    assert s.rows["b"].raw == ("b", 2)  # as returned, not padded
    assert s.rows["b"].values["note"] is None
    assert s.indexes == {"id": 0, "qty": 1, "note": 2}
    assert (s.tz, s.last_data_row, s.grid_rows) == (TZ, 3, 100)


def test_tagged_orphan_and_duplicate():
    rows = [HEADER, ["a", 1, ""], ["", 2, ""], ["dup", 3, ""], ["dup", 4, ""], ["x", 5, ""]]
    # metadata mode: key only from tags; untagged rows are orphans
    s = snap(rows, {2: ["t-a"], 5: ["t-dup"], 6: ["t-dup"]})
    assert list(s.rows) == ["t-a"]
    assert s.rows["t-a"].tagged is True
    assert s.orphans == [3, 4]
    assert s.duplicates == {"t-dup": [5, 6]}


def test_key_column_orphan_dup_and_tag_priority():
    rows = [HEADER, ["a", 1, ""], ["", 2, ""], ["dup", 3, ""], ["dup", 4, ""], ["a2", 5, ""], ["bad", "oops", ""]]
    s = snap(rows, {6: ["a"]}, identity="metadata+key_column")
    assert s.orphans == [3]
    assert s.duplicates == {"dup": [4, 5], "a": [2, 6]}  # tagged row 6 collides with untagged row 2 keyed "a"
    assert list(s.rows) == ["bad"]
    assert [(e.row_number, e.field) for e in s.errors] == [(7, "qty")]


def test_row_with_several_tags_makes_every_key_a_duplicate():
    rows = [HEADER, ["a", 1, ""], ["ab", 2, ""], ["c", 3, ""]]
    s = snap(rows, {2: ["t-a"], 3: ["t-a", "t-b"], 4: ["t-c"]})
    assert list(s.rows) == ["t-c"]
    assert s.duplicates == {"t-a": [2, 3], "t-b": [3]}
    assert s.orphans == []


def test_tagged_empty_rows_are_data_rows_even_past_the_values():
    rows = [HEADER, ["a", 1, ""], ["", "", ""]]
    s = snap(rows, {2: ["t-a"], 3: ["t-b"], 4: ["t-c"], 1: ["t-header"]})  # row 4 is absent from `rows`
    assert list(s.rows) == ["t-a", "t-b", "t-c"]
    assert [s.rows[k].row_number for k in s.rows] == [2, 3, 4]
    assert s.rows["t-c"].raw == ()
    assert s.last_data_row == 4
    assert s.orphans == []


def test_empty_tag_suffix_is_not_an_identity():
    rows = [HEADER, ["a", 1, ""], ["", "", ""]]
    s = snap(rows, {2: ["t-a"], 3: [""]})
    assert list(s.rows) == ["t-a"]
    assert s.orphans == [3] and s.last_data_row == 3 and s.duplicates == {}


def test_key_decoded_via_codec_as_str():
    schema = TableSchema(
        columns=[Column(field="id", header="ID", codec="int"), Column(field="d", header="D", codec="date")],
        key_field="id",
        identity="key_column",
    )
    s = build_snapshot(schema, DEFAULT_CODECS, TZ, [["ID", "D"], [7.0, 46000], ["bad", 46001]], {}, 10)
    assert list(s.rows) == ["7"]
    assert s.rows["7"].values["d"] == date(2025, 12, 9)
    assert s.orphans == [3]  # undecodable key is an orphan, its error is still reported
    assert s.errors[0].row_number == 3


def test_trailing_and_inner_empty_rows():
    rows = [HEADER, ["a", 1, ""], ["", "", ""], [], ["b", 2, ""], ["", " ", None], []]
    s = snap(rows, identity="key_column")
    assert list(s.rows) == ["a", "b"]
    assert s.last_data_row == 5
    assert s.orphans == []


def test_no_data_rows():
    s = snap([HEADER, [], []], identity="key_column")
    assert (s.rows, s.last_data_row) == ({}, 1)


def test_header_row_offset():
    rows = [["title"], ["junk"], HEADER, ["skipped?", 0, ""], ["a", 1, ""]]
    s = snap(rows, identity="key_column", header_row=3, data_start_row=5)
    assert list(s.rows) == ["a"]
    assert s.rows["a"].row_number == 5
    assert s.last_data_row == 5
    s = snap(rows[:3], identity="key_column", header_row=3)
    assert s.last_data_row == 3


def test_missing_header_is_schema_error():
    with pytest.raises(SchemaError):
        snap([["ID", "Qty"]], identity="key_column")


def test_strict_raises_first_error():
    rows = [HEADER, ["a", "x", ""], ["b", "y", ""]]
    with pytest.raises(RowDecodeError) as exc:
        snap(rows, identity="key_column", strict=True)
    assert exc.value.error.row_number == 2
    assert len(snap(rows, identity="key_column").errors) == 2


# --- Table.read ------------------------------------------------------------------------------------------------------


class FakeCredentials:
    valid = True
    token = "tok"


META = {
    "properties": {"timeZone": "Europe/Moscow", "locale": "ru_RU"},
    "sheets": [
        {"properties": {"sheetId": 1, "title": "Other", "gridProperties": {"rowCount": 5, "columnCount": 3}}},
        {"properties": {"sheetId": 42, "title": "It's", "gridProperties": {"rowCount": 77, "columnCount": 3}}},
    ],
}
VALUES = {"valueRanges": [{"values": [HEADER, ["a", 1, "x"], ["b", 2, "y"], ["c", 3, "z"]]}]}
TAGS = {
    "matchedDeveloperMetadata": [
        {"developerMetadata": {"metadataKey": "k:A", "location": {"dimensionRange": {"startIndex": 1}}}},
        {"developerMetadata": {"metadataKey": "other:zzz", "location": {"dimensionRange": {"startIndex": 2}}}},
        {"developerMetadata": {"metadataKey": "k:C", "location": {"dimensionRange": {"startIndex": 3}}}},
    ]
}


def make_client(values=VALUES):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("developerMetadata:search"):
            return httpx.Response(200, json=TAGS)
        if "values:batchGet" in path:
            return httpx.Response(200, json=values)
        return httpx.Response(200, json=META)

    return SheetsClient(credentials=FakeCredentials(), transport=httpx.MockTransport(handler)), seen


async def test_table_read_sequence_meta_refetch_and_quoting():
    client, seen = make_client()
    async with client:
        table = client.table(SID, 42, make_schema())
        assert isinstance(table, Table)
        snapshot = await table.read()
        await table.read()
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == [
        SID,
        "values:batchGet",
        "developerMetadata:search",
        SID,
        "values:batchGet",
        "developerMetadata:search",
    ]
    meta, values, search = seen[:3]
    assert meta.url.params["fields"] == (
        "properties(timeZone,locale),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    )
    assert values.url.params.get_list("ranges") == ["'It''s'"]
    assert json.loads(search.content) == {
        "dataFilters": [{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": 42}}}]
    }
    assert (table.title, table.grid_rows, table.tz) == ("It's", 77, TZ)
    assert snapshot.grid_rows == 77 and snapshot.tz == TZ
    assert list(snapshot.rows) == ["A", "C"]  # foreign-prefix tag ignored; row 3 is an orphan
    assert snapshot.rows["A"].row_number == 2 and snapshot.rows["A"].tagged
    assert snapshot.orphans == [3]


async def test_table_read_key_column_skips_metadata_and_empty_tab():
    client, seen = make_client({"valueRanges": [{"values": [HEADER]}]})
    async with client:
        snapshot = await client.table(SID, 42, make_schema(identity="key_column")).read()
    assert not any(r.url.path.endswith("developerMetadata:search") for r in seen)
    assert snapshot.rows == {} and snapshot.last_data_row == 1


async def test_table_read_empty_tab_is_schema_error():
    client, _ = make_client({"valueRanges": [{}]})
    async with client:
        with pytest.raises(SchemaError):
            await client.table(SID, 42, make_schema(identity="key_column")).read()


async def test_table_read_unknown_gid():
    client, _ = make_client()
    async with client:
        with pytest.raises(SchemaError, match="gid 99"):
            await client.table(SID, 99, make_schema()).read()


def test_table_unknown_codec():
    schema = TableSchema(columns=[Column(field="id", header="ID", codec="nope")], key_field="id")
    with pytest.raises(SchemaError):
        Table(None, SID, 1, schema)


# --- PublicCsvTable --------------------------------------------------------------------------------------------------

URL = f"https://docs.google.com/spreadsheets/d/{SID}/edit?usp=sharing#gid=42"


def csv_schema(pattern: str | None = None) -> TableSchema:
    return TableSchema(
        columns=[
            Column(field="id", header="ID"),
            Column(field="day", header="Day", codec="date", format=NumberFormat(type="DATE", pattern=pattern)),
        ],
        key_field="id",
        identity="key_column",
    )


def csv_table(response: httpx.Response, schema=None, url=URL):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return PublicCsvTable(url, schema or csv_schema(), tz=TZ, transport=httpx.MockTransport(handler)), seen


def csv_response(text: str, content_type: str = "text/csv; charset=utf-8", status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=text.encode(), headers={"content-type": content_type})


async def test_csv_happy_path_with_fallback_warning():
    body = '\ufeffID,Day\r\n"a, 1",05.03.2026\r\nb,\r\n'
    table, seen = csv_table(csv_response(body))
    snapshot = await table.read()
    assert str(seen[0].url) == f"https://docs.google.com/spreadsheets/d/{SID}/export?format=csv&gid=42"
    assert snapshot.rows["a, 1"].values["day"] == date(2026, 3, 5)
    assert snapshot.rows["b"].values["day"] is None
    assert [(w.row_number, w.field, w.matched_format) for w in snapshot.warnings] == [(2, "day", "dd.mm.yyyy")]
    assert (snapshot.grid_rows, snapshot.last_data_row, snapshot.tz) == (3, 3, TZ)


@pytest.mark.parametrize("suffix", ["?usp=sharing", "#gid=123", "/edit"])
async def test_csv_id_stops_at_query_fragment_and_slash(suffix):
    table, seen = csv_table(csv_response("ID,Day\n"), url=f"https://docs.google.com/spreadsheets/d/{SID}{suffix}")
    await table.read()
    assert seen[0].url.path == f"/spreadsheets/d/{SID}/export"


async def test_csv_url_without_gid_and_query_gid():
    table, seen = csv_table(csv_response("ID,Day\n"), url=f"https://docs.google.com/spreadsheets/d/{SID}/edit")
    await table.read()
    assert str(seen[0].url).endswith("/export?format=csv")
    table, seen = csv_table(csv_response("ID,Day\n"), url=f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=7")
    await table.read()
    assert str(seen[0].url).endswith("&gid=7")


@pytest.mark.parametrize(
    "response",
    [
        csv_response("<html>sign in</html>", "text/html; charset=utf-8"),
        csv_response("ID,Day\n", status=403),
    ],
)
async def test_csv_bad_response_is_source_error(response):
    table, _ = csv_table(response)
    with pytest.raises(SourceError):
        await table.read()


def test_csv_requires_key_column_identity():
    schema = csv_schema().model_copy(update={"identity": "metadata"})
    with pytest.raises(SchemaError, match="key_column"):
        PublicCsvTable(URL, schema, tz=TZ)


def test_csv_month_name_pattern_is_schema_error():
    with pytest.raises(SchemaError, match="mmm"):
        PublicCsvTable(URL, csv_schema("dd mmm yyyy"), tz=TZ)
    PublicCsvTable(URL, csv_schema("dd.mm.yyyy"), tz=TZ)  # numeric pattern is fine


def test_csv_bad_url():
    with pytest.raises(SourceError):
        PublicCsvTable("https://example.com/nope", csv_schema(), tz=TZ)
