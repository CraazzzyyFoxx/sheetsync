# sheetsync — specification

Async Python library for bidirectional Google Sheets ↔ application sync.
Status: draft, awaiting confirmation. Decisions below were settled in a design interview (Q1–Q30);
items marked **[default]** were chosen while writing the spec and are open to veto.

## 1. Scope

| In | Out |
|---|---|
| Transport (auth, retry, batching, addressing) | Persistence (DB models, migrations) |
| Schema + codecs (typed row ↔ record, round-trip) | Scheduling (cron, queues) |
| Pure sync planner (diff → plan) + plan executor | Distributed locking implementation |
| Read-only public CSV source | Cell styling other than number formats (colour, font, borders) |
| Number/date display formats per column (§5.1) | Spreadsheet-level `locale` / `timeZone` changes |

- Separate repo `E:/projects/sheetsync`, package/import name `sheetsync`, `requires-python >=3.12`.
- Tooling: uv, pytest, ruff. Distributed by git tag (`v0.1.0`).
- Runtime deps: `httpx`, `google-auth`, `tenacity`, `pydantic>=2` **[default]** (schema validation + `from_model`).
- First consumer: dudeduck (migration in §10). anak-tournaments migrates later, out of this scope.

## 2. Sync model: per-column ownership

Each column has exactly one `owner`:

| owner | read | written | hashed | meaning |
|---|---|---|---|---|
| `app` | yes (drift check) | yes | yes | DB is the source of truth; pushed to the sheet |
| `sheet` | yes | never | no | people/formulas own it; values returned to app as `pulls` |
| `skip` | no | no | no | documented only (header resolves, not treated as unknown); not synced |

No conflict resolution: the owner always wins. If someone edits an `app` cell by hand, the next write overwrites it
and the change is reported as `drift`.

## 3. Package layout [default]

```
src/sheetsync/
  __init__.py   # public re-exports
  errors.py     # SheetsyncError, SchemaError, ApiError(status, body), SourceError, LockTimeout
  schema.py     # Owner, Column, TableSchema (from_json / from_model), header resolution
  codecs.py     # Codec protocol, Ctx, built-in codecs, DEFAULT_CODECS
  plan.py       # RowState, Row, Snapshot, Plan, plan_sync (pure), hashing
  client.py     # SheetsClient: auth, retried HTTP, low-level Sheets v4 calls
  table.py      # Table (read/apply/push/delete), PublicCsvTable (read only)
  lock.py       # AsyncLock protocol, NullLock, AsyncioLock
```

## 4. Schema

```python
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
    required: bool = False  # decode: empty cell → RowError
    options: dict = {}  # codec params (valid_values, value_map, separator, true_values, ...)
    format: NumberFormat | None = None  # display format override (§5.1)


class TableSchema(BaseModel):
    columns: list[Column]
    header_row: int = 1  # 1-based
    data_start_row: int | None = None  # default header_row + 1
    identity: Literal["metadata", "key_column", "metadata+key_column"] = "metadata"
    key_field: str  # record field holding the key (always required)
    metadata_prefix: str = "k:"  # metadataKey = prefix + key  [default]
    on_app_delete: Literal["delete_row", "clear", "ignore"] = "delete_row"
    formats: dict[str, NumberFormat] = {}  # codec name → default display format (§5.1)

    @classmethod
    def from_json(cls, data: dict, codecs: Mapping[str, Codec]) -> "TableSchema": ...
    @classmethod
    def from_model(cls, model: type[BaseModel], **table_opts) -> "TableSchema":
        ...
        # fields annotated Annotated[T, Col(header=..., owner=..., codec=...)]
```

Validation (raises `SchemaError`; nothing is written before this passes):
- unknown codec name; duplicate `field`; neither `header` nor `index`;
- `identity` includes `key_column` → `key_field` must map to a column;
- header resolution on every read: header text is stripped and case-folded **[default]**. A missing header on an
  `app`/`sheet` column is an error. A missing header on a `skip` column is tolerated. A duplicate header is an error
  unless `index` pins the column.
- Resolution order: if both `header` and `index` are set, the header must be found at that index; otherwise it is an error
  (this catches inserted columns).

## 5. Codecs

```python
Cell = str | float | bool | None


@dataclass(frozen=True)
class Ctx:
    tz: ZoneInfo  # spreadsheet timezone (spreadsheets.get → properties.timeZone; CSV: constructor arg)
    options: Mapping[str, Any]
    format: NumberFormat | None  # resolved display format of the column (§5.1)
    warn: Callable[[str], None]  # codec reports a fallback parse → RowWarning (§5.1)


class Codec(Protocol[T]):
    def decode(self, raw: Cell, ctx: Ctx) -> T: ...  # raw may be a typed API value OR CSV text
    def encode(self, value: T, ctx: Ctx) -> Cell: ...  # None → cell skipped, "" → cell cleared
```

