import csv
import io
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from sheetsync.codecs import DEFAULT_CODECS, Cell, Codec, NumberFormat, sheets_pattern_to_strptime
from sheetsync.errors import RowDecodeError, SchemaError, SheetsyncError, SourceError
from sheetsync.lock import AsyncLock, NullLock
from sheetsync.plan import Plan, Row, RowState, Snapshot, app_cells, app_hash, fill_skipped, plan_sync
from sheetsync.schema import Column, Owner, TableSchema, _is_empty, decode_row, encode_record, resolve_columns

if TYPE_CHECKING:
    from sheetsync.client import SheetsClient

META_FIELDS = "properties(timeZone,locale),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _number_format(fmt: NumberFormat) -> dict[str, str]:
    return {"type": fmt.type, **({"pattern": fmt.pattern} if fmt.pattern else {})}


def _cell_data(value: Cell, fmt: NumberFormat | None) -> dict[str, Any]:
    cell: dict[str, Any] = {}
    if isinstance(value, bool):
        cell["userEnteredValue"] = {"boolValue": value}
    elif isinstance(value, int | float):
        cell["userEnteredValue"] = {"numberValue": value}
    elif value:  # "" / None: no userEnteredValue, which clears the cell
        cell["userEnteredValue"] = {"stringValue": value}
    if fmt:
        cell["userEnteredFormat"] = {"numberFormat": _number_format(fmt)}
    return cell


@dataclass(frozen=True)
class ApplyResult:
    states: dict[str, RowState]  # every row left in the sheet (incl. creates), with its final row number
    removed: list[str]  # keys of plan.deletes


def build_snapshot(
    schema: TableSchema,
    codecs: Mapping[str, Codec[Any]],
    tz: ZoneInfo,
    rows: Sequence[Sequence[Cell]],
    tags: Mapping[int, list[str]],
    grid_rows: int,
    *,
    strict: bool = False,
) -> Snapshot:
    """Decode a whole tab (`rows[0]` is sheet row 1; `tags` maps row number -> its tagged keys) into a Snapshot."""
    header = rows[schema.header_row - 1] if schema.header_row <= len(rows) else []
    indexes = resolve_columns(schema, header)
    data_indexes = [indexes[c.field] for c in schema.columns if c.owner != Owner.SKIP and c.field in indexes]
    keyed_by_column = "key_column" in schema.identity
    first = schema.first_data_row
    last_data_row = first - 1
    errors, warnings, orphans = [], [], []
    by_key: dict[str, list[Row]] = {}
    conflicted: set[str] = set()  # keys on a row that carries several of our tags: never safe to address

    # a tagged row is a data row even when its synced cells are empty or it is past the end of `values`
    for row_number in range(first, max([len(rows), *tags]) + 1):
        cells = rows[row_number - 1] if row_number <= len(rows) else []
        row_tags = tags.get(row_number, [])
        if not row_tags and all(_is_empty(cells[i]) for i in data_indexes if i < len(cells)):
            continue
        last_data_row = row_number
        values, row_errors, row_warnings = decode_row(schema, indexes, cells, row_number, codecs, tz)
        if strict and row_errors:
            raise RowDecodeError(row_errors[0])
        errors += row_errors
        warnings += row_warnings
        if len(row_tags) > 1:
            conflicted.update(row_tags)
        keys, tagged = row_tags, bool(row_tags)
        if not keys:
            decoded = values.get(schema.key_field) if keyed_by_column else None
            if decoded is not None and (key := str(decoded)):  # an empty or undecodable key cell decodes to None
                keys = [key]
        for key in keys:
            by_key.setdefault(key, []).append(Row(key, row_number, values, tuple(cells), tagged))
        if not keys:
            orphans.append(row_number)

    return Snapshot(
        rows={k: v[0] for k, v in by_key.items() if len(v) == 1 and k not in conflicted},
        errors=errors,
        warnings=warnings,
        orphans=orphans,
        duplicates={k: [r.row_number for r in v] for k, v in by_key.items() if len(v) > 1 or k in conflicted},
        tz=tz,
        indexes=indexes,
        last_data_row=last_data_row,
        grid_rows=grid_rows,
    )


