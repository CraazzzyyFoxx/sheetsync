import csv
import io
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from sheetsync.codecs import DEFAULT_CODECS, Cell, Codec, sheets_pattern_to_strptime
from sheetsync.errors import RowDecodeError, SchemaError, SourceError
from sheetsync.plan import Row, Snapshot
from sheetsync.schema import Owner, TableSchema, _is_empty, decode_row, resolve_columns

if TYPE_CHECKING:
    from sheetsync.client import SheetsClient

META_FIELDS = "properties(timeZone,locale),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"


def build_snapshot(
    schema: TableSchema,
    codecs: Mapping[str, Codec[Any]],
    tz: ZoneInfo,
    rows: Sequence[Sequence[Cell]],
    tags: Mapping[int, str],
    grid_rows: int,
    *,
    strict: bool = False,
) -> Snapshot:
    """Decode a whole tab (`rows[0]` is sheet row 1; `tags` maps row number -> key) into a Snapshot."""
    header = rows[schema.header_row - 1] if schema.header_row <= len(rows) else []
    indexes = resolve_columns(schema, header)
    data_indexes = [indexes[c.field] for c in schema.columns if c.owner != Owner.SKIP and c.field in indexes]
    keyed_by_column = "key_column" in schema.identity
    first = schema.first_data_row
    last_data_row = first - 1
    errors, warnings, orphans = [], [], []
    by_key: dict[str, list[Row]] = {}

    for row_number in range(first, len(rows) + 1):
        cells = rows[row_number - 1]
        if all(_is_empty(cells[i]) for i in data_indexes if i < len(cells)):
            continue
        last_data_row = row_number
        values, row_errors, row_warnings = decode_row(schema, indexes, cells, row_number, codecs, tz)
        if strict and row_errors:
            raise RowDecodeError(row_errors[0])
        errors += row_errors
        warnings += row_warnings
        key, tagged = tags.get(row_number), True
        if key is None:
            key, tagged = None, False
            decoded = values.get(schema.key_field) if keyed_by_column else None
            if decoded is not None:  # an empty or undecodable key cell decodes to None
                key = str(decoded)
        if key:
            by_key.setdefault(key, []).append(Row(key, row_number, values, tuple(cells), tagged))
        else:
            orphans.append(row_number)

    return Snapshot(
        rows={k: v[0] for k, v in by_key.items() if len(v) == 1},
        errors=errors,
        warnings=warnings,
        orphans=orphans,
        duplicates={k: [r.row_number for r in v] for k, v in by_key.items() if len(v) > 1},
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
        quoted = "'" + self.title.replace("'", "''") + "'"
        values = await self.client.values_batch_get(self.spreadsheet_id, [quoted])
        rows = values["valueRanges"][0].get("values", [])
        tags: dict[int, str] = {}
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
                    tags[row_number] = meta["metadataKey"][len(prefix) :]
        return build_snapshot(self.schema, self.codecs, self.tz, rows, tags, self.grid_rows, strict=strict)


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
