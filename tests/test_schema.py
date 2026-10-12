from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from sheetsync.codecs import DEFAULT_CODECS, NumberFormat
from sheetsync.errors import SchemaError
from sheetsync.schema import (
    Col,
    Column,
    Owner,
    TableSchema,
    decode_row,
    encode_record,
    get_path,
    resolve_columns,
)

TZ = ZoneInfo("UTC")


def schema(*cols: Column, **opts) -> TableSchema:
    opts.setdefault("key_field", cols[0].field)
    return TableSchema(columns=list(cols), **opts)


def col(field: str, header: str | None = None, **kw) -> Column:
    return Column(field=field, header=header or field, **kw)


# --- validation -----------------------------------------------------------------------------------------------------


def test_duplicate_field_rejected():
    with pytest.raises(SchemaError, match="duplicate"):
        TableSchema.from_json(
            {"columns": [{"field": "a", "index": 0}, {"field": "a", "index": 1}], "key_field": "a"}, DEFAULT_CODECS
        )


def test_column_needs_header_or_index():
    with pytest.raises(SchemaError, match="header or an index"):
        TableSchema.from_json({"columns": [{"field": "a"}], "key_field": "a"}, DEFAULT_CODECS)


def test_key_field_must_name_column_for_key_column_identity():
    data = {"columns": [{"field": "a", "index": 0}], "key_field": "zzz"}
    TableSchema.from_json(data, DEFAULT_CODECS)  # metadata identity: key_field need not be a column
    for identity in ("key_column", "metadata+key_column"):
        with pytest.raises(SchemaError, match="zzz"):
            TableSchema.from_json({**data, "identity": identity}, DEFAULT_CODECS)


def test_key_field_required():
    with pytest.raises(SchemaError, match="key_field"):
        TableSchema.from_json({"columns": [{"field": "a", "index": 0}]}, DEFAULT_CODECS)


def test_unknown_codec_rejected():
    data = {"columns": [{"field": "a", "index": 0, "codec": "nope"}], "key_field": "a"}
    with pytest.raises(SchemaError, match="nope"):
        TableSchema.from_json(data, DEFAULT_CODECS)


def test_from_json_valid_with_defaults():
    s = TableSchema.from_json({"columns": [{"field": "a", "header": "A"}], "key_field": "a"}, DEFAULT_CODECS)
    assert (s.header_row, s.first_data_row, s.identity, s.metadata_prefix, s.on_app_delete) == (
        1,
        2,
        "metadata",
        "k:",
        "delete_row",
    )
    assert s.columns[0].owner is Owner.APP


def test_first_data_row_explicit():
    assert schema(col("a"), header_row=3, data_start_row=10).first_data_row == 10
    assert schema(col("a"), header_row=3).first_data_row == 4


def test_format_resolution_order():
    col_fmt = NumberFormat(type="DATE", pattern="dd.mm.yyyy")
    default_fmt = NumberFormat(type="DATE", pattern="yyyy-mm-dd")
    s = schema(
        col("a", codec="date", format=col_fmt),
        col("b", codec="date"),
        col("c", codec="int"),
        formats={"date": default_fmt},
    )
    a, b, c = s.columns
    assert s.format_for(a) == col_fmt
    assert s.format_for(b) == default_fmt
    assert s.format_for(c) is None


# --- header resolution ----------------------------------------------------------------------------------------------


def test_resolve_header_case_and_space_insensitive():
    s = schema(col("a", "Name"), col("b", "Score"))
    assert resolve_columns(s, ["  score ", "x", " NAME"]) == {"a": 2, "b": 0}


def test_resolve_index_only():
    s = schema(Column(field="a", index=3))
    assert resolve_columns(s, []) == {"a": 3}


def test_resolve_pinned_header_matches():
    s = schema(Column(field="a", header="Name", index=1))
    assert resolve_columns(s, ["Name", "name"]) == {"a": 1}


def test_resolve_pin_mismatch_is_error():
    s = schema(Column(field="a", header="Name", index=0))
    with pytest.raises(SchemaError, match="Name"):
        resolve_columns(s, ["inserted", "Name"])
    with pytest.raises(SchemaError):
        resolve_columns(s, [])  # pin beyond the header row


def test_resolve_duplicate_header_is_error():
    s = schema(col("a", "Name"))
    with pytest.raises(SchemaError, match="2 times"):
        resolve_columns(s, ["Name", "name"])


def test_resolve_missing_header_error_for_app_and_sheet():
    for owner in (Owner.APP, Owner.SHEET):
        with pytest.raises(SchemaError, match="not found"):
            resolve_columns(schema(col("a", "Name", owner=owner)), ["other"])