class Table:
    def __init__(
        self,
        client: "SheetsClient",
        spreadsheet_id: str,
        gid: int,
        schema: TableSchema,
        codecs: Mapping[str, Codec[Any]] = DEFAULT_CODECS,
    ) -> None:
        schema.validate_codecs(codecs)
        self.client = client
        self.spreadsheet_id = spreadsheet_id
        self.gid = gid
        self.schema = schema
        self.codecs = codecs
        self.title: str | None = None  # tab title, tz and grid size: latest values from `fetch_meta`
        self.tz: ZoneInfo | None = None
        self.grid_rows = 0

    async def fetch_meta(self) -> None:
        """Refresh title, tz and grid size (tabs get renamed, rows get added); called by every `read()`."""
        meta = await self.client.get_spreadsheet(self.spreadsheet_id, META_FIELDS)
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            if props["sheetId"] == self.gid:
                self.tz = ZoneInfo(meta["properties"]["timeZone"])
                self.grid_rows = props["gridProperties"]["rowCount"]
                self.title = props["title"]
                return
        raise SchemaError(f"spreadsheet {self.spreadsheet_id!r} has no tab with gid {self.gid}")

    async def read(self, *, strict: bool = False) -> Snapshot:
        await self.fetch_meta()
        assert self.title is not None and self.tz is not None
        quoted = _quote(self.title)
        values = await self.client.values_batch_get(self.spreadsheet_id, [quoted])
        rows = values["valueRanges"][0].get("values", [])
        tags: dict[int, list[str]] = {}
        if "metadata" in self.schema.identity:
            lookup = {"locationType": "ROW", "metadataLocation": {"sheetId": self.gid}}
            found = await self.client.search_developer_metadata(
                self.spreadsheet_id, [{"developerMetadataLookup": lookup}]
            )
            prefix = self.schema.metadata_prefix
            for match in found.get("matchedDeveloperMetadata", []):
                meta = match["developerMetadata"]
                if meta["metadataKey"].startswith(prefix):
                    row_number = meta["location"]["dimensionRange"]["startIndex"] + 1
                    tags.setdefault(row_number, []).append(meta["metadataKey"][len(prefix) :])
        return build_snapshot(self.schema, self.codecs, self.tz, rows, tags, self.grid_rows, strict=strict)

    async def apply(self, plan: Plan, *, repair_formats: bool = True) -> ApplyResult:
        """Write a plan. The caller holds the lock across `read -> plan_sync -> apply`; the snapshot must be fresh."""
        snap, schema, indexes, gid = plan.snapshot, self.schema, plan.snapshot.indexes, self.gid
        assert self.title is not None, "apply() needs a preceding read()"
        metadata = "metadata" in schema.identity
        prefix = schema.metadata_prefix
        app_cols = [(indexes[c.field], c) for c in schema.columns if c.owner == Owner.APP and c.field in indexes]
        app_cols.sort(key=lambda p: p[0])

        def fmt(col: Column) -> NumberFormat | None:
            return schema.format_for(col)

        def encode(record: Mapping) -> list[Cell]:
            return encode_record(schema, indexes, record, self.codecs, snap.tz)

        def lookup(key: str) -> dict[str, Any]:
            return {
                "developerMetadataLookup": {
                    "metadataKey": prefix + key,
                    "locationType": "ROW",
                    "metadataLocation": {"sheetId": gid},
                }
            }

        def runs(split_by_format: bool) -> list[list[tuple[int, Column]]]:
            """Contiguous app columns; optionally split where "has a resolved format" changes."""
            keyed = groupby(
                enumerate(app_cols), lambda p: (p[1][0] - p[0], split_by_format and fmt(p[1][1]) is not None)
            )
            return [[col for _, col in run] for _, run in keyed]

        def update_cells(row_index: int, run: list[tuple[int, Column]], rows: list[dict], fields: str) -> dict:
            start = {"sheetId": gid, "rowIndex": row_index, "columnIndex": run[0][0]}
            return {"updateCells": {"rows": rows, "start": start, "fields": fields}}

        def row_range(row_number: int) -> dict[str, Any]:
            return {"sheetId": gid, "dimension": "ROWS", "startIndex": row_number - 1, "endIndex": row_number}

        # 1. value writes: updates keep whatever else is in the row (None cells are skipped by the API)
        written: dict[str, list[Cell]] = {}
        by_filter: list[dict] = []
        by_range: list[dict] = []
        for key, record in plan.updates.items():
            row = snap.rows[key]
            written[key] = cells = encode(record)
            if metadata and row.tagged:
                by_filter.append({"dataFilter": lookup(key), "majorDimension": "ROWS", "values": [cells]})
            else:
                by_range.append({"range": f"{_quote(self.title)}!A{row.row_number}", "values": [cells]})
        if by_filter:
            await self.client.values_batch_update_by_data_filter(self.spreadsheet_id, by_filter)
        if by_range:
            await self.client.values_batch_update(self.spreadsheet_id, by_range)

        # 2. structural changes, one batchUpdate
        requests: list[dict] = []
        created = {key: encode(record) for key, record in plan.creates.items()}
        first_new = snap.last_data_row + 1
        if created and (shortfall := snap.last_data_row + len(created) - snap.grid_rows) > 0:
            requests.append({"appendDimension": {"sheetId": gid, "dimension": "ROWS", "length": shortfall}})
        for run in runs(split_by_format=True) if created else []:
            formatted = fmt(run[0][1]) is not None
            rows = [{"values": [_cell_data(cells[i], fmt(col)) for i, col in run]} for cells in created.values()]
            fields = "userEnteredValue,userEnteredFormat.numberFormat" if formatted else "userEnteredValue"
            requests.append(update_cells(first_new - 1, run, rows, fields))

        gone = {key: snap.rows[key].row_number for key in plan.deletes if key in snap.rows}
        if metadata:
            tag_rows = {key: first_new + i for i, key in enumerate(created)}
            # metadata+key_column recovery: rows found via the key column get tagged
            tag_rows |= {k: r.row_number for k, r in snap.rows.items() if not r.tagged and k not in gone}
            for key, row_number in tag_rows.items():
                location = {"dimensionRange": row_range(row_number)}
                tag = {"metadataKey": prefix + key, "location": location, "visibility": "DOCUMENT"}
                requests.append({"createDeveloperMetadata": {"developerMetadata": tag}})

        last_row = snap.last_data_row + len(created)
        if repair_formats and last_row >= schema.first_data_row:
            for idx, col in app_cols:
                if (f := fmt(col)) is not None:
                    grid_range = {
                        "sheetId": gid,
                        "startRowIndex": schema.first_data_row - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": idx,
                        "endColumnIndex": idx + 1,
                    }
                    cell = {"userEnteredFormat": {"numberFormat": _number_format(f)}}
                    requests.append(
                        {"repeatCell": {"range": grid_range, "cell": cell, "fields": "userEnteredFormat.numberFormat"}}
                    )

        deleted_rows: list[int] = []
        if schema.on_app_delete == "clear":
            for key, row_number in gone.items():
                for run in runs(split_by_format=False):
                    blank = [{"values": [{} for _ in run]}]
                    requests.append(update_cells(row_number - 1, run, blank, "userEnteredValue"))
                if metadata and snap.rows[key].tagged:
                    requests.append({"deleteDeveloperMetadata": {"dataFilter": lookup(key)}})
        else:
            deleted_rows = sorted(gone.values())
            requests += [{"deleteDimension": {"range": row_range(r)}} for r in reversed(deleted_rows)]
        if requests:
            await self.client.batch_update(self.spreadsheet_id, requests)

        def final_row(row_number: int) -> int:  # minus the deleted rows above it
            return row_number - bisect_left(deleted_rows, row_number)

        def after_write(row: Row) -> str:
            current = app_cells(schema, indexes, row.raw)
            if row.key not in written:
                return app_hash(current)
            return app_hash(fill_skipped(app_cells(schema, indexes, written[row.key]), current))

        states = {
            key: RowState(key, final_row(row.row_number), after_write(row))
            for key, row in snap.rows.items()
            if key not in gone
        }
        for i, (key, cells) in enumerate(created.items()):
            states[key] = RowState(key, final_row(first_new + i), app_hash(app_cells(schema, indexes, cells)))
        return ApplyResult(states, list(plan.deletes))

    async def push(self, key: str, record: Mapping, *, lock: AsyncLock | None = None) -> RowState:
        async with NullLock() if lock is None else lock:
            snap = await self.read()  # ponytail: full-tab read per push, cache row lookups if push rate grows
            if key in snap.duplicates:
                raise SheetsyncError(f"key {key!r} is duplicated in the sheet (rows {snap.duplicates[key]})")
            plan = plan_sync(self.schema, snap, {key: record}, {}, codecs=self.codecs)
            return (await self.apply(plan, repair_formats=False)).states[key]

    async def delete(self, key: str, state: RowState, *, lock: AsyncLock | None = None) -> None:
        async with NullLock() if lock is None else lock:
            snap = await self.read()
            plan = plan_sync(self.schema, snap, {}, {key: state}, codecs=self.codecs)
            await self.apply(plan, repair_formats=False)


