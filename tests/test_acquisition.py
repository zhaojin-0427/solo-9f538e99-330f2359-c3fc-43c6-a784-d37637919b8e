"""采集 API 端到端测试：真实本地 HTTP 分页源 + 独立 SQLite。

覆盖：
- 正常分页拉取 -> 全页到齐发布新修订，旧供数过期；
- 断网（503）保留已确认页与游标，恢复后从游标续拉，不重取已确认页；
- 跨页版号不同 -> 拒绝发布、不产生修订；
- 拉取期间人工更正与供数仍可用，但首次发布检测到分类表变过即拒绝覆盖；
- 发布写入失败 -> 原分类和供数保持不变，重试成功；
- 发布幂等：重试返回同一修订，不再使后来生成的结果过期；
- 分页请求在途时人工更正不被写锁阻塞；
- 服务重启后用新连接续拉，已确认页不重取；
- 游标未前进、整版关系超 40 条等协议/校验异常拒绝发布。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import mock_source.server as mock
from app import acquisition as acq
from app import services
from app.database import connect, immediate_tx, init_db
from app.main import app, get_conn
from app.models import (
    AcquisitionCreate,
    BatchCreate,
    EdgeIn,
    ObservationIn,
    ResultCreate,
    TaxonomyCreate,
)
from mock_source.server import Handler, SourceState


# ---------- 分页源夹具 ----------

class ControlledState(SourceState):
    """可精确控制断流与换版的本地分页源状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_after_successes: int | None = None
        self.successes = 0
        self.version_switch_after: int | None = None
        self.page_calls = 0
        # 让第 N 次分页请求阻塞，便于在“拉取进行中”插入其他操作。
        self.block_on_call: int | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def page(self, cursor):
        if self.fail_after_successes is not None and self.successes >= self.fail_after_successes:
            raise RuntimeError("outage")
        if self.block_on_call is not None and self.page_calls + 1 == self.block_on_call:
            self.entered.set()
            assert self.release.wait(timeout=10), "测试未及时放行分页请求"
        data = super().page(cursor)
        self.successes += 1
        self.page_calls += 1
        if self.version_switch_after is not None and self.page_calls >= self.version_switch_after:
            data["version"] = f"{data['version']}-switched"
        return data


@pytest.fixture()
def source(monkeypatch):
    state = ControlledState()
    monkeypatch.setattr(mock, "STATE", state)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}/pages"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(prefix="eco_acq_"), "acq.db")
    conn = connect(path)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def client(db):
    def _override():
        yield db

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _batch(conn, names):
    return services.create_batch(
        conn,
        BatchCreate(
            observations=[
                ObservationIn(code=f"o{i}", name=n) for i, n in enumerate(names, 1)
            ]
        ),
    )


def _result(conn, batch_id, revision_id=None):
    return services.generate_result(
        conn, ResultCreate(batch_id=batch_id, revision_id=revision_id)
    )


def _make_ready_acquisition(db, source):
    """构造一个已拉完页、尚待发布的采集，便于单独验证发布语义。"""
    page = acq.fetch_source_page(source.url, None)
    with immediate_tx(db):
        cur = db.execute(
            "INSERT INTO acquisition(source_url, status, base_revision_id, version, next_cursor) "
            "VALUES (?, 'ready', ?, ?, NULL)",
            (source.url, services.latest_revision_id(db), page.version),
        )
        acquisition_id = cur.lastrowid
        db.execute(
            """
            INSERT INTO acquisition_page(
                acquisition_id, page_no, request_cursor, next_cursor, version,
                accepted_names, synonym_edges
            ) VALUES (?, 1, NULL, NULL, ?, ?, ?)
            """,
            (
                acquisition_id,
                page.version,
                json.dumps(page.accepted_names, ensure_ascii=False),
                json.dumps(
                    [e.model_dump() for e in page.synonym_edges], ensure_ascii=False
                ),
            ),
        )
    return acquisition_id


