from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from sheetsync import DEFAULT_CODECS, Ctx, NumberFormat, sheets_pattern_to_strptime

MSK = ZoneInfo("Europe/Moscow")


def make_ctx(pattern=None, type_="DATE_TIME"):
    warnings: list[str] = []
    fmt = NumberFormat(type=type_, pattern=pattern) if pattern is not None else None
    return Ctx(tz=MSK, format=fmt, warn=warnings.append), warnings


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("yyyy-mm-dd", "%Y-%m-%d"),
        ("dd.mm.yy", "%d.%m.%y"),
        ("d/m/yyyy", "%d/%m/%Y"),
        ("dd.mm.yyyy hh:mm", "%d.%m.%Y %H:%M"),  # mm after h -> minutes
        ("hh:mm:ss", "%H:%M:%S"),
        ("mm:ss", "%M:%S"),  # mm before s -> minutes
        ("mm/dd/yyyy hh:mm:ss", "%m/%d/%Y %H:%M:%S"),  # first mm month, second minutes
        ("hh:mm AM/PM", "%I:%M %p"),
        ("h:mm:ss AM/PM", "%I:%M:%S %p"),
        (r"dd\Tmm", "%dT%m"),
        ('dd"%"mm', "%d%%%m"),
        ("dd-mm-yyyy @ hh", "%d-%m-%Y @ %H"),
    ],
)
def test_pattern_translation(pattern, expected):
    assert sheets_pattern_to_strptime(pattern) == expected


@pytest.mark.parametrize(
    "pattern", ["mmm yyyy", "mmmm", "mmmmm", "ddd", "dddd", "[h]:mm", "[m]", "[s]", "hh:mm:ss.0", "hh:mm:ss.000", "yyy"]
)
def test_pattern_unsupported(pattern):
    with pytest.raises(ValueError, match="unsupported"):
        sheets_pattern_to_strptime(pattern)


def test_datetime_pattern_hit_is_silent():
    c, warnings = make_ctx("dd.mm.yyyy hh:mm")
    assert DEFAULT_CODECS["datetime"].decode("12.10.2026 14:30", c) == datetime(2026, 10, 12, 14, 30, tzinfo=MSK)
    assert warnings == []


def test_date_pattern_hit_is_silent():
    c, warnings = make_ctx("dd.mm.yyyy", "DATE")
    assert DEFAULT_CODECS["date"].decode("12.10.2026", c) == date(2026, 10, 12)
    assert warnings == []


def test_datetime_pattern_miss_falls_back_with_label():
    c, warnings = make_ctx("dd.mm.yyyy hh:mm")
    assert DEFAULT_CODECS["datetime"].decode("2026-10-12T14:30:00", c) == datetime(2026, 10, 12, 14, 30, tzinfo=MSK)
    assert warnings == ["ISO 8601"]
    c, warnings = make_ctx("dd.mm.yyyy hh:mm")
    assert DEFAULT_CODECS["datetime"].decode("12.10.2026", c) == datetime(2026, 10, 12, tzinfo=MSK)
    assert warnings == ["dd.mm.yyyy"]


def test_fallback_label_includes_time_suffix():
    c, warnings = make_ctx()
    assert DEFAULT_CODECS["datetime"].decode("12.10.2026 14:30", c) == datetime(2026, 10, 12, 14, 30, tzinfo=MSK)
    assert DEFAULT_CODECS["datetime"].decode("12.10.2026 14:30:15", c) == datetime(2026, 10, 12, 14, 30, 15, tzinfo=MSK)
    assert warnings == ["dd.mm.yyyy hh:mm", "dd.mm.yyyy hh:mm:ss"]


def test_no_pattern_iso_is_silent():
    c, warnings = make_ctx()
    assert DEFAULT_CODECS["date"].decode("2026-10-12", c) == date(2026, 10, 12)
    assert DEFAULT_CODECS["datetime"].decode("2026-10-12 14:30", c) == datetime(2026, 10, 12, 14, 30, tzinfo=MSK)
    assert warnings == []


