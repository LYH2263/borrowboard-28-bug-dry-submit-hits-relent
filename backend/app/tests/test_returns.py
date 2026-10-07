"""干跑/提交一致性回归：
干跑不改集合；提交成功三处同一世界；提交失败三处回提交前；
干跑后同物又借出→整单拒且新笔不丢；二次提交回放不重写；未在借笔不被带走；叠单不留残局。"""
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_TIMEOUT", "2")  # 叠单用例不必等满默认 5 秒
    from app.main import app
    with TestClient(app) as c:
        yield c


def _add_item(client, title="电钻", owner="老周"):
    r = client.post("/api/items", json={"title": title, "owner": owner})
    assert r.status_code == 200
    return r.json()["id"]


def _lend(client, iid, borrower="邻居甲", due="2026-12-31"):
    r = client.post(f"/api/items/{iid}/lend", json={"borrower": borrower, "due_date": due})
    assert r.status_code == 200, r.text
    return r.json()["loan_id"]


def _raw_client_db(client):
    from app.db import db_path
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


def test_dry_run_does_not_change_collections(client):
    iid = _add_item(client)
    lid = _lend(client, iid)
    before = client.get("/api/board").json()
    r = client.post("/api/returns/dry-run", json={"loan_ids": [lid]})
    assert r.status_code == 200
    after = client.get("/api/board").json()
    # 分栏集合与顶细条不动
    assert after == before
    assert {"id": iid} not in [{"id": i["id"]} for i in after["available"]]
    assert lid in [l["id"] for l in after["active"]]
    # 干跑只列出将回可借栏的 title 与提交后计数
    assert r.json()["titles"] == ["电钻"]
    assert r.json()["counts_after"]["available"] == before["counts"]["available"] + 1
    # 库中仍在借
    c = _raw_client_db(client)
    assert c.execute("SELECT status FROM loans WHERE id=?", (lid,)).fetchone()["status"] == "active"
    assert c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"] == "on_loan"
    c.close()


def test_commit_success_makes_three_views_one_world(client):
    iid = _add_item(client)
    lid = _lend(client, iid, due="2026-12-31")
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [lid]}).json()["batch_id"]
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 200 and r.json()["replay"] is False

    board = client.get("/api/board").json()
    loans = client.get("/api/loans").json()
    # 可借栏加出这一件
    assert iid in [i["id"] for i in board["available"]]
    # 在借栏不再挂着
    assert lid not in [l["id"] for l in board["active"] + board["overdue"]]
    # 借还记录里这笔在已还，且带 returned_at
    rec = next(l for l in loans["returned"] if l["id"] == lid)
    assert rec["status"] == "returned" and rec["returned_at"]
    # 顶细条：可借+1、在借-1，对得上分栏
    assert board["counts"] == {
        "available": len(board["available"]),
        "active": len(board["active"]),
        "overdue": len(board["overdue"]),
    }


def test_commit_rejected_when_item_relent_after_dry_run(client):
    """坏现象复现路径：干跑后同物被还并再借给邻居乙（已有新 active），旧干跑名单提交必须整单失败。"""
    iid = _add_item(client)
    lid_a = _lend(client, iid, borrower="邻居甲")
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [lid_a]}).json()["batch_id"]

    # 旧笔归还 + 邻居又借出通过同一物
    assert client.post(f"/api/loans/{lid_a}/return").status_code == 200
    lid_b = _lend(client, iid, borrower="邻居乙")

    before = client.get("/api/board").json()
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409  # stale_batch，整单未动
    after = client.get("/api/board").json()
    assert after == before  # 三处回提交前

    loans = client.get("/api/loans").json()
    # 新借用人那笔既不消失也不被改成 returned
    rec_b = next(l for l in (loans["active"] + loans["overdue"]) if l["id"] == lid_b)
    assert rec_b["borrower"] == "邻居乙" and rec_b["status"] == "active"
    # 可借栏没有凭空加出这一件（可借已加、在借还挂着的残局不得留下）
    assert iid not in [i["id"] for i in after["available"]]
    c = _raw_client_db(client)
    assert c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"] == "on_loan"
    assert c.execute("SELECT status FROM return_batches WHERE id=?", (bid,)).fetchone()["status"] == "preview"
    c.close()


