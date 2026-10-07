"""归还闸引擎层：提交拍板必须吃真实漂移名单，与干跑同一道闸。"""
from app.engines.borrow_rules import commit_decision, stale_loan_ids
from app.engines import commit_bypass as cb


def test_commit_decision_sees_real_stale_list():
    loans_by_id = {7: {"id": 7, "status": "returned"}}
    stale = stale_loan_ids(loans_by_id, [7])
    decision = cb.commit_stale_decision("preview", stale, commit_decision)
    assert decision["action"] == "reject"
    assert decision["stale"] == [7]


def test_commit_decision_flags_missing_loan_as_stale():
    stale = stale_loan_ids({}, [9])
    decision = cb.commit_stale_decision("preview", stale, commit_decision)
    assert decision["action"] == "reject"
    assert decision["stale"] == [9]


def test_commit_decision_replays_committed_batch():
    assert cb.commit_stale_decision("committed", [], commit_decision) == {"action": "replay"}


def test_commit_decision_commits_clean_batch():
    assert cb.commit_stale_decision("preview", [], commit_decision) == {"action": "commit"}


def test_preview_note_states_commit_enforces_same_gate():
    note = cb.preview_stale_note([])
    assert note == {"stale_count": 0, "commit_ignores_stale": False}
