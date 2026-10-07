"""归还闸：干跑与提交共用同一份漂移判定，提交不绕过、不重算。"""

def commit_stale_decision(batch_status: str, stale: list, decide_fn):
    """提交瞬间的拍板吃事务内重算出的真实漂移名单：任一漂移→整单失败。"""
    return decide_fn(batch_status, stale)

def dry_run_stale_guard(loans_by_id: dict, loan_ids: list, guard_fn) -> list:
    return guard_fn(loans_by_id, loan_ids)

def preview_stale_note(stale: list) -> dict:
    return {"stale_count": len(stale), "commit_ignores_stale": False}