def test_non_active_loan_is_not_taken_and_not_rewritten(client):
    iid = _add_item(client)
    lid = _lend(client, iid)
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [lid]}).json()["batch_id"]
    # 干跑后这笔被单笔归还
    assert client.post(f"/api/loans/{lid}/return").status_code == 200
    c = _raw_client_db(client)
    returned_at_first = c.execute("SELECT returned_at FROM loans WHERE id=?", (lid,)).fetchone()["returned_at"]
    c.close()

    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409
    c = _raw_client_db(client)
    row = c.execute("SELECT status, returned_at FROM loans WHERE id=?", (lid,)).fetchone()
    assert row["status"] == "returned"
    assert row["returned_at"] == returned_at_first  # 未在借笔不被再写一遍
    c.close()


def test_double_commit_replays_without_rewriting(client):
    iid = _add_item(client)
    lid = _lend(client, iid)
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [lid]}).json()["batch_id"]
    first = client.post("/api/returns/commit", json={"batch_id": bid})
    assert first.status_code == 200 and first.json()["replay"] is False
    committed_at_first = _raw_client_db(client).execute(
        "SELECT committed_at FROM return_batches WHERE id=?", (bid,)).fetchone()["committed_at"]

    second = client.post("/api/returns/commit", json={"batch_id": bid})
    assert second.status_code == 200 and second.json()["replay"] is True
    c = _raw_client_db(client)
    assert c.execute("SELECT committed_at FROM return_batches WHERE id=?", (bid,)).fetchone()[
        "committed_at"] == committed_at_first
    assert c.execute("SELECT COUNT(*) n FROM loans WHERE id=? AND status='returned'", (lid,)).fetchone()["n"] == 1
    c.close()
    # 可借栏也只有这一件、没有重复加
    board = client.get("/api/board").json()
    assert [i["id"] for i in board["available"]].count(iid) == 1


def test_dry_run_rejects_non_active_selection(client):
    iid = _add_item(client)
    lid = _lend(client, iid)
    client.post(f"/api/loans/{lid}/return")
    assert client.post("/api/returns/dry-run", json={"loan_ids": [lid]}).status_code == 409
    assert client.post("/api/returns/dry-run", json={"loan_ids": []}).status_code == 400


def test_overlapping_writer_leaves_no_residue(client):
    """逾期扫/单笔归还与批量提交叠单：写锁串行化。持锁方先把这笔归还落库，批量提交等到锁后在自己
    事务内重查名单→已漂移→整单拒，绝不重复写、不留半局。"""
    iid = _add_item(client)
    lid = _lend(client, iid, borrower="邻居甲", due="2020-01-01")  # 逾期笔
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [lid]}).json()["batch_id"]

    winner_ts = "2026-01-01T00:00:00+00:00"
    lock_held = threading.Event()

    def winner_returns_first():
        # 模拟逾期扫/单笔归还先抢到写锁；拿到锁后发信号，再持有片刻后落库
        c = _raw_client_db(client)
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        lock_held.set()
        time.sleep(0.5)
        c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=? AND status='active'",
                  (winner_ts, lid))
        c.execute("UPDATE items SET status='available' WHERE id=? AND NOT EXISTS ("
                  "SELECT 1 FROM loans WHERE item_id=? AND status='active')", (iid, iid))
        c.execute("COMMIT")
        c.close()

    t = threading.Thread(target=winner_returns_first)
    t.start()
    assert lock_held.wait(timeout=5)  # 确保胜方已持写锁，再让批量提交去等锁
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    t.join()
    # 等到锁后重查：笔已漂移 → 整单失败
    assert r.status_code == 409

    # 最终世界 = 胜方那一次归还，恰好一次：可借栏有此物，借还记录在已还，批次仍 preview 未被写第二遍
    board = client.get("/api/board").json()
    loans = client.get("/api/loans").json()
    assert iid in [i["id"] for i in board["available"]]
    assert lid not in [l["id"] for l in board["active"] + board["overdue"]]
    rec = next(l for l in loans["returned"] if l["id"] == lid)
    assert rec["returned_at"] == winner_ts  # 不是批量提交写的时间戳
    c = _raw_client_db(client)
    assert c.execute("SELECT COUNT(*) n FROM loans WHERE id=? AND status='returned'", (lid,)).fetchone()["n"] == 1
    assert c.execute("SELECT status FROM return_batches WHERE id=?", (bid,)).fetchone()["status"] == "preview"
    c.close()
