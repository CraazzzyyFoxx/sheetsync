# sheetsync — implementation plan

Spec: `SPEC.md` (binding authority). This plan pins the interfaces between tasks; where it is more specific than
the spec, the plan wins only if the spec is silent.

## Global Constraints

- Python `>=3.12`, uv, src layout (`src/sheetsync/`), tests in `tests/`, pytest + pytest-asyncio (`asyncio_mode = "auto"`),
  ruff (line length 120, rules `E,F,I,UP,B`).
- Runtime deps exactly: `httpx`, `google-auth[requests]`, `tenacity`, `pydantic>=2`, `tzdata` (Windows has no system tz db).
  Dev deps: `pytest`, `pytest-asyncio`, `ruff`. Nothing else.
- Unit tests never touch the network: HTTP via `httpx.MockTransport`.
- Minimal code: no abstractions with one implementation, no config for constants, no speculative features.
  Deliberate simplifications with a known ceiling carry a `# ponytail: <ceiling>, <upgrade path>` comment.
- Module layout (SPEC §3) with these placement rulings:
  - `NumberFormat`, `Cell`, `Ctx`, `Codec` live in `codecs.py` (schema imports codecs, never the reverse).
  - `RowError`, `RowWarning`, `RowDecodeError` live in `errors.py` (schema and plan both use them).
- Public names are re-exported from `sheetsync/__init__.py` as each task adds them.
- Every task: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` green before commit.
  Commit messages: conventional commits (`feat: …`, `test: …`, `docs: …`).

## Shared contracts (all tasks)

```python
# codecs.py
Cell = str | int | float | bool | None

class NumberFormat(BaseModel):            # frozen
    type: Literal["DATE", "TIME", "DATE_TIME", "NUMBER", "CURRENCY", "PERCENT", "SCIENTIFIC", "TEXT"]
    pattern: str | None = None

@dataclass(frozen=True)
class Ctx:
    tz: ZoneInfo
    options: Mapping[str, Any] = MappingProxyType({})
    format: NumberFormat | None = None
    warn: Callable[[str], None] = _noop    # called with the matched fallback format label

class Codec(Protocol[T]):
    def decode(self, raw: Cell, ctx: Ctx) -> T: ...   # never called with an empty cell (caller filters)
    def encode(self, value: T, ctx: Ctx) -> Cell: ... # never called with None (caller maps None → "")

DEFAULT_CODECS: Mapping[str, Codec]       # read-only mapping: str int float bool date datetime enum list

# errors.py
class SheetsyncError(Exception)
class SchemaError(SheetsyncError)
class ApiError(SheetsyncError):  status: int; body: str
class SourceError(SheetsyncError)
class LockTimeout(SheetsyncError)
class RowDecodeError(SheetsyncError): error: RowError     # raised by read(strict=True)

@dataclass(frozen=True) class RowError:   row_number: int; field: str | None; raw: Cell; message: str
@dataclass(frozen=True) class RowWarning: row_number: int; field: str; raw: Cell; matched_format: str
```

Codec decode failures raise `ValueError` (or `TypeError`) with a human-readable message; the caller turns them into
`RowError`. Codecs never return `None`.

---

### Task 1: Skeleton, errors, locks, typed codecs

Files: `pyproject.toml`, `.gitignore`, `README.md` (one paragraph placeholder pointing at SPEC.md),
`src/sheetsync/__init__.py`, `errors.py`, `lock.py`, `codecs.py`, `tests/test_codecs.py`.

- `pyproject.toml` per Global Constraints; build backend `hatchling`; version `0.1.0`.
- `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `dist/`, `.superpowers/`.
- `errors.py`: the classes from Shared contracts, nothing more.
- `lock.py`: `AsyncLock` Protocol (`__aenter__`/`__aexit__`), `NullLock` (does nothing), `AsyncioLock`
  (wraps `asyncio.Lock`). No tests (trivial wrappers).
