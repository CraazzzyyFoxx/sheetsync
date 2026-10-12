# sheetsync

Async Python library for bidirectional Google Sheets sync (Python >= 3.12). The design lives in [SPEC.md](SPEC.md).

## Install

```
uv add "sheetsync @ git+<repo-url>@v0.1.0"
```

`<repo-url>`: set once the repo has a remote. Releases are git tags.

## Quickstart

Describe the tab as a schema (JSON-shaped dict, a `TableSchema(...)`, or `TableSchema.from_model(Model, key_field="id")`;
`key_field` is required in every form):

```python
from datetime import date

from sheetsync import DEFAULT_CODECS, AsyncioLock, SheetsClient, TableSchema, plan_sync

schema = TableSchema.from_json(
    {
        "key_field": "id",
        "formats": {"date": {"type": "DATE", "pattern": "dd.mm.yyyy"}},
        "columns": [
            {"field": "id", "header": "ID"},
            {"field": "qty", "header": "Qty", "codec": "int"},
            {"field": "day", "header": "Day", "codec": "date"},
            {"field": "status", "header": "Status", "owner": "sheet"},
        ],
    },
    DEFAULT_CODECS,
)
lock = AsyncioLock()
records = {"o-1": {"id": "o-1", "qty": 3, "day": date(2026, 10, 12)}}  # key -> record dict
states = {}  # key -> RowState, persisted by the app between runs

async with SheetsClient(service_account_info) as client:  # service account JSON, loaded to a dict
    table = client.table(spreadsheet_id, gid, schema)  # codecs=DEFAULT_CODECS by default

    # reconcile: one lock across read -> plan -> apply
    async with lock:
        snap = await table.read()  # Snapshot: rows, errors, warnings, orphans, duplicates
        plan = plan_sync(schema, snap, records, states)
        # plan.creates / updates / deletes / pulls / drift / missing_in_sheet / orphans
        result = await table.apply(plan)  # ApplyResult(states, removed)
    states.update(result.states)  # merge: result.states is not a full replacement
    for key in result.removed:  # the keys of plan.deletes
        states.pop(key, None)

    # event push: single upsert / delete, the lock is held inside
    states["o-1"] = await table.push("o-1", records["o-1"], lock=lock)  # returns the new RowState
    await table.delete("o-1", states.pop("o-1"), lock=lock)
```

`ApplyResult.states` covers only the rows in the snapshot plus the creates, so it is not a full replacement: states of
`missing_in_sheet` and duplicate keys are absent from it, and replacing `states` wholesale would make the next reconcile
re-create those rows.

`read(strict=True)` raises `RowDecodeError` on the first bad row instead of collecting `snap.errors`.
`apply(plan, repair_formats=False)` skips the per-column format repair (`push` and `delete` always skip it).

## Column ownership

| owner | read | written | hashed | meaning |
|---|---|---|---|---|
| `app` | yes (drift check) | yes | yes | the app is the source of truth; pushed to the sheet |
| `sheet` | yes | never | no | people/formulas own it; values come back as `plan.pulls` |
| `skip` | no | no | no | documented only; the header resolves but the column is not synced |

There is no conflict resolution: the owner wins. A hand edit of an `app` cell is overwritten by the next write and
reported in `plan.drift` as `field -> (sheet value, app value)`.

## Identity modes

| `identity` | rows are located by | notes |
|---|---|---|
| `metadata` (default) | developer metadata tag `prefix + key` on the row | the tag follows the row when it moves; 30,000-char cap |
| `key_column` | the value in the key column | the only mode `PublicCsvTable` accepts |
| `metadata+key_column` | metadata first; untagged rows matched by the key column and re-tagged in `apply` | recovery path |

**Metadata ceiling.** Developer metadata is capped at 30,000 characters per tab, so a tab holds about

```
rows ~= 30_000 / (len(metadata_prefix) + len(key))
```

tagged rows: with the default prefix `k:` and 8-character keys, about 3,000 rows. The library does not work around the
cap; plan around it (rotate tabs, or use `key_column`). A duplicate key in the sheet (two tags or two key cells) lands in
`snap.duplicates`; neither row is touched.

## Display formats and fallback warnings

Values are written RAW (serial numbers, plain numbers); how they look is controlled only by the cell's number format.
`Column.format` overrides `TableSchema.formats[codec]`; with neither, the library never touches that column's format.
A format with no `pattern` follows the spreadsheet's locale. The library never changes the spreadsheet's locale or
timezone. Formats apply to `app` columns only: on create, and as one `repeatCell` per column on `apply` (which also
repairs hand-reformatted cells).

Text cells (always in CSV; in the API when Sheets did not recognise a value) are parsed with the column `pattern`, then
with a fallback list (ISO 8601, `dd.mm.yyyy`, `mm/dd/yyyy`, `yyyy-mm-dd`, decimal comma and thousands spaces for numbers).
A fallback match is accepted and reported in `snap.warnings` as `RowWarning(row_number, field, raw, matched_format)`; an
ambiguous or unparseable value is a `RowError` in `snap.errors`, never a guess. Both lists are never capped.

## Locking contract

The caller holds one lock across `read -> plan_sync -> apply`; otherwise the snapshot can be stale and row numbers wrong.
`apply` takes no lock. `push` and `delete` do read + plan + apply inside the lock you pass (`lock=`; default `NullLock`).
`AsyncioLock` covers a single process; for several processes pass your own `AsyncLock` (any object with async
`__aenter__` / `__aexit__`, e.g. a Redis lock wrapper).

## Public CSV source

`PublicCsvTable(url, schema, tz=ZoneInfo(...))` reads a published or link-shared sheet through one GET of
`/export?format=csv&gid=...`. Limits:

- read-only (`read()` only), no auth, no writes, no formats;
- `identity="key_column"` is required (CSV carries no metadata); there are no row tags, `orphans` and `duplicates` come from the key column;
- cells are display text, so values are parsed per the format rules above; month-name patterns (`mmm`, `mmmm`) depend on locale and raise `SchemaError`;
- the timezone is a constructor argument (no `spreadsheets.get`);
- a non-CSV answer (an HTML login page) raises `SourceError`.

## Metadata spike

`scripts/spike_metadata.py` checks what happens to a row tag when a person edits the sheet by hand. Use a *test*
spreadsheet shared (editor) with the service account:

```
export SHEETSYNC_SA_JSON=/path/to/service-account.json
export SHEETSYNC_SPREADSHEET_ID=<test spreadsheet id>
uv run python scripts/spike_metadata.py
```

It creates a scratch tab, writes 5 tagged rows, then walks through manual steps (sort, filter, cut/paste a row, copy a
row, duplicate the tab, make a copy of the file). After each step it re-reads and prints which tags sit on which rows,
and flags a tag that no longer matches its row's ID cell. At the end it offers to delete the scratch tab.

### Findings

Not run yet; fill in after running the spike (what happened to the tag).

| Operation | Tag follows the row? | Duplicated / lost? | Notes |
|---|---|---|---|
| Sort | | | |
| Filter (hidden rows) | | | |
| Cut / paste a row | | | |
| Copy / paste a row | | | |
| Duplicate the tab | | | |
| Make a copy of the file | | | |
| 30,000-char cap | | | rows tagged before the API refused |

## Tests

```
uv run pytest -q
```

Unit tests never touch the network. The integration test (create -> read -> update -> manual edit -> drift -> delete,
plus format check) runs against a real spreadsheet only when opted in:

```
SHEETSYNC_IT=1 SHEETSYNC_SA_JSON=... SHEETSYNC_SPREADSHEET_ID=... uv run pytest tests/test_integration.py
```
