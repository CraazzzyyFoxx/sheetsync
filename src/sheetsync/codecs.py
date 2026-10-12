import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

Cell = str | int | float | bool | None

_EPOCH = datetime(1899, 12, 30)
_MS = timedelta(milliseconds=1)
_MS_PER_DAY = 86_400_000


class NumberFormat(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["DATE", "TIME", "DATE_TIME", "NUMBER", "CURRENCY", "PERCENT", "SCIENTIFIC", "TEXT"]
    pattern: str | None = None


def _noop(_: str) -> None:
    return None


@dataclass(frozen=True)
class Ctx:
    tz: ZoneInfo
    options: Mapping[str, Any] = MappingProxyType({})
    format: NumberFormat | None = None
    warn: Callable[[str], None] = _noop  # called with the matched fallback format label


class Codec[T](Protocol):
    def decode(self, raw: Cell, ctx: Ctx) -> T: ...  # never called with an empty cell (caller filters)
    def encode(self, value: T, ctx: Ctx) -> Cell: ...  # never called with None (caller maps None -> "")


def to_serial(dt: datetime, tz: ZoneInfo) -> float:
    """Aware datetime -> Sheets serial number in `tz`, rounded to the millisecond."""
    naive = dt.astimezone(tz).replace(tzinfo=None)
    return round((naive - _EPOCH) / _MS) / _MS_PER_DAY


def from_serial(x: float, tz: ZoneInfo) -> datetime:
    """Sheets serial number -> aware datetime in `tz`, rounded to the millisecond."""
    try:
        ms = round(timedelta(days=x) / _MS)
        return (_EPOCH + ms * _MS).replace(tzinfo=tz)
    except OverflowError:
        raise ValueError(f"serial {x} is out of the supported date range") from None


_TOKEN = re.compile(r'"([^"]*)"|\\(.)|(\[[^\]]*\])|(\.0+)|(AM/PM)|(y+|m+|d+|h+|s+)|(.)', re.IGNORECASE | re.DOTALL)


def sheets_pattern_to_strptime(pattern: str) -> str:
    """Translate a Sheets date/time pattern to a strptime format; ValueError for locale-dependent/unsupported tokens."""
    parts: list[tuple[str, str]] = []  # (kind, text): kind is a date/time letter, "p" for AM/PM, "" for a literal
    for m in _TOKEN.finditer(pattern):
        quoted, escaped, bracket, frac, ampm, run, other = m.groups()
        if bracket or frac:
            raise ValueError(f"unsupported pattern token {m.group()!r} in {pattern!r}")
        if ampm:
            parts.append(("p", ""))
        elif run:
            kind = run[0].lower()
            if len(run) > 2 and kind != "y" or kind == "y" and len(run) not in (2, 4):
                raise ValueError(f"unsupported pattern token {run!r} in {pattern!r}")
            parts.append((kind, run))
        else:
            parts.append(("", next(g for g in (quoted, escaped, other) if g is not None)))
    twelve_hour = any(kind == "p" for kind, _ in parts)
    clock = [i for i, (kind, _) in enumerate(parts) if kind in ("y", "m", "d", "h", "s")]
    out: list[str] = []
    for i, (kind, text) in enumerate(parts):
        if kind == "":
            out.append(text.replace("%", "%%"))
        elif kind == "p":
            out.append("%p")
        elif kind == "y":
            out.append("%Y" if len(text) == 4 else "%y")
        elif kind == "m":
            pos = clock.index(i)
            prev_kind = parts[clock[pos - 1]][0] if pos > 0 else ""
            next_kind = parts[clock[pos + 1]][0] if pos + 1 < len(clock) else ""
            out.append("%M" if prev_kind == "h" or next_kind == "s" else "%m")
        else:
            out.append({"d": "%d", "h": "%I" if twelve_hour else "%H", "s": "%S"}[kind])
    return "".join(out)


_ISO = "ISO 8601"
_DATE_FALLBACKS = (
    ("dd.mm.yyyy", "%d.%m.%Y"),
    ("dd/mm/yyyy", "%d/%m/%Y"),
    ("mm/dd/yyyy", "%m/%d/%Y"),
    ("yyyy-mm-dd", "%Y-%m-%d"),
)
_TIME_SUFFIXES = (("", ""), (" hh:mm", " %H:%M"), (" hh:mm:ss", " %H:%M:%S"))


def _parse_datetime_text(text: str, ctx: Ctx) -> datetime:
    text = text.strip()
    pattern = ctx.format.pattern if ctx.format else None
    if pattern:
        try:
            return datetime.strptime(text, sheets_pattern_to_strptime(pattern))
        except ValueError:  # no match, or a pattern we cannot translate (e.g. month names): use the fallback list
            pass
    found: dict[datetime, str] = {}  # distinct value -> label of the first fallback that produced it
    try:
        iso = datetime.fromisoformat(text)
    except ValueError:
        pass
    else:
        if not pattern:
            return iso  # canonical text is silent only when the column has no pattern
        found[iso] = _ISO
    for label, fmt in _DATE_FALLBACKS:
        for suffix_label, suffix_fmt in _TIME_SUFFIXES:
            try:
                found.setdefault(datetime.strptime(text, fmt + suffix_fmt), label + suffix_label)
            except ValueError:
                pass
    if not found:
        raise ValueError(f"cannot parse {text!r} as a date/time")
    if len(found) > 1:
        raise ValueError(f"ambiguous date {text!r}: matches {' and '.join(found.values())}")
    ((value, label),) = found.items()
    ctx.warn(label)
    return value


_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _plain_number(s: str) -> float | None:
    return float(s) if _NUMBER.fullmatch(s) else None


def _parse_number_text(text: str, ctx: Ctx) -> float:
    text = text.strip()
    pattern = ctx.format.pattern if ctx.format else None
    if not pattern:
        try:
            return float(text)
        except ValueError:
            pass
    else:
        slots = [i for i, ch in enumerate(pattern) if ch in "0#?"]
        first, last = (slots[0], slots[-1] + 1) if slots else (0, len(pattern))
        prefix, suffix = (re.sub(r'["\\]', "", pattern[:first]), re.sub(r'["\\]', "", pattern[last:]))
        body = text.removeprefix(prefix).removesuffix(suffix).strip()
        if "," in pattern[first:last]:  # grouping separators are only dropped when the pattern has them
            body = body.replace(",", "")
        n = _plain_number(body)
        if n is not None:
            return n / 100 if "%" in pattern else n
    s = re.sub(r"[^0-9,.\-+eE%]", "", text)  # drops spaces, NBSP, narrow NBSP, currency symbols
    percent = s.endswith("%")
    s = s.removesuffix("%")
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif s.count(",") == 1:
        if len(re.match(r"\d*", s.partition(",")[2])[0]) == 3:
            raise ValueError(f"ambiguous number {text!r}: ',' may be a thousands or a decimal separator")
        s = s.replace(",", ".")
    n = _plain_number(s)
    if n is None:
        raise ValueError(f"cannot parse {text!r} as a number")
    ctx.warn("number fallback")
    return n / 100 if percent else n


def _text(raw: Cell) -> str:
    if isinstance(raw, bool):
        return "TRUE" if raw else "FALSE"
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def _number(raw: Cell, ctx: Ctx) -> int | float:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"expected a number, got {raw!r}")
    if isinstance(raw, str):
        return _parse_number_text(raw, ctx)
    return raw


