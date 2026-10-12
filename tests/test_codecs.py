from datetime import date, datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from sheetsync.codecs import DEFAULT_CODECS, Ctx, ListCodec, from_serial, to_serial

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def ctx(tz=MSK, **options):
    return Ctx(tz=tz, options=MappingProxyType(options))


def roundtrip(name, value, **options):
    c = ctx(**options)
    codec = DEFAULT_CODECS[name]
    return codec.decode(codec.encode(value, c), c)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("str", "hello"),
        ("str", "0"),
        ("int", 0),
        ("int", -7),
        ("int", 42),
        ("float", 0.0),
        ("float", 3.25),
        ("bool", False),
        ("bool", True),
        ("date", date(2026, 10, 12)),
        ("date", date(1899, 12, 30)),
    ],
)
def test_roundtrip(name, value):
    result = roundtrip(name, value)
    assert result == value
    assert type(result) is type(value)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 3, 28, 23, 30, tzinfo=BERLIN),  # before spring-forward
        datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN),  # after spring-forward (CEST)
        datetime(2026, 10, 24, 12, 0, 0, 123000, tzinfo=BERLIN),  # ms precision, CEST
        datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN),  # after fall-back (CET)
        datetime(2026, 10, 12, 12, 0, tzinfo=MSK),
    ],
)
def test_datetime_roundtrip_across_dst(value):
    c = ctx(tz=value.tzinfo)
    codec = DEFAULT_CODECS["datetime"]
    result = codec.decode(codec.encode(value, c), c)
    assert result == value
    assert result.utcoffset() == value.utcoffset()


def test_datetime_encode_converts_to_sheet_tz():
    utc_noon = datetime.fromisoformat("2026-10-12T09:00:00+00:00")
    assert DEFAULT_CODECS["datetime"].encode(utc_noon, ctx()) == 46307.5


def test_enum_and_list_roundtrip():
    assert roundtrip("enum", "b", valid_values=["a", "b"]) == "b"
    assert roundtrip("enum", 2, value_map={"one": 1, "two": 2}) == 2
    assert roundtrip("list", ["a", "b"]) == ["a", "b"]
    assert roundtrip("list", ["x", "y"], separator="; ") == ["x", "y"]
    assert roundtrip("list", []) == []


def test_list_with_item_codec():
    codec = ListCodec(DEFAULT_CODECS["int"])
    c = ctx()
    assert codec.encode([1, 0, 3], c) == "1, 0, 3"
    assert codec.decode("1, 0, 3", c) == [1, 0, 3]
    assert codec.decode(" , ", c) == []


@pytest.mark.parametrize("name", ["int", "float", "bool"])
def test_falsy_values_are_not_blanked(name):
    value = {"int": 0, "float": 0.0, "bool": False}[name]
    encoded = DEFAULT_CODECS[name].encode(value, ctx())
    assert encoded == value
    assert encoded != ""
    assert type(encoded) is type(value)


def test_serial_anchors():
    assert to_serial(datetime(1899, 12, 30, tzinfo=MSK), MSK) == 0
    assert to_serial(datetime(1900, 1, 1, tzinfo=MSK), MSK) == 2
    assert to_serial(datetime(2026, 10, 12, 12, 0, tzinfo=MSK), MSK) == 46307.5
    assert from_serial(46307.5, MSK) == datetime(2026, 10, 12, 12, 0, tzinfo=MSK)
    assert DEFAULT_CODECS["date"].encode(date(1900, 1, 1), ctx()) == 2


def test_serial_rounds_to_millisecond():
    dt = datetime(2026, 10, 12, 12, 0, 0, 400, tzinfo=MSK)  # 0.4 ms rounds down
    assert to_serial(dt, MSK) == 46307.5
    assert from_serial(46307.5 + 1e-9, MSK) == datetime(2026, 10, 12, 12, 0, tzinfo=MSK)


def test_decode_typed_numbers():
    c = ctx()
    assert DEFAULT_CODECS["date"].decode(46307, c) == date(2026, 10, 12)
    assert DEFAULT_CODECS["date"].decode(46307.5, c) == date(2026, 10, 12)  # fractional serial -> its date
    assert DEFAULT_CODECS["datetime"].decode(46307.5, c) == datetime(2026, 10, 12, 12, 0, tzinfo=MSK)
    assert DEFAULT_CODECS["str"].decode(5.0, c) == "5"
    assert DEFAULT_CODECS["str"].decode(5.5, c) == "5.5"
    assert DEFAULT_CODECS["str"].decode(True, c) == "TRUE"
    assert DEFAULT_CODECS["str"].decode(False, c) == "FALSE"
    assert DEFAULT_CODECS["int"].decode(5.0, c) == 5
    assert DEFAULT_CODECS["float"].decode(5, c) == 5.0