- `codecs.py` — this task implements the **typed-value path only** (API values: numbers, bools, strings that are
  already canonical). Text parsing of formatted strings is Task 2; leave a single private hook per codec where Task 2
  plugs in (`_parse_datetime_text`, `_parse_number_text`) that, in this task, accepts only canonical text:
  `datetime.fromisoformat` / `float()`, else `ValueError`.
  - Serial dates: epoch `datetime(1899, 12, 30)`. `to_serial(dt: datetime, tz) -> float`: convert to `tz`, drop tzinfo,
    `(naive - epoch) / timedelta(days=1)`. `from_serial(x: float, tz) -> datetime`: `epoch + timedelta(days=x)` rounded
    to the nearest millisecond, `replace(tzinfo=tz)`. Datetime precision on the wire is milliseconds: `encode` rounds to
    milliseconds, so round-trip holds for millisecond-precision values.
  - `str`: decode → `str(raw)`, but an integral float becomes `"5"` not `"5.0"`; bool → `"TRUE"`/`"FALSE"`.
    encode → `str(value)`.
  - `int`: decode int → int; integral float → int; non-integral float → ValueError; bool → ValueError; str → via
    number text hook then must be integral. encode → int.
  - `float`: decode int/float → float; bool → ValueError; str → number text hook. encode → float.
  - `bool`: decode bool → bool; str → case-folded, stripped lookup in `options["true_values"]`
    (default `{"true", "1", "yes", "y", "да"}`) / `options["false_values"]` (default `{"false", "0", "no", "n", "нет"}`);
    number 1/0 → True/False; anything else ValueError. encode → bool.
  - `date`: decode number → `from_serial(x).date()`; str → text hook, must be midnight, `.date()`.
    encode: `datetime` instance → ValueError; `date` → integer serial (`(date - epoch.date()).days`).
  - `datetime`: decode number → `from_serial(x, ctx.tz)` (aware); str → text hook (naive → `replace(tzinfo=ctx.tz)`,
    aware → `astimezone(ctx.tz)`). encode: naive → ValueError; aware → `to_serial` (milliseconds).
  - `enum`: `options["value_map"]` (`{cell_text: value}`) or `options["valid_values"]` (list). decode: value_map lookup by
    `str(raw)`; else must be in valid_values (compare `str(raw)`), returns the matching valid value; else ValueError
    listing allowed values. encode: value_map reverse lookup (ValueError if absent); valid_values membership check,
    returns `str(value)`. Neither option → ValueError at decode/encode time.
  - `list`: `ListCodec(item: Codec = StrCodec())`; `options["separator"]` default `", "`. decode: `str(raw)` split on
    separator, strip, drop empties, each via `item.decode`. encode: join `str(item.encode(v))`.
  - Falsy values round-trip as themselves: `0`, `0.0`, `False` are never turned into `""`.
- Tests (`tests/test_codecs.py`): round-trip `decode(encode(v)) == v` for every built-in on typed values incl. `0`,
  `0.0`, `False`, aware datetime in a non-UTC tz across DST, date; serial anchor values (`1899-12-30` → 0,
  `1900-01-01` → 2, `2026-10-12 12:00` in `Europe/Moscow` → known serial); error cases (naive datetime encode,
  non-integral int decode, bool true/false sets, enum both modes, datetime given to date encode).

### Task 2: Display-format text parsing (SPEC §5.1)

Files: `src/sheetsync/codecs.py`, `tests/test_text_parsing.py`.

- `sheets_pattern_to_strptime(pattern: str) -> str`. Tokens: `yyyy`→`%Y`, `yy`→`%y`, `m`/`mm`→`%m` (month) or `%M`
  (minute: when the previous date/time token was `h`/`hh`, or the next one is `s`/`ss`), `d`/`dd`→`%d`,
  `h`/`hh`→`%H` (or `%I` when the pattern contains `AM/PM`), `s`/`ss`→`%S`, `AM/PM`→`%p`. Quoted literals `"..."`
  and backslash-escaped chars are literal; `%` in literals is escaped as `%%`; any other char is literal.
  Unsupported → `ValueError`: `mmm`, `mmmm`, `mmmmm`, `ddd`, `dddd`, `[h]`, `[m]`, `[s]`, fractional seconds `.0`.