def _aware(raw: Cell, ctx: Ctx) -> datetime:
    """Number -> serial in ctx.tz; text -> parsed. Always returns a datetime aware in ctx.tz."""
    if isinstance(raw, str):
        dt = _parse_datetime_text(raw, ctx)
        return dt.replace(tzinfo=ctx.tz) if dt.tzinfo is None else dt.astimezone(ctx.tz)
    return from_serial(_number(raw, ctx), ctx.tz)


class StrCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> str:
        return _text(raw)

    def encode(self, value: str, ctx: Ctx) -> Cell:
        return str(value)


class IntCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> int:
        n = _number(raw, ctx)
        if isinstance(n, float) and not n.is_integer():
            raise ValueError(f"expected an integer, got {n}")
        return int(n)

    def encode(self, value: int, ctx: Ctx) -> Cell:
        return int(value)


class FloatCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> float:
        return float(_number(raw, ctx))

    def encode(self, value: float, ctx: Ctx) -> Cell:
        return float(value)


class BoolCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int | float) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().casefold()
            if text in {v.casefold() for v in ctx.options.get("true_values", ("true", "1", "yes", "y", "да"))}:
                return True
            if text in {v.casefold() for v in ctx.options.get("false_values", ("false", "0", "no", "n", "нет"))}:
                return False
        raise ValueError(f"cannot interpret {raw!r} as a boolean")

    def encode(self, value: bool, ctx: Ctx) -> Cell:
        return bool(value)


class DateCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> date:
        dt = _aware(raw, ctx)
        if isinstance(raw, str) and dt.time() != datetime.min.time():  # a fractional serial just yields its date
            raise ValueError(f"expected a date without a time, got {raw!r}")
        return dt.date()

    def encode(self, value: date, ctx: Ctx) -> Cell:
        if isinstance(value, datetime):
            raise ValueError("date column got a datetime; pass a date")
        return (value - _EPOCH.date()).days


class DateTimeCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> datetime:
        return _aware(raw, ctx)

    def encode(self, value: datetime, ctx: Ctx) -> Cell:
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return to_serial(value, ctx.tz)


class EnumCodec:
    def decode(self, raw: Cell, ctx: Ctx) -> Any:
        text = str(raw)
        if "value_map" in ctx.options:
            value_map = ctx.options["value_map"]
            if text not in value_map:
                raise ValueError(f"{text!r} is not one of {list(value_map)}")
            return value_map[text]
        if "valid_values" in ctx.options:
            for v in ctx.options["valid_values"]:
                if str(v) == text:
                    return v
            raise ValueError(f"{text!r} is not one of {list(ctx.options['valid_values'])}")
        raise ValueError("enum codec needs a 'value_map' or 'valid_values' option")

    def encode(self, value: Any, ctx: Ctx) -> Cell:
        if "value_map" in ctx.options:
            for cell_text, v in ctx.options["value_map"].items():
                if v == value:
                    return cell_text
            raise ValueError(f"{value!r} is not one of {list(ctx.options['value_map'].values())}")
        if "valid_values" in ctx.options:
            if value not in ctx.options["valid_values"]:
                raise ValueError(f"{value!r} is not one of {list(ctx.options['valid_values'])}")
            return str(value)
        raise ValueError("enum codec needs a 'value_map' or 'valid_values' option")


class ListCodec:
    def __init__(self, item: Codec[Any] | None = None) -> None:
        self.item = item or StrCodec()

    def decode(self, raw: Cell, ctx: Ctx) -> list[Any]:
        sep = ctx.options.get("separator", ", ")
        parts = (p.strip() for p in str(raw).split(sep))
        return [self.item.decode(p, ctx) for p in parts if p]

    def encode(self, value: list[Any], ctx: Ctx) -> Cell:
        sep = ctx.options.get("separator", ", ")
        return sep.join(str(self.item.encode(v, ctx)) for v in value)


DEFAULT_CODECS: Mapping[str, Codec[Any]] = MappingProxyType(
    {
        "str": StrCodec(),
        "int": IntCodec(),
        "float": FloatCodec(),
        "bool": BoolCodec(),
        "date": DateCodec(),
        "datetime": DateTimeCodec(),
        "enum": EnumCodec(),
        "list": ListCodec(),
    }
)
