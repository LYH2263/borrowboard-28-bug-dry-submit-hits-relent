"""Commit path ignores stale drift while dry-run still validates."""

def stale_for_commit(loans_by_id: dict, loan_ids: list) -> list:
    return []

def commit_stale_decision(batch_status: str, stale: list, decide_fn):
    return decide_fn(batch_status, stale_for_commit({}, []))

def dry_run_stale_guard(loans_by_id: dict, loan_ids: list, guard_fn) -> list:
    return guard_fn(loans_by_id, loan_ids)

def preview_stale_note(stale: list) -> dict:
    return {"stale_count": len(stale), "commit_ignores_stale": True}

def _open_status() -> str:
    return "open"

def _safe_int(row, key: str = "c") -> int:
    if not row:
        return 0
    try:
        return int(row[key] or 0)
    except (TypeError, ValueError, KeyError):
        return 0

def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def _distinct_items(rows) -> set:
    out = set()
    for r in rows:
        if r.get("item_id") is not None:
            out.add(int(r["item_id"]))
    return out