def test_decode_canonical_text():
    c = ctx()
    assert DEFAULT_CODECS["int"].decode("5", c) == 5
    assert DEFAULT_CODECS["float"].decode("2.5", c) == 2.5
    assert DEFAULT_CODECS["date"].decode("2026-10-12", c) == date(2026, 10, 12)
    assert DEFAULT_CODECS["datetime"].decode("2026-10-12T12:00:00", c) == datetime(2026, 10, 12, 12, 0, tzinfo=MSK)
    aware = DEFAULT_CODECS["datetime"].decode("2026-10-12T09:00:00+00:00", c)
    assert aware == datetime(2026, 10, 12, 12, 0, tzinfo=MSK)
    assert aware.utcoffset() == datetime(2026, 10, 12, tzinfo=MSK).utcoffset()


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("int", 5.5),
        ("int", "5.5"),
        ("int", True),
        ("int", "abc"),
        ("float", False),
        ("float", "abc"),
        ("date", "2026-10-12T12:00:00"),
        ("date", "not a date"),
        ("datetime", True),
        ("datetime", "not a date"),
        ("date", 1e300),
        ("datetime", 1e300),
        ("datetime", float("inf")),
    ],
)
def test_decode_errors(name, raw):
    with pytest.raises(ValueError):
        DEFAULT_CODECS[name].decode(raw, ctx())


def test_encode_errors():
    c = ctx()
    with pytest.raises(ValueError, match="timezone-aware"):
        DEFAULT_CODECS["datetime"].encode(datetime(2026, 10, 12, 12, 0), c)
    with pytest.raises(ValueError, match="datetime"):
        DEFAULT_CODECS["date"].encode(datetime(2026, 10, 12, 12, 0, tzinfo=MSK), c)


def test_bool_default_sets():
    codec, c = DEFAULT_CODECS["bool"], ctx()
    for text in ("true", " TRUE ", "1", "Yes", "y", "Да"):
        assert codec.decode(text, c) is True
    for text in ("false", "FALSE", "0", "no", "N", "Нет"):
        assert codec.decode(text, c) is False
    assert codec.decode(1, c) is True
    assert codec.decode(0.0, c) is False
    for bad in ("maybe", 2, 0.5):
        with pytest.raises(ValueError):
            codec.decode(bad, c)


def test_bool_custom_sets():
    codec = DEFAULT_CODECS["bool"]
    c = ctx(true_values=["Active"], false_values=["Gone"])
    assert codec.decode("active", c) is True
    assert codec.decode("GONE", c) is False
    with pytest.raises(ValueError):
        codec.decode("yes", c)


def test_enum_valid_values():
    codec, c = DEFAULT_CODECS["enum"], ctx(valid_values=["new", "done", 3])
    assert codec.decode("done", c) == "done"
    assert codec.decode(3, c) == 3  # numeric cell matches by str(raw), returns the valid value
    with pytest.raises(ValueError):
        codec.decode(3.0, c)  # str(3.0) == "3.0"
    assert codec.encode("new", c) == "new"
    with pytest.raises(ValueError, match="new"):
        codec.decode("bogus", c)
    with pytest.raises(ValueError, match="new"):
        codec.encode("bogus", c)


def test_enum_value_map():
    codec, c = DEFAULT_CODECS["enum"], ctx(value_map={"Open": "open", "Closed": "closed"})
    assert codec.decode("Open", c) == "open"
    assert codec.encode("closed", c) == "Closed"
    with pytest.raises(ValueError, match="Open"):
        codec.decode("open", c)
    with pytest.raises(ValueError, match="open"):
        codec.encode("Open", c)


def test_enum_without_options():
    codec = DEFAULT_CODECS["enum"]
    with pytest.raises(ValueError, match="value_map"):
        codec.decode("x", ctx())
    with pytest.raises(ValueError, match="value_map"):
        codec.encode("x", ctx())


def test_default_codecs_registry():
    assert set(DEFAULT_CODECS) == {"str", "int", "float", "bool", "date", "datetime", "enum", "list"}
    with pytest.raises(TypeError):
        DEFAULT_CODECS["x"] = DEFAULT_CODECS["str"]  # type: ignore[index]


def test_int_encode_rejects_beyond_float_precision():
    codec = DEFAULT_CODECS["int"]
    assert codec.encode(2**53, ctx()) == 2**53
    for big in (2**53 + 1, -(2**53) - 1):
        with pytest.raises(ValueError, match="2\\*\\*53"):
            codec.encode(big, ctx())
