"""pytest fixtures —— 每个测试会话使用独立 DB，避免污染演示数据"""
import itertools
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设置，否则 db.py 会用默认路径
os.environ.setdefault("DSAR_DB", str(ROOT / "data" / "test_dsar.db"))
os.environ.setdefault("SKIP_SEED", "1")


def _reset_db():
    from app.models.db import DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture(scope="session")
def seeded_db():
    """整个测试会话共用一个已 seed 的库"""
    from app.models.db import DB_PATH  # noqa: F401
    from app.models.seed import seed_all
    _reset_db()
    counts = seed_all()
    yield counts
    _reset_db()


@pytest.fixture(scope="session")
def client(seeded_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client):
    """
    供 API 测试使用的客户端包装。

    提供 new_request() 帮助函数：每个测试建自己的 requester + request，
    避免共享 session 级 DB 时互相污染状态。
    """
    counter = itertools.count(1)

    class Api:
        def __init__(self, c):
            self.c = c

        def new_requester(self, jurisdiction="EU", **kw):
            n = next(counter)
            rid = kw.get("requester_id") or f"REQ-T{n:04d}"
            email = kw.get("email") or f"{rid.lower()}@test.invalid"
            from app.models.db import db_connection
            with db_connection() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO requesters
                       (requester_id, full_name, email, phone, subject_type,
                        jurisdiction, account_id, locale)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (rid, kw.get("full_name", f"Test {rid}"), email,
                     kw.get("phone", "+8613800009999"),
                     kw.get("subject_type", "CUSTOMER"), jurisdiction,
                     kw.get("account_id"), kw.get("locale", "en-US")),
                )
                conn.commit()
            return rid

        def new_request(self, request_type="ACCESS", jurisdiction="EU",
                        received_date=None, requester_id=None, **kw):
            requester_id = requester_id or self.new_requester(jurisdiction)
            body = {"requester_id": requester_id, "request_type": request_type,
                    "jurisdiction": jurisdiction, "channel": kw.get("channel", "WEB_FORM"),
                    "actor": "pytest"}
            if received_date:
                body["received_date"] = received_date
            resp = self.c.post("/api/requests", json=body)
            assert resp.status_code == 200, resp.text
            return resp.json()["request"]

        def verify_ok(self, request_id, method="EMAIL_OTP", signals=None, actor="pytest"):
            """把一个请求推到 identity VERIFIED（走真实 API，不直接改库）"""
            body = {"method": method, "actor": actor}
            if signals:
                body["risk_signals"] = signals
            resp = self.c.post(f"/api/requests/{request_id}/verify", json=body)
            assert resp.status_code == 200, resp.text
            vid = resp.json()["verification"]["verification_id"]
            sub = self.c.post(f"/api/verify/{vid}/submit",
                              json={"evidence_ref": "PYTEST-EV-1", "actor": actor})
            assert sub.status_code == 200, sub.text
            if sub.json()["verification"]["status"] != "VERIFIED":
                appr = self.c.post(f"/api/verify/{vid}/approve",
                                   json={"note": "pytest 人工复核：证据与档案一致",
                                         "verified_by": "privacy-officer-pytest"})
                assert appr.status_code == 200, appr.text
            return vid

        def resolve_all_tasks(self, request_id, status="COMPLETED", records=2):
            """
            把请求下所有未收敛任务推到终态。

            必须走 PENDING → RUNNING → 终态：任务状态机不允许 PENDING 直接跳 COMPLETED
            （检索动作必须先真正开始），这正是防「假完成」的约束。
            """
            tasks = self.c.get(f"/api/requests/{request_id}/tasks").json()["tasks"]
            for t in tasks:
                if t["status"] != "PENDING":
                    continue
                if status == "EXCLUDED":
                    body = {"status": "EXCLUDED", "actor": "pytest", "record_count": 0,
                            "excluded_reason": "Art. 17(3)(b) 测试用留存义务"}
                else:
                    self.c.patch(f"/api/tasks/{t['task_id']}",
                                 json={"status": "RUNNING", "actor": "pytest"})
                    body = {"status": status, "actor": "pytest", "record_count": records}
                self.c.patch(f"/api/tasks/{t['task_id']}", json=body)
            return tasks

    return Api(client)