def test_resolve_missing_skip_column_omitted():
    s = schema(col("a", "Name"), col("b", "Notes", owner=Owner.SKIP))
    assert resolve_columns(s, ["Name"]) == {"a": 0}
    assert resolve_columns(s, ["Name", "Notes"]) == {"a": 0, "b": 1}


def test_resolve_skip_column_pin_mismatch_is_error():
    s = schema(col("a", "Name"), Column(field="b", header="Notes", index=0, owner=Owner.SKIP))
    with pytest.raises(SchemaError, match="Notes"):
        resolve_columns(s, ["Name", "Notes"])  # header exists, but not at the pinned index
    assert resolve_columns(s, ["Name", "Other"]) == {"a": 0}  # fully missing header is tolerated


def test_resolve_none_header_cell():
    assert resolve_columns(schema(col("a", "Name")), [None, 5, "Name"]) == {"a": 2}


# --- get_path -------------------------------------------------------------------------------------------------------


def test_get_path():
    rec = {"a": 0, "price": {"dollar": None}}
    assert get_path(rec, "a") == 0
    assert get_path(rec, "price.dollar") is None
    for bad in ("b", "price.euro", "a.x"):
        with pytest.raises(KeyError):
            get_path(rec, bad)


# --- decode ---------------------------------------------------------------------------------------------------------


def test_decode_basic_flat_dotted():
    s = schema(col("id", codec="int"), col("price.dollar", "Price", codec="float"), col("name"))
    idx = {"id": 0, "price.dollar": 1, "name": 2}
    values, errors, warnings = decode_row(s, idx, [7, 1.5, "bob"], 5, DEFAULT_CODECS, TZ)
    assert values == {"id": 7, "price.dollar": 1.5, "name": "bob"}
    assert errors == [] and warnings == []


def test_decode_empty_cells_and_padding():
    s = schema(col("a"), col("b"), col("c"), col("d"))
    idx = {"a": 0, "b": 1, "c": 2, "d": 3}
    values, errors, _ = decode_row(s, idx, ["x", "", "  "], 2, DEFAULT_CODECS, TZ)  # d is beyond the row
    assert values == {"a": "x", "b": None, "c": None, "d": None}
    assert errors == []


def test_decode_required_empty_and_padded():
    s = schema(col("a", required=True), col("b", required=True))
    values, errors, _ = decode_row(s, {"a": 0, "b": 1}, [" "], 9, DEFAULT_CODECS, TZ)
    assert values == {"a": None, "b": None}
    assert [(e.row_number, e.field, e.raw, e.message) for e in errors] == [
        (9, "a", " ", "required"),
        (9, "b", None, "required"),
    ]


def test_decode_collects_all_errors_per_row():
    s = schema(col("a", codec="int"), col("b", codec="int"), col("c", codec="int"))
    values, errors, _ = decode_row(s, {"a": 0, "b": 1, "c": 2}, ["x", 2, "y"], 4, DEFAULT_CODECS, TZ)
    assert values == {"a": None, "b": 2, "c": None}
    assert [(e.field, e.raw) for e in errors] == [("a", "x"), ("c", "y")]
    assert all(e.row_number == 4 and e.message for e in errors)


def test_decode_skips_skip_columns_and_unresolved():
    s = schema(col("a"), col("b", owner=Owner.SKIP), col("c", owner=Owner.SHEET))
    values, errors, _ = decode_row(s, {"a": 0, "b": 1}, ["x", "bad"], 2, DEFAULT_CODECS, TZ)
    assert values == {"a": "x"} and errors == []


def test_decode_passes_options_and_format_to_codec():
    fmt = NumberFormat(type="DATE", pattern="dd.mm.yyyy")
    s = schema(
        col("tags", codec="list", options={"separator": ";"}),
        col("d", codec="date"),
        formats={"date": fmt},
    )
    values, errors, warnings = decode_row(s, {"tags": 0, "d": 1}, ["a;b", "31.12.2024"], 2, DEFAULT_CODECS, TZ)
    assert values == {"tags": ["a", "b"], "d": date(2024, 12, 31)}
    assert errors == [] and warnings == []


def test_decode_warnings_bound_per_column():
    s = schema(
        col("a", codec="date", format=NumberFormat(type="DATE", pattern="yyyy-mm-dd")),
        col("b", codec="date", format=NumberFormat(type="DATE", pattern="yyyy-mm-dd")),
    )
    values, errors, warnings = decode_row(s, {"a": 0, "b": 1}, ["31.12.2024", "2024-12-31"], 6, DEFAULT_CODECS, TZ)
    assert values == {"a": date(2024, 12, 31), "b": date(2024, 12, 31)}
    assert errors == []
    assert len(warnings) == 1
    w = warnings[0]
    assert (w.row_number, w.field, w.raw) == (6, "a", "31.12.2024")
    assert w.matched_format