def test_ambiguous_date_is_an_error():
    c, warnings = make_ctx()
    with pytest.raises(ValueError, match="ambiguous date.*dd/mm/yyyy and mm/dd/yyyy"):
        DEFAULT_CODECS["date"].decode("01/02/2026", c)
    assert warnings == []


def test_unambiguous_slash_dates():
    c, warnings = make_ctx()
    assert DEFAULT_CODECS["date"].decode("13/02/2026", c) == date(2026, 2, 13)  # only dd/mm is valid
    assert DEFAULT_CODECS["date"].decode("02/13/2026", c) == date(2026, 2, 13)  # only mm/dd is valid
    assert warnings == ["dd/mm/yyyy", "mm/dd/yyyy"]


def test_pattern_resolves_otherwise_ambiguous_date():
    c, warnings = make_ctx("dd/mm/yyyy", "DATE")
    assert DEFAULT_CODECS["date"].decode("01/02/2026", c) == date(2026, 2, 1)
    assert warnings == []


def test_unparsable_date_is_an_error():
    c, _ = make_ctx("dd.mm.yyyy")
    with pytest.raises(ValueError, match="cannot parse"):
        DEFAULT_CODECS["date"].decode("someday", c)


def test_unsupported_pattern_surfaces_as_error():
    c, _ = make_ctx("mmm yyyy")
    with pytest.raises(ValueError, match="unsupported"):
        DEFAULT_CODECS["date"].decode("Oct 2026", c)


def test_number_pattern_hit_is_silent():
    c, warnings = make_ctx("$#,##0.00", "CURRENCY")
    assert DEFAULT_CODECS["float"].decode("$1,234.50", c) == 1234.5
    c2, warnings2 = make_ctx("0%", "PERCENT")
    assert DEFAULT_CODECS["float"].decode("12%", c2) == pytest.approx(0.12)
    c3, warnings3 = make_ctx('0.00 "kr"', "NUMBER")
    assert DEFAULT_CODECS["float"].decode("5.25 kr", c3) == 5.25
    assert warnings == warnings2 == warnings3 == []


def test_number_no_pattern_canonical_is_silent():
    c, warnings = make_ctx()
    assert DEFAULT_CODECS["int"].decode("5", c) == 5
    assert DEFAULT_CODECS["float"].decode("2.5", c) == 2.5
    assert warnings == []


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        (None, "1 234,56", 1234.56),
        (None, "1\u00a0234,56", 1234.56),
        (None, "1\u202f234,56", 1234.56),
        (None, "1.234,56", 1234.56),
        (None, "1,234.56", 1234.56),
        (None, "€ 12,5", 12.5),
        (None, "$1,234.50", 1234.5),
        (None, "12%", 0.12),
        ("#,##0.00", "1 234,56", 1234.56),  # pattern miss -> fallback
    ],
)
def test_number_fallback_warns(pattern, text, expected):
    c, warnings = make_ctx(pattern, "NUMBER")
    assert DEFAULT_CODECS["float"].decode(text, c) == pytest.approx(expected)
    assert warnings == ["number fallback"]


@pytest.mark.parametrize("text", ["1,234", "1,234%"])
def test_number_ambiguous_comma(text):
    c, warnings = make_ctx()
    with pytest.raises(ValueError, match="ambiguous number"):
        DEFAULT_CODECS["float"].decode(text, c)
    assert warnings == []


@pytest.mark.parametrize("text", ["abc", "12 euros", "1,2,3"])
def test_number_unparsable(text):
    c, _ = make_ctx()
    with pytest.raises(ValueError, match="cannot parse"):
        DEFAULT_CODECS["float"].decode(text, c)


def test_int_fallback_and_non_integer():
    c, warnings = make_ctx()
    assert DEFAULT_CODECS["int"].decode("1 234", c) == 1234
    assert warnings == ["number fallback"]
    with pytest.raises(ValueError, match="integer"):
        DEFAULT_CODECS["int"].decode("1,5", c)
