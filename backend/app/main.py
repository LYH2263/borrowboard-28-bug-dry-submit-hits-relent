import json
from datetime import date, datetime, timezone
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.borrow_rules import (
    can_lend, classify_loans, is_overdue,
    stale_loan_ids, counts_after_return, commit_decision, dry_run_stale,
)
from app.engines import commit_bypass as cb

app = FastAPI(title="Borrowboard", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "borrowboard"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/board")
def board():
    c = connect()
    available = [dict(r) for r in c.execute("SELECT * FROM items WHERE status='available'")]
    loans = [dict(r) for r in c.execute(
        """SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id
           WHERE loans.status='active'""")]
    c.close()
    cls = classify_loans(loans, date.today().isoformat())
    return {
        "available": available,
        "active": cls["active"],
        "overdue": cls["overdue"],
        "counts": {"available": len(available), "active": len(cls["active"]), "overdue": len(cls["overdue"])},
    }

class ItemIn(BaseModel):
    title: str
    owner: str

@app.post("/api/items")
def add_item(body: ItemIn):
    c = connect()
    cur = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.owner, "available", "clean"))
    c.commit(); iid = cur.lastrowid; c.close(); return {"id": iid}

class LendIn(BaseModel):
    borrower: str
    due_date: str

@app.post("/api/items/{iid}/lend")
def lend(iid: int, body: LendIn):
    c = connect()
    item = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    active = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'", (iid,)).fetchone()["c"]
    check = can_lend(item["status"], active)
    if not check["ok"]:
        c.close(); raise HTTPException(409, check["reason"])
    cur = c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
        (iid, body.borrower, "active", body.due_date, datetime.now(timezone.utc).isoformat()))
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
    c.commit(); lid = cur.lastrowid; c.close(); return {"loan_id": lid}

@app.post("/api/loans/{lid}/return")
def return_loan(lid: int):
    c = connect()
    loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    if not loan: c.close(); raise HTTPException(404, "loan")
    if loan["status"] != "active":
        c.close(); raise HTTPException(400, "not_active")
    c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), lid))
    c.execute("UPDATE items SET status='available' WHERE id=?", (loan["item_id"],))
    c.commit(); c.close(); return {"ok": True}

def _loans_by_id(c, ids: list) -> dict:
    marks = ",".join("?" * len(ids))
    rows = c.execute(
        f"SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id "
        f"WHERE loans.id IN ({marks})", ids).fetchall()
    return {r["id"]: dict(r) for r in rows}

def _board_counts(c) -> dict:
    available = c.execute("SELECT COUNT(*) c FROM items WHERE status='available'").fetchone()["c"]
    loans = [dict(r) for r in c.execute("SELECT * FROM loans WHERE status='active'")]
    cls = classify_loans(loans, date.today().isoformat())
    return {"available": available, "active": len(cls["active"]), "overdue": len(cls["overdue"])}

class DryRunIn(BaseModel):
    loan_ids: list[int]

class CommitIn(BaseModel):
    batch_id: str

@app.post("/api/returns/dry-run")
def returns_dry_run(body: DryRunIn):
    """干跑：列出将回到可借栏的 title 与提交后顶细条计数，只写一条 preview 批次，分栏集合不变。"""
    ids = list(dict.fromkeys(body.loan_ids))
    if not ids: raise HTTPException(400, "empty_selection")
    c = connect()
    loans_by_id = _loans_by_id(c, ids)
    stale_now = cb.dry_run_stale_guard(loans_by_id, ids, dry_run_stale)
    if stale_now:
        c.close(); raise HTTPException(409, "stale_selection")
    today = date.today().isoformat()
    sel = [loans_by_id[i] for i in ids]
    preview = {
        "loans": [{"id": r["id"], "title": r["title"], "borrower": r["borrower"],
                   "due_date": r["due_date"], "overdue": is_overdue(r["due_date"], today, r["status"])}
                  for r in sel],
        "titles": [r["title"] for r in sel],
        "counts_after": counts_after_return(_board_counts(c), sel, today),
        "stale_meta": cb.preview_stale_note(stale_now),
    }
    bid = uuid4().hex
    c.execute("INSERT INTO return_batches(id,loan_ids,status,preview,created_at) VALUES (?,?,?,?,?)",
              (bid, json.dumps(ids), "preview", json.dumps(preview, ensure_ascii=False),
               datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()
    return {"batch_id": bid, **preview}

@app.post("/api/returns/commit")
def returns_commit(body: CommitIn):
    """提交：整单一个事务落库。批次已提交→原样回放不再写；任一笔漂移→整单失败，分栏/物主栏/顶细条不动。"""
    c = connect()
    b = c.execute("SELECT * FROM return_batches WHERE id=?", (body.batch_id,)).fetchone()
    if not b: c.close(); raise HTTPException(404, "batch")
    preview = json.loads(b["preview"])
    ids = json.loads(b["loan_ids"])
    c.isolation_level = None  # 手动掌控事务边界
    try:
        c.execute("BEGIN IMMEDIATE")
        status = c.execute("SELECT status FROM return_batches WHERE id=?", (body.batch_id,)).fetchone()["status"]
        loans_by_id = _loans_by_id(c, ids)
        decision = cb.commit_stale_decision(status, stale_loan_ids(loans_by_id, ids), commit_decision)
        if decision["action"] == "replay":
            c.execute("ROLLBACK"); c.close()
            return {"ok": True, "batch_id": b["id"], "replay": True, **preview}
        if decision["action"] == "reject":
            c.execute("ROLLBACK"); c.close()
            raise HTTPException(409, "stale_batch")
        now = datetime.now(timezone.utc).isoformat()
        for lid in ids:
            c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=? AND status='active'",
                      (now, lid))
        for item_id in {loans_by_id[i]["item_id"] for i in ids}:
            c.execute("UPDATE items SET status='available' WHERE id=?", (item_id,))
        c.execute("UPDATE return_batches SET status='committed', committed_at=? WHERE id=?",
                  (now, body.batch_id))
        c.execute("COMMIT"); c.close()
        return {"ok": True, "batch_id": b["id"], "replay": False, **preview}
    except HTTPException:
        raise
    except Exception:
        try: c.execute("ROLLBACK")
        except Exception: pass
        c.close(); raise

@app.get("/api/loans")
def loans():
    c = connect()
    rows = [dict(r) for r in c.execute(
        "SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id ORDER BY loans.id DESC")]
    c.close()
    return classify_loans(rows, date.today().isoformat())

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