def test_decode_options_are_read_only():
    seen = {}

    class Spy:
        def decode(self, raw, ctx):
            seen["opts"] = ctx.options
            return raw

    s = schema(col("a", codec="spy", options={"k": 1}))
    decode_row(s, {"a": 0}, ["x"], 2, {"spy": Spy()}, TZ)
    assert seen["opts"]["k"] == 1
    with pytest.raises(TypeError):
        seen["opts"]["k"] = 2


# --- encode ---------------------------------------------------------------------------------------------------------


def test_encode_width_and_non_app_positions_none():
    s = schema(col("a"), col("b", owner=Owner.SHEET), col("c", owner=Owner.SKIP), col("d", codec="int"))
    idx = {"a": 0, "b": 1, "c": 2, "d": 4}
    row = encode_record(s, idx, {"a": "x", "d": 3}, DEFAULT_CODECS, TZ)
    assert row == ["x", None, None, None, 3]


def test_encode_none_becomes_empty_string():
    s = schema(col("a"), col("b", codec="int"))
    assert encode_record(s, {"a": 0, "b": 1}, {"a": None, "b": None}, DEFAULT_CODECS, TZ) == ["", ""]


def test_encode_falsy_values_preserved():
    s = schema(col("i", codec="int"), col("f", codec="float"), col("b", codec="bool"), col("s"))
    idx = {"i": 0, "f": 1, "b": 2, "s": 3}
    row = encode_record(s, idx, {"i": 0, "f": 0.0, "b": False, "s": ""}, DEFAULT_CODECS, TZ)
    assert row == [0, 0.0, False, ""]
    assert row[0] is not False and row[2] is False


def test_encode_nested_path_and_datetime():
    s = schema(col("price.dollar", "Price", codec="float"), col("at", codec="datetime"))
    dt = datetime(2024, 1, 2, 3, 4, tzinfo=TZ)
    row = encode_record(s, {"price.dollar": 0, "at": 1}, {"price": {"dollar": 2.5}, "at": dt}, DEFAULT_CODECS, TZ)
    assert row[0] == 2.5 and isinstance(row[1], float)


def test_encode_missing_field_raises_schema_error():
    s = schema(col("a"), col("price.dollar", "Price"))
    with pytest.raises(SchemaError, match=r"price\.dollar"):
        encode_record(s, {"a": 0, "price.dollar": 1}, {"a": "x", "price": {}}, DEFAULT_CODECS, TZ)


# --- from_model -----------------------------------------------------------------------------------------------------


class Price(BaseModel):
    dollar: Annotated[float, Col(header="USD")]
    note: str = ""  # no Col -> ignored


class Item(BaseModel):
    id: Annotated[int, Col(header="Id")]
    name: Annotated[str | None, Col(header="Name", required=True)] = None
    seen: Annotated[date | None, Col(index=2)] = None
    at: Annotated[datetime, Col(header="At", owner=Owner.SHEET)] = datetime(2000, 1, 1)
    kind: Annotated[str, Col(header="Kind", codec="enum", options={"valid_values": ["a"]})] = "a"
    price: Price = Price(dollar=1)
    ignored: int = 0


def test_from_model_columns():
    s = TableSchema.from_model(Item, key_field="id", header_row=2)
    assert s.header_row == 2
    assert [(c.field, c.codec) for c in s.columns] == [
        ("id", "int"),
        ("name", "str"),
        ("seen", "date"),
        ("at", "datetime"),
        ("kind", "enum"),
        ("price.dollar", "float"),
    ]
    by_field = {c.field: c for c in s.columns}
    assert by_field["name"].required and by_field["name"].header == "Name"
    assert by_field["seen"].index == 2 and by_field["seen"].header is None
    assert by_field["at"].owner is Owner.SHEET
    assert by_field["kind"].options == {"valid_values": ["a"]}
    s.validate_codecs(DEFAULT_CODECS)


def test_from_model_optional_of_union_syntax_and_typing_optional():
    from typing import Optional

    class M(BaseModel):
        a: Annotated[Optional[int], Col(header="A")] = None  # noqa: UP045
        b: Annotated[bool | None, Col(header="B")] = None

    s = TableSchema.from_model(M, key_field="a")
    assert [c.codec for c in s.columns] == ["int", "bool"]


def test_from_model_wraps_validation_errors():
    class M(BaseModel):
        a: Annotated[int, Col()] = 0  # neither header nor index

    with pytest.raises(SchemaError, match="header or an index"):
        TableSchema.from_model(M, key_field="a")
    with pytest.raises(SchemaError, match="key_field"):
        TableSchema.from_model(Item)