# ---------- 用例 ----------

def test_acquisition_publishes_revision_and_expires_old_results(db, client, source):
    bid = _batch(db, ["旧名甲", "分叉名", "环名x", "无人名", "菱形名", "接受名A"])
    rev1 = services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["接受名A", "接受名B"], synonym_edges=[])
    )
    old_result = _result(db, bid)
    assert services.get_result(db, old_result)["status"] == "active"

    r = client.post("/acquisitions", json={"source_url": source.url, "note": "首次采集"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed", body
    assert body["version"] == source.version
    assert body["pages_confirmed"] >= 2
    assert body["next_cursor"] is None
    assert body["published_revision_id"] == rev1 + 1

    rev2 = body["published_revision_id"]
    assert services.get_revision(db, rev2)["superseded_at"] is None
    assert services.get_revision(db, rev1)["superseded_at"] is not None
    assert services.get_result(db, old_result)["status"] == "expired"

    new_result = _result(db, bid)
    detail = client.get(f"/results/{new_result}").json()
    assert detail["revision_id"] == rev2
    by_code = {i["observation_code"]: i for i in detail["items"]}
    # 默认奇数版：分叉名只指向接受名A
    assert by_code["o2"]["item_status"] == "included"
    assert by_code["o2"]["accepted_name"] == "接受名A"

    # 查询接口能读到已确认页
    got = client.get(f"/acquisitions/{body['acquisition_id']}").json()
    assert got["pages_confirmed"] == len(got["pages"]) == body["pages_confirmed"]


def test_outage_keeps_confirmed_pages_and_resumes(db, client, source):
    source.page_size = 2
    source.fail_after_successes = 2  # 拉到第 2 页后断流

    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert body["status"] == "in_progress"
    assert body["pages_confirmed"] == 2
    assert body["error_code"] == "source_unavailable"
    assert body["pages"][1]["request_cursor"] == body["pages"][0]["next_cursor"]
    assert services.latest_revision_id(db) is None  # 未发布

    # 断网期间继续：不重取已确认页，游标/页数不变
    before_calls = source.page_calls
    again = client.post(f"/acquisitions/{body['acquisition_id']}/continue").json()
    assert again["status"] == "in_progress"
    assert again["pages_confirmed"] == 2
    assert again["error_code"] == "source_unavailable"
    assert source.page_calls == before_calls  # 请求在断流处即失败，未重取已确认页

    # 恢复后从游标续拉：只会请求第 3 页及以后
    source.fail_after_successes = None
    done = client.post(f"/acquisitions/{body['acquisition_id']}/continue").json()
    assert done["status"] == "completed"
    assert done["pages_confirmed"] > 2
    pages = done["pages"]
    # 前两页原样保留（confirmed_at 早于续拉），第 3 页从第 2 页游标开始
    assert pages[0]["request_cursor"] is None
    assert pages[1]["request_cursor"] == pages[0]["next_cursor"]
    assert pages[2]["request_cursor"] == pages[1]["next_cursor"]
    assert services.latest_revision_id(db) == done["published_revision_id"]


def test_version_mismatch_across_pages_rejected(db, client, source):
    source.page_size = 1
    source.version_switch_after = 2  # 第 2 次请求起换版号

    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert body["status"] == "rejected"
    assert body["error_code"] == "version_mismatch"
    assert body["pages_confirmed"] == 1
    assert services.latest_revision_id(db) is None  # 绝不拼版

    again = client.post(f"/acquisitions/{body['acquisition_id']}/continue").json()
    assert again["status"] == "rejected"
    assert services.latest_revision_id(db) is None

    # 换一个版号一致的新采集仍可正常发布
    source.version_switch_after = None
    ok = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert ok["status"] == "completed"


def test_manual_edits_during_pull_are_allowed_but_block_publication(db, client, source):
    bid = _batch(db, ["旧名甲"])
    rev0 = services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["接受名A"], synonym_edges=[])
    )
    source.page_size = 2
    source.fail_after_successes = 1
    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert body["status"] == "in_progress"
    assert body["pages_confirmed"] == 1

    # 拉取期间人工更正 + 生成供数，均正常
    rev1 = services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["接受名A", "接受名B"], synonym_edges=[])
    )
    manual_result = _result(db, bid)
    assert services.get_result(db, manual_result)["status"] == "active"

    # 恢复拉取，页能拉完，但首次发布前发现分类表被改过 -> 拒绝覆盖
    source.fail_after_successes = None
    done = client.post(f"/acquisitions/{body['acquisition_id']}/continue").json()
    assert done["status"] == "rejected"
    assert done["error_code"] == "taxonomy_changed"
    assert done["published_revision_id"] is None
    assert services.latest_revision_id(db) == rev1  # 人工修订未被覆盖
    assert services.get_result(db, manual_result)["status"] == "active"