class PublicCsvTable:
    def __init__(
        self,
        url: str,
        schema: TableSchema,
        *,
        tz: ZoneInfo,
        codecs: Mapping[str, Codec[Any]] = DEFAULT_CODECS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if schema.identity != "key_column":
            raise SchemaError("PublicCsvTable requires identity='key_column'")
        schema.validate_codecs(codecs)
        for c in schema.columns:
            fmt = schema.format_for(c)
            if c.owner != Owner.SKIP and c.codec in ("date", "datetime") and fmt and fmt.pattern:
                try:
                    sheets_pattern_to_strptime(fmt.pattern)
                except ValueError as e:
                    raise SchemaError(f"column {c.field!r}: {e}") from e
        if not (sheet_id := re.search(r"/spreadsheets/d/([^/?#]+)", url)):
            raise SourceError(f"not a Google Sheets URL: {url!r}")
        gid = re.search(r"[?&#]gid=(\d+)", url)
        self.export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id[1]}/export?format=csv" + (
            f"&gid={gid[1]}" if gid else ""
        )
        self.schema = schema
        self.tz = tz
        self.codecs = codecs
        self._transport = transport

    async def read(self, *, strict: bool = False) -> Snapshot:
        async with httpx.AsyncClient(transport=self._transport, follow_redirects=True, timeout=20) as http:
            response = await http.get(self.export_url)
        content_type = response.headers.get("content-type", "")
        if not response.is_success or not content_type.startswith("text/csv"):
            raise SourceError(f"CSV export failed: HTTP {response.status_code}, content-type {content_type!r}")
        rows: list[list[Cell]] = list(csv.reader(io.StringIO(response.text.removeprefix("\ufeff"), newline="")))
        return build_snapshot(self.schema, self.codecs, self.tz, rows, {}, len(rows), strict=strict)
