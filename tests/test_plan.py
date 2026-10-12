import sys
from zoneinfo import ZoneInfo

from sheetsync.plan import Plan, Row, RowState, Snapshot, app_cells, app_hash, plan_sync
from sheetsync.schema import Column, Owner, TableSchema

TZ = ZoneInfo("UTC")
INDEXES = {"id": 0, "name": 1, "qty": 2, "note": 3}


def make_schema(**opts) -> TableSchema:
    cols = [
        Column(field="id", header="id"),
        Column(field="name", header="name"),
        Column(field="qty", header="qty", codec="int"),
        Column(field="note", header="note", owner=Owner.SHEET),
    ]
    return TableSchema(columns=cols, key_field="id", **opts)


def row(key: str, number: int, name="n", qty=1, note=None) -> Row:
    return Row(key, number, {"id": key, "name": name, "qty": qty, "note": note}, (key, name, qty, note), True)


def snap(*rows: Row, duplicates=None, orphans=None) -> Snapshot:
    return Snapshot({r.key: r for r in rows}, [], [], orphans or [], duplicates or {}, TZ, INDEXES, 10, 20)


def rec(key: str, name="n", qty=1) -> dict:
    return {"id": key, "name": name, "qty": qty}


def state_for(r: Row, schema: TableSchema | None = None) -> RowState:
    return RowState(r.key, r.row_number, app_hash(app_cells(schema or make_schema(), INDEXES, r.raw)))


def plan(snapshot: Snapshot, records: dict, states: dict | None = None, **opts) -> Plan:
    return plan_sync(make_schema(**opts), snapshot, records, states or {})


def test_app_cells_schema_order_and_short_row():
    s = make_schema()
    assert app_cells(s, INDEXES, ("a", "b", 3, "x")) == ["a", "b", 3]
    assert app_cells(s, INDEXES, ("a",)) == ["a", None, None]
    assert app_cells(s, {"id": 0, "qty": 2}, ("a", "b", 3)) == ["a", 3]


def test_app_hash_canonical_forms():
    assert app_hash([5]) == app_hash([5.0])
    assert app_hash([None]) == app_hash([""])
    assert app_hash([0.1 + 0.2]) == app_hash([0.3])
    assert app_hash([True]) != app_hash([1])
    assert app_hash(["é"]) == app_hash(["é"])
    assert len(app_hash([])) == 16


def test_create_when_unknown_everywhere():
    p = plan(snap(), {"a": rec("a")})
    assert p.creates == {"a": rec("a")}
    assert not p.updates and not p.missing_in_sheet and not p.deletes


def test_missing_in_sheet_when_state_but_no_row():
    p = plan(snap(), {"a": rec("a")}, {"a": RowState("a", 3, "x")})
    assert p.missing_in_sheet == ["a"]
    assert not p.creates and not p.deletes


def test_update_when_app_cells_differ():
    r = row("a", 2)
    p = plan(snap(r), {"a": rec("a", name="new")}, {"a": state_for(r)})
    assert p.updates == {"a": rec("a", name="new")}


def test_no_update_when_identical():
    r = row("a", 2)
    p = plan(snap(r), {"a": rec("a")}, {"a": state_for(r)})
    assert not p.updates and not p.drift


def test_update_suppressed_for_formatting_equivalents():
    r = Row("a", 2, {"id": "a", "name": None, "qty": 5.0, "note": None}, ("a", "", 5.0, None), True)
    p = plan(snap(r), {"a": {"id": "a", "name": None, "qty": 5}})
    assert not p.updates


def test_delete_when_state_without_record():
    st = RowState("gone", 4, "h")
    p = plan(snap(), {}, {"gone": st})
    assert p.deletes == {"gone": st}


def test_ignore_delete_policy():
    p = plan(snap(), {}, {"gone": RowState("gone", 4, "h")}, on_app_delete="ignore")
    assert p.deletes == {}


def test_drift_distinguishes_bool_from_number():
    old = row("a", 2, qty=1)
    edited = row("a", 2, qty=True)
    p = plan(snap(edited), {"a": rec("a", qty=1)}, {"a": state_for(old)})
    assert p.drift == {"a": {"qty": (True, 1)}}
    assert "a" in p.updates


def test_public_exports():
    import sheetsync

    for name in ("Plan", "Row", "RowState", "Snapshot", "app_cells", "app_hash", "plan_sync"):
        assert name in sheetsync.__all__
        assert getattr(sheetsync, name) is getattr(sys.modules["sheetsync.plan"], name)


def test_drift_lists_changed_fields():
    r = row("a", 2, name="sheet-edit", qty=1)
    old = row("a", 2, name="old", qty=1)
    p = plan(snap(r), {"a": rec("a", name="app", qty=1)}, {"a": state_for(old)})
    assert p.drift == {"a": {"name": ("sheet-edit", "app")}}
    assert "a" in p.updates


def test_no_drift_when_sheet_matches_state():
    r = row("a", 2)
    p = plan(snap(r), {"a": rec("a", name="app")}, {"a": state_for(r)})
    assert p.drift == {} and "a" in p.updates


def test_no_drift_without_record():
    r = row("a", 2, name="edited")
    p = plan(snap(r), {}, {"a": state_for(row("a", 2))})
    assert p.drift == {}
    assert "a" in p.deletes


def test_pulls_hold_sheet_owned_values():
    p = plan(snap(row("a", 2, note="hi"), row("b", 3)), {})
    assert p.pulls == {"a": {"note": "hi"}, "b": {"note": None}}


def test_pulls_empty_without_sheet_columns():
    s = TableSchema(columns=[Column(field="id", header="id")], key_field="id")
    snapshot = Snapshot({"a": Row("a", 2, {"id": "a"}, ("a",), True)}, [], [], [], {}, TZ, {"id": 0}, 2, 5)
    assert plan_sync(s, snapshot, {}, {}).pulls == {}


def test_duplicates_excluded_from_every_bucket():
    r = row("d", 2, name="sheet")
    old = row("d", 2, name="old")
    snapshot = snap(r, row("e", 5), duplicates={"d": [2, 6], "x": [7, 8], "y": [9, 10]})
    records = {"d": rec("d", name="app"), "x": rec("x")}
    states = {"d": state_for(old), "x": RowState("x", 7, "h"), "y": RowState("y", 9, "h")}
    p = plan(snapshot, records, states)
    assert p.creates == {} and p.updates == {} and p.deletes == {} and p.drift == {}
    assert p.missing_in_sheet == [] and "d" not in p.pulls


def test_orphans_and_snapshot_passed_through():
    snapshot = snap(orphans=[7, 9])
    p = plan(snapshot, {})
    assert p.orphans == [7, 9]
    assert p.snapshot is snapshot