- Datetime text (used by `date` and `datetime` codecs):
  1. `ctx.format` has a pattern → try `strptime(text, translated)`. Success → value, no warning.
  2. Otherwise fallback. If the column has **no** pattern, canonical ISO (`datetime.fromisoformat`) is accepted
     silently first. Then the fallback list (label → strptime), each also tried with ` hh:mm` and ` hh:mm:ss` suffixes:
     `ISO 8601` (only when a pattern was set — then it is a fallback), `dd.mm.yyyy`, `dd/mm/yyyy`, `mm/dd/yyyy`,
     `yyyy-mm-dd`.
  3. Collect every matching entry. Exactly one distinct resulting value → `ctx.warn(label)` (label includes the time
     suffix actually matched, e.g. `"dd.mm.yyyy hh:mm"`), return it. More than one distinct value → `ValueError`
     (`"ambiguous date …: matches dd/mm/yyyy and mm/dd/yyyy"`). None → `ValueError`.
- Number text (used by `int` and `float`):
  1. Canonical `float(text)` accepted silently **only when the column has no pattern**.
  2. Pattern set → strip the pattern's literal prefix/suffix characters (anything that is not `0 # ? , . % E e + -`
     and not inside the digits), remove `,` grouping, parse with `.` decimal; pattern contains `%` → divide by 100.
     Success → no warning.
  3. Fallback: remove spaces, NBSP (`\u00a0`), narrow NBSP (`\u202f`), currency symbols and any char not in
     `0-9 , . - + e E %`. Both `,` and `.` present → the last one is the decimal separator. Only `,`: exactly one comma
     with exactly 3 digits after it → `ValueError` ambiguous; otherwise `,` is the decimal separator. Trailing `%` →
     divide by 100. Success → `ctx.warn("number fallback")`.
- Wire both into the Task 1 hooks; Task 1 behaviour for typed values is unchanged.
- Tests: translation table (incl. `mm` minutes vs months both directions, `AM/PM` 12h, literals, every unsupported token);
  pattern hit without warning; pattern miss → fallback with warning label; no-pattern ISO silent; `01/02/2026` ambiguous
  error; `13/02/2026` → dd/mm with warning; numbers: `"$1,234.50"` with `$#,##0.00`, `"12%"` with `0%`,
  `"1 234,56"` fallback warning, `"1,234"` ambiguous, int codec on `"5"` without pattern silent.

### Task 3: Schema

Files: `src/sheetsync/schema.py`, `tests/test_schema.py`.

```python
class Owner(StrEnum): APP = "app"; SHEET = "sheet"; SKIP = "skip"

class Column(BaseModel):            # fields exactly as SPEC §4 incl. `format: NumberFormat | None = None`
class TableSchema(BaseModel):       # fields exactly as SPEC §4 incl. `formats: dict[str, NumberFormat] = {}`
    @property first_data_row -> int                       # data_start_row or header_row + 1
    def format_for(self, column: Column) -> NumberFormat | None   # column.format → formats[codec] → None
    def validate_codecs(self, codecs: Mapping[str, Codec]) -> None # SchemaError on unknown codec name
    @classmethod from_json(cls, data: Mapping, codecs: Mapping[str, Codec]) -> TableSchema
    @classmethod from_model(cls, model: type[BaseModel], **table_opts) -> TableSchema

class Col:   # annotation marker for from_model: Annotated[T, Col(header=..., index=..., codec=..., owner=..., required=..., options=..., format=...)]

def resolve_columns(schema, header_cells: Sequence[Cell]) -> dict[str, int]      # field → 0-based index
def get_path(record: Mapping, path: str) -> Any        # dotted traversal; KeyError if absent
def decode_row(schema, indexes, cells, row_number, codecs, tz) -> tuple[dict[str, Any], list[RowError], list[RowWarning]]
def encode_record(schema, indexes, record, codecs, tz) -> list[Cell]
```

