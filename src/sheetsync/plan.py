import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sheetsync.codecs import DEFAULT_CODECS, Cell, Codec
from sheetsync.errors import RowError, RowWarning
from sheetsync.schema import Owner, TableSchema, encode_record, get_path


@dataclass(frozen=True)
class RowState:
    key: str
    row_number: int | None
    app_hash: str


@dataclass(frozen=True)
class Row:
    key: str
    row_number: int
    values: dict[str, Any]
    raw: tuple[Cell, ...]
    tagged: bool


@dataclass(frozen=True)
class Snapshot:
    rows: dict[str, Row]
    errors: list[RowError]
    warnings: list[RowWarning]
    orphans: list[int]
    duplicates: dict[str, list[int]]
    tz: ZoneInfo
    indexes: dict[str, int]
    last_data_row: int
    grid_rows: int


@dataclass(frozen=True)
class Plan:
    creates: dict[str, dict]
    updates: dict[str, dict]
    deletes: dict[str, RowState]
    pulls: dict[str, dict[str, Any]]
    drift: dict[str, dict[str, tuple[Any, Any]]]
    missing_in_sheet: list[str]
    orphans: list[int]
    snapshot: Snapshot


def _canonical(cell: Cell) -> Cell:
    if cell is None:
        return ""
    if isinstance(cell, float):
        return int(cell) if cell.is_integer() else round(cell, 9)
    return cell  # bool is checked implicitly: it is not a float, and stays a bool


def app_cells(schema: TableSchema, indexes: Mapping[str, int], cells: Sequence[Cell]) -> list[Cell]:
    """Cells of the app-owned columns, in schema column order (None where the row is too short)."""
    return [
        cells[indexes[c.field]] if indexes[c.field] < len(cells) else None
        for c in schema.columns
        if c.owner == Owner.APP and c.field in indexes
    ]


def fill_skipped(encoded: Sequence[Cell], current: Sequence[Cell]) -> list[Cell]:
    """A codec that encodes to None leaves the sheet cell as is, so the `current` cell stands in for it."""
    return [c if e is None else e for e, c in zip(encoded, current, strict=True)]


def app_hash(cells: Sequence[Cell]) -> str:
    canonical = [_canonical(c) for c in cells]
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def plan_sync(
    schema: TableSchema,
    snapshot: Snapshot,
    records: Mapping[str, Mapping],
    states: Mapping[str, RowState],
    *,
    codecs: Mapping[str, Codec[Any]] = DEFAULT_CODECS,
) -> Plan:
    dupes = snapshot.duplicates
    indexes = snapshot.indexes
    app_fields = [c.field for c in schema.columns if c.owner == Owner.APP and c.field in indexes]
    creates: dict[str, dict] = {}
    updates: dict[str, dict] = {}
    deletes: dict[str, RowState] = {}
    drift: dict[str, dict[str, tuple[Any, Any]]] = {}
    missing: list[str] = []

    for key, record in records.items():
        if key in dupes:
            continue
        row = snapshot.rows.get(key)
        if row is None:
            if key in states:
                missing.append(key)
            else:
                creates[key] = dict(record)
            continue
        sheet = app_cells(schema, indexes, row.raw)
        encoded = fill_skipped(
            app_cells(schema, indexes, encode_record(schema, indexes, record, codecs, snapshot.tz)), sheet
        )
        if app_hash(encoded) != app_hash(sheet):
            updates[key] = dict(record)
        state = states.get(key)
        if state is not None and app_hash(sheet) != state.app_hash:
            diff = {
                f: (row.values[f], get_path(record, f))
                for f, e, s in zip(app_fields, encoded, sheet, strict=True)
                if app_hash([e]) != app_hash([s])  # same canonicalisation as the hash: True is not 1
            }
            if diff:
                drift[key] = diff

    if schema.on_app_delete != "ignore":
        deletes = {k: s for k, s in states.items() if k not in records and k not in dupes}

    sheet_fields = [c.field for c in schema.columns if c.owner == Owner.SHEET and c.field in indexes]
    pulls = (
        {k: {f: r.values[f] for f in sheet_fields} for k, r in snapshot.rows.items() if k not in dupes}
        if sheet_fields
        else {}
    )
    return Plan(creates, updates, deletes, pulls, drift, missing, list(snapshot.orphans), snapshot)
