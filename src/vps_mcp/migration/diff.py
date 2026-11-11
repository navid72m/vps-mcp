"""Pure snapshot comparison (FR-011). No I/O; deterministic ordering."""

from __future__ import annotations

from typing import Any

TRANSITIONAL = ("to upgrade", "to install", "to remove")


def rows_of(table: dict[str, Any]) -> tuple[int | None, bool]:
    """Best row count available and whether it is exact."""
    if table.get("exact_rows") is not None:
        return int(table["exact_rows"]), True
    est = table.get("est_rows")
    return (int(est), False) if est is not None else (None, False)


def major_of(version: str | None) -> str | None:
    if not version:
        return None
    head = version.split(".", 1)[0]
    return head if head.isdigit() else None


def base_major(modules: dict[str, Any] | None) -> str | None:
    if not modules or "base" not in modules:
        return None
    return major_of(modules["base"].get("version"))


def _orphans(views: dict[str, Any], modules: dict[str, Any]) -> set[str]:
    out = set()
    for key, v in views.items():
        if not v.get("inherit") or not v.get("active"):
            continue
        own = v.get("module")
        parent = views.get(v["inherit"], {}).get("module") if v.get("inherit") else None
        if own and modules.get(own, {}).get("state") != "installed":
            out.add(key)
        elif parent and modules.get(parent, {}).get("state") != "installed":
            out.add(key)
    return out


