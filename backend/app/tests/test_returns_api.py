"""归还干跑/提交端到端不变量：

- 干跑不改集合（分栏、顶细条、借还记录一律不动）；
- 提交成功后可借栏、在借栏、借还记录是同一个世界；
- 名单漂移（同物被先归还又借出通过、批里混进非在借笔）→ 整单 409，三处回提交前；
- 二次提交同一批次 → 原样回放，已还的笔不再写第二遍。
"""
import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    with TestClient(app) as c:
        yield c


def _board(c):
    return c.get("/api/board").json()


def _loans(c):
    return c.get("/api/loans").json()


def _loan_row(lid):
    c = connect()
    row = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    c.close()
    return row


def _batch_status(bid):
    c = connect()
    row = c.execute("SELECT status FROM return_batches WHERE id=?", (bid,)).fetchone()
    c.close()
    return row["status"]


def _dry_run(c, ids):
    r = c.post("/api/returns/dry-run", json={"loan_ids": ids})
    assert r.status_code == 200
    return r.json()


def test_dry_run_keeps_collections_untouched(client):
    before_board = _board(client)
    before_loans = _loans(client)
    body = _dry_run(client, [1])
    assert body["titles"] == ["已外借样例"]
    assert body["counts_after"] == {"available": 4, "active": 0, "overdue": 0}
    assert body["stale_meta"]["commit_ignores_stale"] is False
    # 干跑只写 preview 批次：分栏、顶细条、借还记录与干跑前完全一致
    assert _board(client) == before_board
    assert _loans(client) == before_loans
    assert _batch_status(body["batch_id"]) == "preview"


def test_commit_success_aligns_board_and_records(client):
    bid = _dry_run(client, [1])["batch_id"]
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 200
    assert r.json()["replay"] is False
    board = _board(client)
    assert board["counts"] == {"available": 4, "active": 0, "overdue": 0}
    assert "已外借样例" in [i["title"] for i in board["available"]]
    loans = _loans(client)
    assert loans["active"] == [] and loans["overdue"] == []
    assert [l["id"] for l in loans["returned"]] == [1]
    assert loans["returned"][0]["returned_at"]
    assert _batch_status(bid) == "committed"


def test_stale_batch_rejected_and_nothing_moves(client):
    """干跑后同物先归还、邻居又借出通过（新 active），旧批次提交必须整单 409。"""
    bid = _dry_run(client, [1])["batch_id"]
    client.post("/api/loans/1/return")
    first_returned_at = _loan_row(1)["returned_at"]
    new_loan = client.post(
        "/api/items/4/lend", json={"borrower": "邻居乙", "due_date": "2026-12-31"}
    ).json()["loan_id"]
    before_board = _board(client)
    before_loans = _loans(client)

    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409
    assert r.json()["detail"] == "stale_batch"

    # 三处回提交前：分栏/顶细条与借还记录和提交前一致
    assert _board(client) == before_board
    assert _loans(client) == before_loans
    # 可借栏没有加出这一件，新借用人那笔还在在借栏、没被改 returned
    assert all(i["id"] != 4 for i in _board(client)["available"])
    assert new_loan in [l["id"] for l in _loans(client)["active"]]
    # 旧笔保持已还，returned_at 未被重写
    assert _loan_row(1)["status"] == "returned"
    assert _loan_row(1)["returned_at"] == first_returned_at
    # 批次保持 preview，可重新干跑后再提交
    assert _batch_status(bid) == "preview"


def test_non_active_loan_not_taken_along(client):
    """批里混进一笔已被批外归还的笔 → 整单失败，其余在借笔留在原处。"""
    client.post("/api/items", json={"title": "冲击钻", "owner": "老周"})
    lid2 = client.post(
        "/api/items/5/lend", json={"borrower": "邻居丙", "due_date": "2026-11-01"}
    ).json()["loan_id"]
    bid = _dry_run(client, [1, lid2])["batch_id"]
    client.post(f"/api/loans/{lid2}/return")

    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409

    loans = _loans(client)
    assert 1 in [l["id"] for l in loans["overdue"]]  # 在借笔未被带走
    assert lid2 in [l["id"] for l in loans["returned"]]  # 批外归还的那笔保持已还
    assert all(i["id"] != 4 for i in _board(client)["available"])
    assert _batch_status(bid) == "preview"


def test_second_commit_replays_without_rewrite(client):
    bid = _dry_run(client, [1])["batch_id"]
    client.post("/api/returns/commit", json={"batch_id": bid})
    first_returned_at = _loan_row(1)["returned_at"]

    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 200
    assert r.json()["replay"] is True

    assert _loan_row(1)["returned_at"] == first_returned_at  # 已还的笔不再写第二遍
    assert _board(client)["counts"] == {"available": 4, "active": 0, "overdue": 0}


def test_dry_run_rejects_stale_selection(client):
    client.post("/api/loans/1/return")
    r = client.post("/api/returns/dry-run", json={"loan_ids": [1]})
    assert r.status_code == 409


def test_commit_unknown_batch_404(client):
    r = client.post("/api/returns/commit", json={"batch_id": "no-such-batch"})
    assert r.status_code == 404