def test_publish_rejects_when_taxonomy_changed_even_if_reverted(db, client, source):
    rev0 = services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["X"], synonym_edges=[])
    )
    acquisition_id = _make_ready_acquisition(db, source)
    # 开始后人工改一次、再改回完全相同的内容
    services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["X"], synonym_edges=[])
    )
    rev_now = services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["X"], synonym_edges=[])
    )
    assert rev_now == rev0 + 2

    body = client.post(f"/acquisitions/{acquisition_id}/continue").json()
    assert body["status"] == "rejected"
    assert body["error_code"] == "taxonomy_changed"
    assert services.latest_revision_id(db) == rev_now


def test_publish_failure_keeps_state_then_retry_is_idempotent(
    db, client, source, monkeypatch
):
    bid = _batch(db, ["旧名甲"])
    services.create_taxonomy_revision(
        db, TaxonomyCreate(accepted_names=["接受名A"], synonym_edges=[])
    )
    res0 = _result(db, bid)
    acquisition_id = _make_ready_acquisition(db, source)
    rev_before = services.latest_revision_id(db)

    def boom(*args, **kwargs):
        raise __import__("sqlite3").OperationalError("disk I/O error (模拟)")

    monkeypatch.setattr(acq.services, "insert_taxonomy_revision", boom)
    body = client.post(f"/acquisitions/{acquisition_id}/continue").json()
    assert body["status"] == "ready"  # 未完成，可重试
    assert body["error_code"] == "write_failed"

    # 原分类和供数保持
    assert services.latest_revision_id(db) == rev_before
    assert services.get_result(db, res0)["status"] == "active"

    monkeypatch.undo()
    done = client.post(f"/acquisitions/{acquisition_id}/continue").json()
    assert done["status"] == "completed"
    rev_new = done["published_revision_id"]
    assert services.latest_revision_id(db) == rev_new

    # 发布成功后生成的新供数
    res_new = _result(db, bid)
    assert services.get_result(db, res_new)["status"] == "active"

    # 再次继续已完成的采集：幂等，同一修订，不使新供数过期
    again = client.post(f"/acquisitions/{acquisition_id}/continue").json()
    assert again["status"] == "completed"
    assert again["published_revision_id"] == rev_new
    assert services.latest_revision_id(db) == rev_new
    assert services.get_result(db, res_new)["status"] == "active"


def test_acquisition_validation_and_404(db, client, source):
    assert client.get("/acquisitions/999").status_code == 404
    assert client.post("/acquisitions/999/continue").status_code == 404

    # 整版关系超过 40 条 -> 到齐后校验不通过，拒绝发布
    many_edges = [{"synonym": f"s{i}", "target": "A"} for i in range(41)]
    source.set_payload(
        {"version": "big", "accepted_names": ["A"], "synonym_edges": many_edges}
    )
    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert body["status"] == "rejected"
    assert body["error_code"] == "validation_error"
    assert services.latest_revision_id(db) is None

    # 非法 source_url -> 422
    assert client.post(
        "/acquisitions", json={"source_url": "ftp://nope"}
    ).status_code == 422