- Model validation (pydantic validators; `from_json` wraps `ValidationError` into `SchemaError`): duplicate `field`;
  column with neither `header` nor `index`; `key_field` must name a column when identity contains `key_column`;
  `key_field` is always required.
- `from_json` = `model_validate` + `validate_codecs`. `from_model`: one Column per field annotated with `Col`,
  `field` = field name; nested `BaseModel` fields recurse with dotted prefix (`price.dollar`); fields without `Col`
  are ignored; `codec` default inferred from the annotation (`str int float bool date datetime`, `Optional[...]`
  unwrapped), else `"str"`.
- `resolve_columns` (SPEC §4): header text compared stripped + case-folded; header given and found once → index;
  header given + index given → header must sit at that index else SchemaError; header only, duplicated → SchemaError;
  index only → index; header missing → SchemaError for app/sheet columns, column omitted from the result for skip
  columns. Skip columns never appear in the result when their header is missing.
- `decode_row`: for every app and sheet column in `indexes`: cell beyond row length or `None`/`""`/whitespace-only str
  → value `None` (RowError `"required"` if `required`); else `codecs[column.codec].decode(raw, Ctx(tz, column.options,
  schema.format_for(column), warn))`; `ValueError`/`TypeError` → RowError with the message; each `warn(label)` →
  RowWarning. Returned dict is flat, keyed by dotted field.
- `encode_record`: list of length `max(indexes.values()) + 1`, all `None`; for each app column: `get_path(record,
  field)` (missing → SchemaError naming the field); `None` → `""`; else codec encode. Non-app positions stay `None`.
- Tests: each validation error; format resolution order; header resolution rules (case/space-insensitive, duplicate,
  pin mismatch, skip tolerated); decode: empty/required/padding/errors/warnings collected per row; encode: width,
  `None` → `""`, non-app `None`, falsy values preserved, missing field error; `from_model` with nested model and
  Optional.

### Task 4: Planner

Files: `src/sheetsync/plan.py`, `tests/test_plan.py`.

```python
@dataclass(frozen=True) class RowState: key: str; row_number: int | None; app_hash: str
@dataclass(frozen=True) class Row: key: str; row_number: int; values: dict[str, Any]; raw: tuple[Cell, ...]; tagged: bool
@dataclass(frozen=True) class Snapshot:
    rows: dict[str, Row]; errors: list[RowError]; warnings: list[RowWarning]; orphans: list[int]
    duplicates: dict[str, list[int]]; tz: ZoneInfo; indexes: dict[str, int]; last_data_row: int; grid_rows: int
@dataclass(frozen=True) class Plan:
    creates: dict[str, dict]; updates: dict[str, dict]; deletes: dict[str, RowState]; pulls: dict[str, dict[str, Any]]
    drift: dict[str, dict[str, tuple[Any, Any]]]; missing_in_sheet: list[str]; orphans: list[int]; snapshot: Snapshot

def app_cells(schema, indexes, cells: Sequence[Cell]) -> list[Cell]      # app-column cells in schema column order
def app_hash(cells: Sequence[Cell]) -> str
def plan_sync(schema, snapshot, records: Mapping[str, Mapping], states: Mapping[str, RowState], *, codecs=DEFAULT_CODECS) -> Plan
```

- Canonical cell for hashing/comparison: `None` → `""`; bool stays bool (check before int); integral float → int;
  other float → `round(x, 9)`; str unchanged. `app_hash` = sha256 of `json.dumps(canonical, ensure_ascii=False,
  separators=(",", ":"))`, first 16 hex chars.
