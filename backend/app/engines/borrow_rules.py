"""One active loan per item + overdue detection."""

def can_lend(item_status: str, active_loans: int) -> dict:
    if item_status != "available":
        return {"ok": False, "reason": "item_not_available"}
    if active_loans > 0:
        return {"ok": False, "reason": "already_on_loan"}
    return {"ok": True, "reason": ""}

def is_overdue(due_date: str, today: str, loan_status: str) -> bool:
    if loan_status != "active":
        return False
    return bool(due_date) and due_date < today

def classify_loans(loans: list[dict], today: str) -> dict:
    active, overdue, returned = [], [], []
    for L in loans:
        st = L.get("status")
        if st == "returned":
            returned.append(L)
        elif is_overdue(L.get("due_date"), today, st):
            overdue.append({**L, "overdue": True})
        elif st == "active":
            active.append({**L, "overdue": False})
    return {"active": active, "overdue": overdue, "returned": returned}

def stale_loan_ids(loans_by_id: dict, loan_ids: list) -> list:
    """批次内已漂移的笔号：不存在或不再在借。干跑名单是契约，含任一漂移笔即整单失败。"""
    return [i for i in loan_ids
            if loans_by_id.get(i) is None or loans_by_id[i].get("status") != "active"]

def counts_after_return(counts: dict, loans: list[dict], today: str) -> dict:
    """提交落库后顶细条应有的计数；仅供干跑预览，落库前任何集合不变。"""
    n_overdue = sum(1 for L in loans if is_overdue(L.get("due_date"), today, L.get("status")))
    item_ids = {L.get("item_id") for L in loans}
    return {
        "available": counts.get("available", 0) + len(item_ids),
        "active": counts.get("active", 0) - (len(loans) - n_overdue),
        "overdue": counts.get("overdue", 0) - n_overdue,
    }

def commit_decision(batch_status: str, stale: list) -> dict:
    """提交瞬间的拍板：已提交→原样回放（二次提交不重写）；有漂移→整单失败；否则整单落库。"""
    if batch_status == "committed":
        return {"action": "replay"}
    if stale:
        return {"action": "reject", "stale": stale}
    return {"action": "commit"}

def dry_run_stale(loans_by_id: dict, loan_ids: list) -> list:
    return stale_loan_ids(loans_by_id, loan_ids)