def test_source_unreachable_is_retryable(db, client):
    body = client.post(
        "/acquisitions", json={"source_url": "http://127.0.0.1:9/pages"}
    ).json()
    assert body["status"] == "in_progress"
    assert body["error_code"] == "source_unavailable"
    assert body["pages_confirmed"] == 0


def test_survives_process_restart_with_new_connection(db, client, source, monkeypatch):
    """模拟服务重启：关闭连接后用同一库文件重新连接，已确认页与游标仍在。"""
    source.page_size = 2
    source.fail_after_successes = 2
    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    acq_id = body["acquisition_id"]
    assert body["pages_confirmed"] == 2

    db_path = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()  # 模拟进程退出
    fresh = connect(db_path)
    init_db(fresh)
    app.dependency_overrides[get_conn] = lambda: (yield fresh)
    try:
        state = client.get(f"/acquisitions/{acq_id}").json()
        assert state["status"] == "in_progress"
        assert state["pages_confirmed"] == 2
        assert state["next_cursor"] == body["next_cursor"]

        source.fail_after_successes = None
        calls_before = source.page_calls
        done = client.post(f"/acquisitions/{acq_id}/continue").json()
        assert done["status"] == "completed"
        # 已确认的两页没有被重新请求
        assert source.page_calls - calls_before == done["pages_confirmed"] - 2
        assert done["pages"][0]["confirmed_at"] == body["pages"][0]["confirmed_at"]
    finally:
        fresh.close()


def test_manual_edit_while_page_fetch_in_flight(db, client, source):
    """分页请求阻塞期间，另一连接的人工更正与供数不被采集持锁阻塞。"""
    import threading as th

    bid = _batch(db, ["旧名甲"])
    source.page_size = 2
    source.block_on_call = 2  # 第 2 页请求会阻塞
    result: dict = {}

    def run_acquisition():
        result["body"] = client.post(
            "/acquisitions", json={"source_url": source.url}
        ).json()

    worker = th.Thread(target=run_acquisition)
    worker.start()
    assert source.entered.wait(timeout=10), "采集未进入第 2 页拉取"

    # 此时采集正在等 HTTP 响应；用独立连接做人工更正 + 供数，应立刻返回
    other = connect(db.execute("PRAGMA database_list").fetchone()["file"])
    try:
        rev = services.create_taxonomy_revision(
            other, TaxonomyCreate(accepted_names=["人工A"], synonym_edges=[])
        )
        rid = services.generate_result(other, ResultCreate(batch_id=bid))
        assert services.get_result(other, rid)["status"] == "active"
        assert services.latest_revision_id(other) == rev
    finally:
        other.close()

    source.release.set()
    worker.join(timeout=10)
    body = result["body"]
    assert body["status"] == "rejected"
    assert body["error_code"] == "taxonomy_changed"
    assert services.latest_revision_id(db) == rev


def test_stalled_cursor_is_rejected(db, client, source):
    """分页源返回未前进的游标时拒绝继续，避免重复页与无限拉取。"""
    source.page_size = 2
    orig_page = source.page

    def stalled(cursor):
        data = orig_page(cursor)
        if data["next_cursor"] is not None:
            data["next_cursor"] = cursor if cursor is not None else "0"
            if cursor is None:
                data["next_cursor"] = "0"
        return data

    source.page = stalled  # type: ignore[method-assign]
    # 第 1 页 next_cursor=0，第 2 页请求 cursor=0 又返回 0 -> 拒绝且不确认该页
    body = client.post("/acquisitions", json={"source_url": source.url}).json()
    assert body["status"] == "rejected"
    assert body["error_code"] == "source_protocol_error"
    assert body["pages_confirmed"] == 1
    assert services.latest_revision_id(db) is None