- Buckets (keys in `snapshot.duplicates` are excluded from every bucket):
  - record key in `snapshot.rows`: encode via `encode_record`; if `app_hash(encoded app cells)` ≠ `app_hash(sheet app
    cells)` → `updates[key] = record`.
  - record key not in rows, no state → `creates`. Not in rows but has a state → `missing_in_sheet` only.
  - state key not in records → `deletes[key] = state` unless `on_app_delete == "ignore"`.
  - `drift`: key has a state and a row and `app_hash(sheet app cells) != state.app_hash` → per app column whose
    canonical sheet cell ≠ canonical encoded record cell: `field → (row.values[field], get_path(record, field))`;
    records absent → skip drift.
  - `pulls`: every row in `snapshot.rows` → its sheet-owned values (empty dict rows omitted when the schema has no sheet
    columns, i.e. `pulls` is `{}` then).
  - `orphans` copied from the snapshot.
- Tests: one per bucket, duplicates excluded, update suppressed when only formatting-equivalent (`5` vs `5.0`, `None`
  vs `""`) differs, drift field detail, ignore delete policy, missing_in_sheet vs create.

### Task 5: Transport client

Files: `src/sheetsync/client.py`, `tests/test_client.py`.

```python
class SheetsClient:
    def __init__(self, service_account_info: Mapping | None = None, *, credentials=None,
                 transport: httpx.AsyncBaseTransport | None = None, max_attempts: int = 5, backoff: float = 1.0): ...
    async def __aenter__/__aexit__; async def aclose()
    def table(self, spreadsheet_id: str, gid: int, schema, codecs=DEFAULT_CODECS) -> "Table"   # lazy import of table.py
    async def get_spreadsheet(self, spreadsheet_id: str, fields: str) -> dict
    async def values_batch_get(self, spreadsheet_id: str, ranges: list[str]) -> dict    # UNFORMATTED_VALUE, SERIAL_NUMBER, ROWS
    async def values_batch_update(self, spreadsheet_id: str, data: list[dict]) -> dict  # valueInputOption RAW
    async def values_batch_update_by_data_filter(self, spreadsheet_id: str, data: list[dict]) -> dict  # RAW
    async def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> dict
    async def search_developer_metadata(self, spreadsheet_id: str, data_filters: list[dict]) -> dict
```

- Base URL `https://sheets.googleapis.com/v4/spreadsheets/`. Exactly one of `service_account_info` / `credentials`;
  info → `google.oauth2.service_account.Credentials.from_service_account_info(info, scopes=[".../auth/spreadsheets"])`.
- Auth header `Bearer <token>`; when `not credentials.valid` refresh via `asyncio.to_thread(credentials.refresh,
  google.auth.transport.requests.Request())`, guarded by an `asyncio.Lock` so concurrent calls refresh once.
- Retry with tenacity: retry on HTTP 429/500/502/503/504 and `httpx.TransportError`; `max_attempts`; wait =
  `Retry-After` seconds when the response carries it, else exponential with jitter starting at `backoff` (cap 32 s).
  After the last attempt, or immediately on other non-2xx: `ApiError(status, body)` (status 0 for transport errors).
- Tests with `MockTransport` and a fake credentials object (`valid`, `token`, `refresh`), `backoff=0`: query params of
  each method, RAW input option, retries on 429 then success, `Retry-After` honoured (wait function unit-tested
  directly, no real sleeping), non-retryable 400 raises immediately with body, exhaustion raises `ApiError`,
  single refresh under concurrency.

### Task 6: Table read + CSV source

Files: `src/sheetsync/table.py`, `tests/test_table_read.py`.

```python
def build_snapshot(schema, codecs, tz, rows: list[list[Cell]], tags: Mapping[int, str], grid_rows: int,
                   *, strict: bool = False) -> Snapshot        # rows: whole tab, row 1 first; tags: row_number → key
class Table:
    def __init__(self, client, spreadsheet_id: str, gid: int, schema, codecs=DEFAULT_CODECS)  # validate_codecs
    async def read(self, *, strict: bool = False) -> Snapshot
class PublicCsvTable:
    def __init__(self, url: str, schema, *, tz: ZoneInfo, codecs=DEFAULT_CODECS, transport=None)
    async def read(self, *, strict: bool = False) -> Snapshot
```

