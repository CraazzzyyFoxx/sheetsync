"""Spike: what happens to a row's developer-metadata tag when the sheet is edited by hand?

Needs a *test* spreadsheet shared (editor) with the service account.
Env: SHEETSYNC_SA_JSON (path to the service account JSON), SHEETSYNC_SPREADSHEET_ID.
Run: uv run python scripts/spike_metadata.py
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from sheetsync import DEFAULT_CODECS, SheetsClient, SheetsyncError, TableSchema, plan_sync

KEYS = [f"t{i}" for i in range(1, 6)]
SCHEMA = {
    "columns": [{"field": "id", "header": "ID"}, {"field": "note", "header": "Note"}],
    "key_field": "id",
}
STEPS = [
    "Sort the tab by the Note column, Z -> A (Data > Sort range > Advanced, or the column menu).",
    "Create a filter (Data > Create a filter) and hide two rows by filtering on Note; keep it active.",
    "Cut row 3 (select the row, Ctrl+X) and paste it onto row 6 (select row 6, Ctrl+V). Remove the filter first.",
    "Copy row 2 (Ctrl+C) and paste it onto the empty row 8 (Ctrl+V).",
    "Duplicate this tab (tab menu > Duplicate). The copy appears in the report under its own sheetId.",
    "Make a copy of the whole file (File > Make a copy). Share the copy with the service account email "
    "and paste its spreadsheet id at the prompt (empty = skip).",
]


def tab_url(spreadsheet_id: str, gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={gid}"


async def report(client: SheetsClient, table, spreadsheet_id: str, gid: int) -> None:
    """Print which tags sit on which rows: via the library for the scratch tab, raw search for every tab."""
    snap = await table.read()
    print("  scratch tab (library read): row | tag | ID cell")
    for row in sorted(snap.rows.values(), key=lambda r: r.row_number):
        flag = "" if row.values["id"] == row.key else "   <-- tag != ID cell"
        print(f"    {row.row_number:>3} | {row.key:<4} | {row.values['id']}{flag}")
    print(f"  duplicates={snap.duplicates} orphans={snap.orphans} errors={len(snap.errors)}")
    found = await client.search_developer_metadata(
        spreadsheet_id, [{"developerMetadataLookup": {"locationType": "ROW"}}]
    )
    by_sheet: dict[int, dict[int, str]] = {}
    for match in found.get("matchedDeveloperMetadata", []):
        meta = match["developerMetadata"]
        span = meta["location"]["dimensionRange"]
        by_sheet.setdefault(span["sheetId"], {})[span["startIndex"] + 1] = meta["metadataKey"]
    for sheet_id, tags in sorted(by_sheet.items()):
        label = "scratch" if sheet_id == gid else "other tab"
        print(
            f"  all ROW tags, sheetId={sheet_id} ({label}): " + ", ".join(f"{r}:{k}" for r, k in sorted(tags.items()))
        )


async def main() -> None:
    sa_json, spreadsheet_id = os.environ.get("SHEETSYNC_SA_JSON"), os.environ.get("SHEETSYNC_SPREADSHEET_ID")
    if not (sa_json and spreadsheet_id):
        sys.exit("set SHEETSYNC_SA_JSON and SHEETSYNC_SPREADSHEET_ID")
    info = json.loads(Path(sa_json).read_text())
    schema = TableSchema.from_json(SCHEMA, DEFAULT_CODECS)
    title = f"sheetsync-spike-{uuid4().hex[:6]}"
    async with SheetsClient(info) as client:
        reply = await client.batch_update(spreadsheet_id, [{"addSheet": {"properties": {"title": title}}}])
        gid = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
        try:
            await client.values_batch_update(spreadsheet_id, [{"range": f"'{title}'!A1", "values": [["ID", "Note"]]}])
            table = client.table(spreadsheet_id, gid, schema)
            snap = await table.read()
            records = {k: {"id": k, "note": f"note {n}"} for n, k in zip("ECABD", KEYS, strict=True)}
            await table.apply(plan_sync(schema, snap, records, {}))
            print(f"Scratch tab '{title}': {tab_url(spreadsheet_id, gid)}")
            print(f"Service account: {info.get('client_email')}")
            print("Written: 5 tagged rows (t1..t5, rows 2-6). Initial state:")
            await report(client, table, spreadsheet_id, gid)
            for n, step in enumerate(STEPS, 1):
                print(f"\nStep {n}: {step}")
                if n == len(STEPS):
                    copy_id = input("Copy's spreadsheet id (empty = skip): ").strip()
                    if copy_id:
                        copy_snap = await client.search_developer_metadata(
                            copy_id, [{"developerMetadataLookup": {"locationType": "ROW"}}]
                        )
                        keys = sorted(
                            m["developerMetadata"]["metadataKey"] for m in copy_snap.get("matchedDeveloperMetadata", [])
                        )
                        print(f"  tags found in the copy: {keys or 'none'}")
                    continue
                input("Press Enter when done... ")
                await report(client, table, spreadsheet_id, gid)
        finally:
            if input("\nDelete the scratch tab? [y/N] ").strip().lower() == "y":
                await client.batch_update(spreadsheet_id, [{"deleteSheet": {"sheetId": gid}}])
                print("Deleted (a duplicated tab, if you made one, stays: delete it by hand).")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    try:
        asyncio.run(main())
    except SheetsyncError as e:
        sys.exit(f"sheetsync error: {e}")