- The registry is explicit: `DEFAULT_CODECS | {"battle_tag": BattleTag()}`. There is no global mutable registry.
- Built-ins: `str`, `int`, `float` (decimal comma accepted), `bool` (`true_values`), `date`, `datetime`, `enum`
  (`valid_values` or `value_map`, bidirectional), `list` (`separator`, item codec).
- Round-trip invariant: `decode(encode(v)) == v` for every built-in on the API path. Tests enforce it.
- `0`, `0.0`, `False` encode as themselves (fixes dudeduck's falsy-to-empty bug).
- `datetime`: the wire value is a serial number (epoch 1899-12-30) in `ctx.tz`. Decode returns an aware datetime;
  encode requires an aware datetime (naive input → `ValueError`). Text input is parsed per §5.1. Wire precision is
  milliseconds (both directions round to the millisecond).
- `int` encodes only `abs(value) <= 2**53` (the largest range a float cell holds exactly); larger → `ValueError`
  (use a `str` column for large identifiers).
- Empty cell (`None` / `""` / whitespace) → `None` before the codec runs; `required` columns produce a `RowError`.

### 5.1 Display formats

Google Sheets formats per cell, not per viewer: everyone who opens the file sees the same text. "Regional" means
one format per file or per column. Data and display stay separate: values are written RAW (serial numbers, plain
numbers), and how they look is controlled only by `userEnteredFormat.numberFormat`.

```python
class NumberFormat(BaseModel):
    type: Literal["DATE", "TIME", "DATE_TIME", "NUMBER", "CURRENCY", "PERCENT", "SCIENTIFIC", "TEXT"]
    pattern: str | None = None  # Sheets pattern syntax; None → spreadsheet locale default for `type`
```

```json
{
  "formats": {
    "datetime": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm"},
    "date":     {"type": "DATE", "pattern": "dd.mm.yyyy"},
    "float":    {"type": "NUMBER", "pattern": "#,##0.00"}
  },
  "columns": [
    {"field": "price.dollar", "header": "Price $", "codec": "float",
     "format": {"type": "CURRENCY", "pattern": "$#,##0.00"}}
  ]
}
```

- Covers dates, times, numbers, currency, percent.
- Resolution: `Column.format` → `TableSchema.formats[codec]` → none (the library never touches that column's format).
- No pattern means the locale default: `{"type": "DATE"}` displays per the **spreadsheet's** locale
  (`ru_RU` → `12.10.2026`, `en_US` → `10/12/2026`). There are no locale presets in the library.
- The library reads `properties.locale` / `timeZone` and never changes them (changing the locale switches decimal and
  formula-argument separators for the whole file).
- When formats are applied (only on `app`-owned columns; `sheet` and `skip` columns are never formatted):
  - **create**: in the same atomic `updateCells`, mask `userEnteredValue,userEnteredFormat.numberFormat`;
  - **apply/reconcile**: one `repeatCell` per formatted column over the data rows, mask `userEnteredFormat.numberFormat`.
    Idempotent; it repairs cells someone reformatted by hand;
  - **push** (point update): values only; the next reconcile restores formats.
- Parsing text (CSV always; API when a cell holds a string Sheets didn't recognise, so `UNFORMATTED_VALUE` returns text):
  1. parse with the column's `pattern`, translated Sheets → strptime for numeric tokens
     (`yyyy yy m mm d dd h hh s ss AM/PM`; `m`/`mm` means minutes after `h`/before `s`, per Sheets rules).
     Text month names (`mmm`, `mmmm`) depend on locale → `SchemaError` for `PublicCsvTable`;
  2. if there is no pattern or it doesn't match, try the fallback list: ISO 8601, `dd.mm.yyyy[ hh:mm[:ss]]`,
     `mm/dd/yyyy[ hh:mm[:ss]]`, `yyyy-mm-dd[ hh:mm[:ss]]` **[default]**. A fallback match is accepted and reported via
     `ctx.warn` → `Snapshot.warnings` (`RowWarning(row_number, field, raw, matched_format)`);
  3. an ambiguous fallback (`01/02/2026` matches both `dd/mm` and `mm/dd`) or no match → `RowError`, never a guess.
  - Numbers in text use the same flow: `pattern`, then a fallback (decimal comma, spaces/NBSP as thousands separators,
    currency symbols stripped) with a warning.
- A fallback value in an `app` column differs from the hash, so the next reconcile rewrites it RAW. `sheet` columns are
  not written; the warning repeats every read.

## 6. Transport (`SheetsClient`)

- Async `httpx.AsyncClient` against Sheets REST v4; one client per `SheetsClient`, closed via `async with` / `aclose()`.
- Auth: `google.oauth2.service_account.Credentials` from a dict, scope `spreadsheets`. Token refresh is synchronous
  in google-auth, so it runs through `asyncio.to_thread` only when the token is expired.
- Retry (tenacity): 429, 500, 502, 503, 504 and `httpx.TransportError`. Exponential backoff with jitter, honours
  `Retry-After` (delta-seconds or HTTP-date), 5 attempts, then `ApiError`.
- Structural `batchUpdate` is not idempotent (positional `deleteDimension`, duplicate `createDeveloperMetadata`), so it
  retries only on 429 and on transport errors raised before the request is sent (`ConnectError`, `ConnectTimeout`,
  `PoolTimeout`). Any other failure (read timeout, dropped connection, 5xx) is an immediate `ApiError`; the caller
  re-reads and re-plans. Reads and `values:batchUpdate[ByDataFilter]` (idempotent) keep the full policy.
- Read options: `valueRenderOption=UNFORMATTED_VALUE`, `dateTimeRenderOption=SERIAL_NUMBER`.
- Write options: `valueInputOption=RAW`; `null` in a row array means "skip this cell" (this is how
  `sheet`/`skip` columns are left untouched).
- The tab title, grid size and timezone come from one `spreadsheets.get` (fields-masked), fetched on every `read()` (tabs get renamed, rows get added, `appendDimension` changes the grid size).

## 7. Identity

| mode | locate rows | create | notes |
|---|---|---|---|
| `metadata` | `developerMetadata:search` (ROW, sheetId, prefix filtered client-side) | atomic `batchUpdate` (below) | 30,000-char cap per tab, documented |
| `key_column` | read the key column | same `batchUpdate` without the metadata request, under lock | the only mode allowed for CSV |
| `metadata+key_column` | metadata first; untagged rows matched by the key column and re-tagged in `apply` | as `metadata` | recovery path |

- Atomic create (metadata modes), as one `batchUpdate` while the lock is held:
  `[appendDimension if the grid is full] + updateCells(row N) + createDeveloperMetadata(row N)`.
  `N` = last row containing data + 1, taken from a fresh read under the lock.
- Update (metadata modes): `values:batchUpdateByDataFilter` with a `developerMetadataLookup`, so the write
  follows the row wherever it has moved. `RowState.row_number` is not needed for this.
- Delete: `deleteDimension` on the resolved row index; multiple deletes are sent bottom-up in one `batchUpdate`.
  `clear`: app-owned cells are set to `""` and the tag is removed (`deleteDeveloperMetadata`).
- A duplicate key in the sheet (two tags or two key cells) puts both rows in `Snapshot.duplicates`; neither row is touched.
- **The metadata cap is a known ceiling.** README states it: about 30,000 / (len(prefix) + len(key)) rows per tab.
  Planning around it (rotating tabs, key column) is up to the consumer.
- Spike (first implementation step): `scripts/spike_metadata.py` checks what happens to a row tag when the row is sorted,
  filtered, cut/pasted, copied, when the tab is duplicated, and when the file is copied. The consumer runs it against a test spreadsheet;
  the findings go in the README.

## 8. Read, plan, apply

```python
@dataclass(frozen=True)
class RowState:  # persisted by the app
    key: str
    row_number: int | None  # hint only
    app_hash: str  # hash of the app-owned encoded values last written


@dataclass(frozen=True)
class Row:
    key: str
    row_number: int
    values: dict[str, Any]  # decoded, field → value (app + sheet columns)
    raw: tuple[Cell, ...]


@dataclass(frozen=True)
class RowError:
    row_number: int
    field: str | None
    raw: Cell
    message: str


@dataclass(frozen=True)
class RowWarning:
    row_number: int
    field: str
    raw: Cell
    matched_format: str  # fallback format that parsed the value


@dataclass(frozen=True)
class Snapshot:
    rows: dict[str, Row]
    errors: list[RowError]  # never capped, never silently dropped
    warnings: list[RowWarning]  # fallback parses (§5.1), never capped
    orphans: list[int]  # data rows with no resolvable key
    duplicates: dict[str, list[int]]


@dataclass(frozen=True)
class Plan:
    creates: dict[str, dict]  # key → record
    updates: dict[str, dict]  # only where the app-owned hash differs from the sheet
    deletes: dict[str, RowState]  # app records gone, per on_app_delete
    pulls: dict[str, dict[str, Any]]  # key → all sheet-owned values, every reconcile (app upserts idempotently)
    drift: dict[str, dict[str, tuple[Any, Any]]]  # key → field → (sheet value, app value) where sheet ≠ last written
    missing_in_sheet: list[str]  # app has a state, sheet has no row; report only, app decides
    orphans: list[int]
```

```python
client = SheetsClient(service_account_info)
table = client.table(spreadsheet_id, gid, schema, codecs=DEFAULT_CODECS)
csv = PublicCsvTable(url, schema, codecs=..., tz=ZoneInfo("Europe/Moscow"))  # read() only

snap = await table.read(strict=False)  # strict=True → raise on first RowError
plan = plan_sync(schema, snap, records, states)  # pure; records: {key: dict}, states: {key: RowState}
result = await table.apply(plan)  # caller holds the lock → ApplyResult(states, removed)
state = await table.push(key, record, lock=lock)  # single upsert: read + plan + apply under the lock
await table.delete(key, state, lock=lock)
```

- `app_hash` = sha256 over canonical JSON of the encoded app-owned cells in column order, truncated to 16 hex characters **[default]**.
- `ApplyResult.states` covers only the snapshot's rows and the creates; it is not a full replacement of the persisted
  states. The app merges (`states.update(result.states)`, drop `result.removed`): states of `missing_in_sheet` and
  duplicate keys are absent from it and must be kept, or the next reconcile re-creates those rows.
- Detecting drift: the hash of the current sheet app-owned cells ≠ `state.app_hash`.
- `apply` batches all value writes into one `values:batchUpdate`/`…ByDataFilter` call and all structural changes into one
  `batchUpdate` (deletes bottom-up).
- Locking contract: the caller holds one lock across `read → plan_sync → apply` (otherwise the snapshot can be stale);
  `apply` takes no lock. `push`/`delete` do read + plan + apply inside the lock they are given.
- `PublicCsvTable`: one `httpx` GET to `/export?format=csv&gid=`. A non-CSV response (an HTML login page,
  `content-type` not `text/csv`) raises `SourceError`. It requires `identity="key_column"`.

## 9. Locking

```python
class AsyncLock(Protocol):
    async def __aenter__(self) -> None: ...
    async def __aexit__(self, *exc) -> None: ...
```
Ships `NullLock` (default) and `AsyncioLock` (single process). Distributed locks live in the consumer.

## 10. dudeduck migration

Delete:
- `backend/src/services/sheets/` gspread code: `service.py` transport and parsing, dynamic `create_model` with `_CACHE`,
  `find`-based identity
- the one-time import `sync_orders` / `sync_data_from` / `get_create_payload_from_order_sheets` / `boosters_from_order_sync`
- `publish_sync_user`, `publish_sync_data`, `sync_user_to_sheets`, `bulk_sync_orders_to_sheets`, `bulk_create_row_data`
- `errors.GoogleSheetsParserError`, triple `allowed_types`, `order_sheet_parse` table and its CRUD views
- the `gspread` dependency

Add:
- `sheet_table(id, kind UNIQUE, spreadsheet_id, gid, schema_json, created_at, updated_at)`; configs entered by hand
  (seed script / SQL); no conversion from the old table.
- `sheet_row_state(sheet_table_id FK, key, row_number NULL, app_hash, updated_at, PK(sheet_table_id, key))` **[default]**.
- `order → record dict` mapper (keeps booster/screenshot string formatting, app-side).
- Column ownership: every `CreateUpdateOrderSheets` field is `app`; former `generated` columns are `skip`; nothing is `sheet` in v1.
- Event push: the `sheets.sync_order` handler → `table.push` / `table.delete` under a redis-py `Lock`
  `sheetsync:{spreadsheet_id}:{gid}` (timeout 60s, blocking 10s). If the lock times out, the push fails and reconcile repairs it.
- Reconcile: aiocron every 10 min in the worker: lock → `read` → `plan_sync` → `apply` → persist states,
  log `drift`/`errors`/`warnings`/`orphans`/`duplicates`.
- Admin UI: update the TS types only; no editor.
- Display formats live in `schema_json.formats` / per-column `format`; delete `config.app.datetime_format_sheets`.
  The first reconcile applies them to existing rows.

## 11. Tests

- Unit: codecs (round-trip, text paths, falsy values, tz), Sheets-pattern → strptime translation (`mm` minutes vs
  months, AM/PM), fallback with warning, ambiguous fallback → error, schema validation and header resolution,
  format resolution order, `plan_sync` (each Plan bucket, drift, duplicates, orphans), client retry and `Retry-After`,
  table request payloads via `httpx.MockTransport` (atomic create body incl. numberFormat, `repeatCell` masks,
  bottom-up deletes, null-skip for non-app columns), CSV HTML detection.
- Integration (opt-in, `SHEETSYNC_IT=1`, `SHEETSYNC_SA_JSON`, `SHEETSYNC_SPREADSHEET_ID`): create → update → drift →
  delete round trip on a real test sheet.

## 12. Implementation order

1. Repo skeleton, errors, codecs, schema (+ tests)
2. `plan_sync` (+ tests)
3. `SheetsClient` + `Table` + `PublicCsvTable` (+ MockTransport tests)
4. Metadata spike script + integration test; README with the ceiling and spike findings
5. dudeduck migration (§10)