- `build_snapshot`: headers from row `schema.header_row` → `resolve_columns`. Data rows from `first_data_row` to the
  last row having any non-empty cell at an app/sheet index (`last_data_row`; `first_data_row - 1` when none).
  Fully empty rows skipped. Key: `tags[row_number]` if present (`tagged=True`); else, when identity contains
  `key_column`, the decoded key column value as `str` (`tagged=False`); else / empty → `orphans`. Same key on two rows
  → `duplicates[key] = [rows]` and neither row in `rows`. `decode_row` errors/warnings collected; `strict=True` raises
  `RowDecodeError` on the first error. Snapshot `tz`, `indexes`, `last_data_row`, `grid_rows` filled.
- `Table.read`: meta cached on the instance after the first call — `get_spreadsheet(fields="properties(timeZone,locale),
  sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))")`; gid not found → `SchemaError`.
  Values: `values_batch_get([f"'{title}'"])` (single quotes in title doubled). Tags (metadata modes only):
  `search_developer_metadata([{"developerMetadataLookup": {"locationType": "ROW", "metadataLocation": {"sheetId": gid}}}])`,
  keep keys starting with `schema.metadata_prefix`, row = `dimensionRange.startIndex + 1`.
- `PublicCsvTable`: `identity` must be `"key_column"` else `SchemaError`; every resolved pattern of `date`/`datetime`
  columns must translate (`sheets_pattern_to_strptime`) else `SchemaError` at construction. URL → `sheet_id` via
  `/spreadsheets/d/([^/]+)`, gid from `?gid=` or `#gid=`; export URL
  `https://docs.google.com/spreadsheets/d/{id}/export?format=csv[&gid={gid}]`; GET with `follow_redirects=True`,
  timeout 20 s; non-2xx or `content-type` not starting with `text/csv` → `SourceError`. BOM stripped, stdlib `csv`;
  `grid_rows = len(rows)`.
- Tests: fixtures as plain row lists; tagged + key-column + orphan + duplicate; trailing empty rows; header row offset;
  strict raise; Table.read request sequence and meta caching via MockTransport; title quoting; CSV HTML response →
  SourceError; CSV identity guard; CSV `mmm` pattern guard; CSV happy path with a fallback warning.

### Task 7: Table writes (apply / push / delete)

Files: `src/sheetsync/table.py`, `tests/test_table_write.py`.

```python
@dataclass(frozen=True) class ApplyResult: states: dict[str, RowState]; removed: list[str]
class Table:
    async def apply(self, plan: Plan, *, repair_formats: bool = True) -> ApplyResult   # caller holds the lock
    async def push(self, key: str, record: Mapping, *, lock: AsyncLock | None = None) -> RowState
    async def delete(self, key: str, state: RowState, *, lock: AsyncLock | None = None) -> None
```

Ruling vs SPEC §8 (`apply(plan, lock=…)`): `apply` takes no lock — the caller holds one lock across
`read → plan_sync → apply`, otherwise the snapshot can be stale. `push`/`delete` do read+plan+apply inside the lock.

- Value writes (call 1–2, skipped when empty), from `plan.updates`, cells = `encode_record` (None = skip):
  tagged rows in metadata modes → `values_batch_update_by_data_filter` with `{"dataFilter": {"developerMetadataLookup":
  {"metadataKey": prefix + key, "locationType": "ROW", "metadataLocation": {"sheetId": gid}}}, "majorDimension": "ROWS",
  "values": [cells]}`; untagged rows → `values_batch_update` with range `'{title}'!A{row_number}`.