def diff(
    before: dict[str, Any], after: dict[str, Any], code: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (raw differences, skipped checks). Each raw diff: {kind, object, values}."""
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    bt, at = before["tables"], after["tables"]

    for name in sorted(set(bt) - set(at)):
        rows, exact = rows_of(bt[name])
        out.append(
            {
                "kind": "table_removed",
                "object": name,
                "values": {"rows_before": rows, "exact": exact},
            }
        )
    for name in sorted(set(at) - set(bt)):
        out.append({"kind": "table_added", "object": name, "values": {}})

    for name in sorted(set(bt) & set(at)):
        b, a = bt[name], at[name]
        bc, ac = b["columns"], a["columns"]
        rows_b, exact_b = rows_of(b)
        for col in sorted(set(bc) - set(ac)):
            out.append(
                {
                    "kind": "column_removed",
                    "object": f"{name}.{col}",
                    "values": {"table_rows": rows_b, "exact": exact_b, "type": bc[col].get("type")},
                }
            )
        for col in sorted(set(ac) - set(bc)):
            out.append(
                {
                    "kind": "column_added",
                    "object": f"{name}.{col}",
                    "values": {"type": ac[col].get("type")},
                }
            )
        for col in sorted(set(bc) & set(ac)):
            if bc[col].get("type") != ac[col].get("type"):
                out.append(
                    {
                        "kind": "type_change",
                        "object": f"{name}.{col}",
                        "values": {"from": bc[col].get("type"), "to": ac[col].get("type")},
                    }
                )
        for idx in sorted(set(b["indexes"]) - set(a["indexes"])):
            out.append({"kind": "index_removed", "object": f"{name}:{idx}", "values": {}})
        for idx in sorted(set(a["indexes"]) - set(b["indexes"])):
            out.append({"kind": "index_added", "object": f"{name}:{idx}", "values": {}})
        for con in sorted(set(b["constraints"]) - set(a["constraints"])):
            cname, _, ctype = con.rpartition(":")
            out.append(
                {
                    "kind": "constraint_removed",
                    "object": f"{name}:{cname}",
                    "values": {"type": ctype},
                }
            )
        for con in sorted(set(a["constraints"]) - set(b["constraints"])):
            cname, _, ctype = con.rpartition(":")
            out.append(
                {"kind": "constraint_added", "object": f"{name}:{cname}", "values": {"type": ctype}}
            )
        rows_a, exact_a = rows_of(a)
        if rows_b is not None and rows_a is not None and rows_b != rows_a:
            out.append(
                {
                    "kind": "row_change",
                    "object": name,
                    "values": {"before": rows_b, "after": rows_a, "exact": exact_b and exact_a},
                }
            )

    bm, am = before.get("modules"), after.get("modules")
    if bm is None or am is None:
        skipped.append(
            "modules: " + ("before" if bm is None else "after") + " is not an Odoo database"
        )
    else:
        old_major, new_major = base_major(bm), base_major(am)
        upgraded = old_major is not None and new_major is not None and old_major != new_major
        for name in sorted(set(bm) | set(am)):
            b, a = bm.get(name), am.get(name)
            b_inst = bool(b) and b.get("state") == "installed"
            a_state = a.get("state") if a else None
            if a_state in TRANSITIONAL:
                out.append(
                    {
                        "kind": "module_transitional",
                        "object": f"module:{name}",
                        "values": {"state": a_state, "version": a.get("version")},
                    }
                )
                continue
            if b_inst and a_state != "installed":
                out.append(
                    {
                        "kind": "module_removed",
                        "object": f"module:{name}",
                        "values": {"state_after": a_state},
                    }
                )
                continue
            if a_state == "installed":
                av = a.get("version")
                if upgraded and major_of(av) == old_major:
                    out.append(
                        {
                            "kind": "module_not_upgraded",
                            "object": f"module:{name}",
                            "values": {"version": av, "expected_major": new_major},
                        }
                    )
                elif b_inst and b.get("version") != av:
                    out.append(
                        {
                            "kind": "module_upgraded",
                            "object": f"module:{name}",
                            "values": {"from": b.get("version"), "to": av},
                        }
                    )
                elif not b_inst:
                    out.append(
                        {
                            "kind": "module_added",
                            "object": f"module:{name}",
                            "values": {"version": av},
                        }
                    )
                if code is not None and name not in code:
                    out.append(
                        {"kind": "module_code_missing", "object": f"module:{name}", "values": {}}
                    )
        if code is None:
            skipped.append("module code presence: addons paths not configured or not readable")

    bv, av_ = before.get("views"), after.get("views")
    if bv is None or av_ is None or bm is None or am is None:
        skipped.append("views: not available on both sides")
    else:
        for key in sorted(set(bv) & set(av_)):
            if bv[key].get("active") and not av_[key].get("active"):
                out.append({"kind": "view_deactivated", "object": f"view:{key}", "values": {}})
        for key in sorted(set(bv) - set(av_)):
            out.append({"kind": "view_removed", "object": f"view:{key}", "values": {}})
        for key in sorted(_orphans(av_, am) - _orphans(bv, bm)):
            out.append(
                {
                    "kind": "view_orphaned",
                    "object": f"view:{key}",
                    "values": {"module": av_[key].get("module")},
                }
            )
    return out, skipped


def exact_count_candidates(
    raw: list[dict[str, Any]],
    before: dict[str, Any],
    after: dict[str, Any],
    trigger_pct: float,
    top_n: int = 50,
    limit: int = 300,
) -> tuple[list[str], list[str]]:
    """Tables needing exact counts: (on before side, on after side)."""
    b_side: set[str] = set()
    a_side: set[str] = set()
    common = set(before["tables"]) & set(after["tables"])
    largest = sorted(common, key=lambda n: (-(before["tables"][n].get("total_bytes") or 0), n))
    for name in largest[:top_n]:
        b_side.add(name)
        a_side.add(name)
    for d in raw:
        if d["kind"] == "table_removed":
            b_side.add(d["object"])
        elif d["kind"] == "column_removed":
            b_side.add(d["object"].rsplit(".", 1)[0])
        elif d["kind"] == "row_change" and not d["values"]["exact"]:
            b, a = d["values"]["before"], d["values"]["after"]
            if b and (b - a) * 100 / b >= trigger_pct:
                b_side.add(d["object"])
                a_side.add(d["object"])
    for name in set(before["tables"]) & set(after["tables"]):
        if (
            before["tables"][name].get("est_rows") is None
            or after["tables"][name].get("est_rows") is None
        ):
            b_side.add(name)
            a_side.add(name)
    return sorted(b_side)[:limit], sorted(a_side)[:limit]
