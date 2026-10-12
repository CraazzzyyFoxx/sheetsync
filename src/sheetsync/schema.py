import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Union, get_args, get_origin
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError, model_validator

from sheetsync.codecs import Cell, Codec, Ctx, NumberFormat
from sheetsync.errors import RowError, RowWarning, SchemaError


class Owner(StrEnum):
    APP = "app"
    SHEET = "sheet"
    SKIP = "skip"


class Column(BaseModel):
    field: str  # dotted path into the record dict, e.g. "price.dollar"
    header: str | None = None  # resolved against header_row (preferred)
    index: int | None = None  # 0-based fallback / pin; at least one of header/index required
    codec: str = "str"  # name in the codec registry
    owner: Owner = Owner.APP
    required: bool = False  # decode: empty cell -> RowError
    options: dict[str, Any] = {}  # codec params (valid_values, value_map, separator, true_values, ...)
    format: NumberFormat | None = None  # display format override

    @model_validator(mode="after")
    def _header_or_index(self) -> "Column":
        if self.header is None and self.index is None:
            raise ValueError(f"column {self.field!r} needs a header or an index")
        return self


class TableSchema(BaseModel):
    columns: list[Column]
    header_row: int = 1  # 1-based
    data_start_row: int | None = None  # default header_row + 1
    identity: Literal["metadata", "key_column", "metadata+key_column"] = "metadata"
    key_field: str  # record field holding the key (always required)
    metadata_prefix: str = "k:"  # metadataKey = prefix + key
    on_app_delete: Literal["delete_row", "clear", "ignore"] = "delete_row"
    formats: dict[str, NumberFormat] = {}  # codec name -> default display format

    @model_validator(mode="after")
    def _check_columns(self) -> "TableSchema":
        fields = [c.field for c in self.columns]
        if dupes := sorted({f for f in fields if fields.count(f) > 1}):
            raise ValueError(f"duplicate column fields: {dupes}")
        if "key_column" in self.identity and self.key_field not in fields:
            raise ValueError(f"key_field {self.key_field!r} does not name a column")
        return self

    @property
    def first_data_row(self) -> int:
        return self.data_start_row or self.header_row + 1

    def format_for(self, column: Column) -> NumberFormat | None:
        return column.format or self.formats.get(column.codec)

    def validate_codecs(self, codecs: Mapping[str, Codec[Any]]) -> None:
        for c in self.columns:
            if c.codec not in codecs:
                raise SchemaError(f"column {c.field!r}: unknown codec {c.codec!r}")

    @classmethod
    def from_json(cls, data: Mapping[str, Any], codecs: Mapping[str, Codec[Any]]) -> "TableSchema":
        schema = cls._validate(data)
        schema.validate_codecs(codecs)
        return schema

    @classmethod
    def from_model(cls, model: type[BaseModel], **table_opts: Any) -> "TableSchema":
        return cls._validate({"columns": _model_columns(model, ""), **table_opts})

    @classmethod
    def _validate(cls, data: Mapping[str, Any]) -> "TableSchema":
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise SchemaError(str(e)) from e


@dataclass(frozen=True)
class Col:
    """`Annotated[T, Col(...)]` marker: turns a model field into a schema column."""

    header: str | None = None
    index: int | None = None
    codec: str | None = None  # None -> inferred from the annotation
    owner: Owner = Owner.APP
    required: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    format: NumberFormat | None = None


_INFERRED = {str: "str", int: "int", float: "float", bool: "bool", date: "date", datetime: "datetime"}


def _unwrap_optional(ann: Any) -> Any:
    if get_origin(ann) in (Union, types.UnionType):
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _model_columns(model: type[BaseModel], prefix: str) -> list[dict[str, Any]]:
    cols: list[dict[str, Any]] = []
    for name, info in model.model_fields.items():
        ann = _unwrap_optional(info.annotation)
        path = prefix + name
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            cols += _model_columns(ann, path + ".")
        elif col := next((m for m in info.metadata if isinstance(m, Col)), None):
            cols.append(
                {
                    "field": path,
                    "header": col.header,
                    "index": col.index,
                    "codec": col.codec or _INFERRED.get(ann, "str"),
                    "owner": col.owner,
                    "required": col.required,
                    "options": col.options,
                    "format": col.format,
                }
            )
    return cols


def _norm(cell: Cell) -> str:
    return "" if cell is None else str(cell).strip().casefold()


def resolve_columns(schema: TableSchema, header_cells: Sequence[Cell]) -> dict[str, int]:
    """Map each column's field to its 0-based sheet index (SPEC §4)."""
    headers = [_norm(c) for c in header_cells]
    result: dict[str, int] = {}
    for c in schema.columns:
        if c.header is None:
            assert c.index is not None  # guaranteed by Column validation
            result[c.field] = c.index
            continue
        want = _norm(c.header)
        if c.index is not None:
            found = c.index if c.index < len(headers) and headers[c.index] == want else None
        else:
            hits = [i for i, h in enumerate(headers) if h == want]
            if len(hits) > 1:
                raise SchemaError(
                    f"column {c.field!r}: header {c.header!r} appears {len(hits)} times; pin it with index"
                )
            found = hits[0] if hits else None
        if found is not None:
            result[c.field] = found
        elif c.owner != Owner.SKIP:
            where = f" at index {c.index}" if c.index is not None else ""
            raise SchemaError(f"column {c.field!r}: header {c.header!r} not found{where}")
    return result


def get_path(record: Mapping[str, Any], path: str) -> Any:
    cur: Any = record
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _is_empty(raw: Cell) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def decode_row(
    schema: TableSchema,
    indexes: Mapping[str, int],
    cells: Sequence[Cell],
    row_number: int,
    codecs: Mapping[str, Codec[Any]],
    tz: ZoneInfo,
) -> tuple[dict[str, Any], list[RowError], list[RowWarning]]:
    """Decode one row into a flat dotted-field dict. Failed fields are left out of the dict."""
    values: dict[str, Any] = {}
    errors: list[RowError] = []
    warnings: list[RowWarning] = []
    for c in schema.columns:
        if c.owner == Owner.SKIP or c.field not in indexes:
            continue
        i = indexes[c.field]
        raw = cells[i] if i < len(cells) else None
        if _is_empty(raw):
            values[c.field] = None
            if c.required:
                errors.append(RowError(row_number, c.field, raw, "required"))
            continue

        def warn(label: str, f: str = c.field, r: Cell = raw) -> None:
            warnings.append(RowWarning(row_number, f, r, label))

        ctx = Ctx(tz, MappingProxyType(c.options), schema.format_for(c), warn)
        try:
            values[c.field] = codecs[c.codec].decode(raw, ctx)
        except (ValueError, TypeError) as e:
            errors.append(RowError(row_number, c.field, raw, str(e)))
    return values, errors, warnings


def encode_record(
    schema: TableSchema,
    indexes: Mapping[str, int],
    record: Mapping[str, Any],
    codecs: Mapping[str, Codec[Any]],
    tz: ZoneInfo,
) -> list[Cell]:
    """Encode the app-owned columns into a row; other positions stay None (= leave the cell alone)."""
    row: list[Cell] = [None] * (max(indexes.values(), default=-1) + 1)
    for c in schema.columns:
        if c.owner != Owner.APP:
            continue
        try:
            value = get_path(record, c.field)
        except KeyError:
            raise SchemaError(f"record has no field {c.field!r}") from None
        ctx = Ctx(tz, MappingProxyType(c.options), schema.format_for(c))
        row[indexes[c.field]] = "" if value is None else codecs[c.codec].encode(value, ctx)
    return row