- Structural `batch_update` (one call, skipped when empty), request order:
  1. `appendDimension` (ROWS, length = shortfall) when creates exceed `grid_rows`.
  2. Creates at rows `last_data_row + 1 …` in plan order: one `updateCells` per contiguous run of app columns that
     share "has a resolved format" — `rows: [{"values": [CellData…]}]`, `start {sheetId, rowIndex, columnIndex}`,
     `fields` = `"userEnteredValue,userEnteredFormat.numberFormat"` for formatted runs else `"userEnteredValue"`.
     CellData: `userEnteredValue` = `numberValue` (int/float) | `boolValue` | `stringValue` (non-empty str); `""` →
     no `userEnteredValue` (clears); formatted → `userEnteredFormat.numberFormat = {type, pattern?}`. Sheet/skip
     columns are never covered by any run.
  3. Metadata modes: `createDeveloperMetadata` for each create and for each untagged row in `plan.updates` or
     unchanged rows found via key column (re-tag, `metadata+key_column`): `{"metadataKey": prefix + key, "location":
     {"dimensionRange": {"sheetId": gid, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r}}, "visibility": "DOCUMENT"}`.
  4. `repair_formats`: one `repeatCell` per formatted app column over rows `[first_data_row - 1, last row after creates)`,
     `cell.userEnteredFormat.numberFormat`, `fields: "userEnteredFormat.numberFormat"`.
  5. Deletes for keys present in the snapshot: `delete_row` → `deleteDimension` per row, descending row order;
     `clear` → `updateCells` runs with empty CellData (`fields: "userEnteredValue"`) + `deleteDeveloperMetadata` by
     `metadataKey` lookup (metadata modes). Keys absent from the sheet: no request.
- `ApplyResult.states`: every key in the snapshot rows that is not deleted, plus creates — `RowState(key, final
  row_number after deletes above it, app_hash of the app cells now in the sheet)` (for written rows the encoded record
  cells; unchanged rows their sheet cells). `removed`: plan.deletes keys.
- `push`: under lock (`NullLock` default): `read()` → `plan_sync(schema, snap, {key: record}, {})` → `apply(plan,
  repair_formats=False)` → return `states[key]`. Duplicated key → `SheetsyncError`. Comment:
  `# ponytail: full-tab read per push, cache row lookups if push rate grows`.
- `delete`: under lock: `read()` → `plan_sync(schema, snap, {}, {key: state})` → `apply(plan, repair_formats=False)`.
- Tests (MockTransport capturing request JSON): update by data filter (null-skip in cells), untagged A1 update,
  create body incl. appendDimension and runs split by owner and format, `""` clears, metadata create, re-tag,
  repeatCell masks, delete ordering bottom-up and after creates, clear policy, ApplyResult row numbers after deletes,
  push creates when missing / updates when present / no write when equal, delete no-op for absent key.

### Task 8: Spike script, integration test, README

Files: `scripts/spike_metadata.py`, `tests/test_integration.py`, `README.md`.

- `scripts/spike_metadata.py`: env `SHEETSYNC_SA_JSON` (path to service account JSON) and `SHEETSYNC_SPREADSHEET_ID`.
  Creates a scratch tab, writes 5 tagged rows via the library, then prints numbered manual steps (sort, filter, cut/paste
  a row, copy a row, duplicate the tab, make a copy of the file) and after each `input()` re-reads and prints which tags
  sit on which rows. Deletes the scratch tab at the end (prompted).
- `tests/test_integration.py`: skipped unless `SHEETSYNC_IT=1` (+ the two env vars). Scratch tab; create → read →
  update → manual cell edit (via API) → drift reported → delete → row gone; formats applied (read back
  `userEnteredFormat.numberFormat` via `get_spreadsheet` fields).
- `README.md`: install (git tag), quickstart (schema JSON, read/plan/apply, push), ownership table, identity modes and
  the **30,000-char metadata ceiling** formula, display formats + fallback warnings, locking contract (caller holds the
  lock across read/plan/apply), CSV source limits, spike instructions with a "findings" section left as a table to fill.

### Task 9: dudeduck migration

Repo: dudeduck, in a git worktree on branch `feat/sheetsync` from `develop`. Implements SPEC §10. Brief supplied
separately by the controller (needs dudeduck context).
