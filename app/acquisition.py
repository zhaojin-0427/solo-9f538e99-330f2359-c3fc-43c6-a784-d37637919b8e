"""分类中心分页采集：逐页确认落盘、断点续拉、整版校验后发布修订。

关键约束：
- 每一页在独立短事务中确认后才推进游标；HTTP 拉取不占用数据库写锁，
  因此拉取期间人工更正分类表、生成供数仍可正常进行。
- 断网/重启后从持久化的游标继续，已确认页不会重复拉取。
- 同一整版内版号必须一致，跨页版号不同直接拒绝发布，绝不拼版。
- 全部页到齐并通过原有分类表校验后，才在单个事务内发布新修订：
  若采集开始后分类表被其他操作改过（即使内容改回），拒绝覆盖；
  发布成功使旧供数过期；发布事务失败则原分类与供数保持不变。
- 发布幂等：已完成的采集再次继续时原样返回首次发布的修订号，
  不会新建修订，也不会使之后生成的新结果过期。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import httpx

from . import services
from .database import immediate_tx
from .models import AcquisitionCreate, SourcePage
from .services import ConflictError, NotFoundError


DEFAULT_SOURCE_URL = "http://localhost:9001/pages"
FETCH_TIMEOUT = float(os.environ.get("ECO_SOURCE_TIMEOUT", "5"))
MAX_PAGES = int(os.environ.get("ECO_MAX_ACQUISITION_PAGES", "1000"))

# 同一进程内串行处理同一采集，防止并发 continue 重复拉页/重复发布。
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


class SourceError(Exception):
    """分页源不可用或响应不符合约定（可重试）。"""

    code = "source_error"


class SourceUnavailableError(SourceError):
    code = "source_unavailable"


class SourceProtocolError(SourceError):
    code = "source_protocol_error"


def _lock_for(acquisition_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(acquisition_id, threading.Lock())


# ---------- HTTP 分页源 ----------

def fetch_source_page(source_url: str, cursor: str | None) -> SourcePage:
    """拉取一页。游标为空表示第一页；异常统一转成可重试的 SourceError。"""
    params = {"cursor": cursor} if cursor is not None else None
    try:
        response = httpx.get(source_url, params=params, timeout=FETCH_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise SourceUnavailableError(
            f"分页源返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailableError(f"无法访问分页源：{exc}") from exc

    try:
        return SourcePage.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError
        raise SourceProtocolError(f"分页源响应不符合约定：{exc}") from exc


# ---------- 读取 ----------

def get_acquisition(conn: sqlite3.Connection, acquisition_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT a.*,
               (SELECT COUNT(*) FROM acquisition_page p
                 WHERE p.acquisition_id = a.acquisition_id) AS pages_confirmed
          FROM acquisition a
         WHERE a.acquisition_id = ?
        """,
        (acquisition_id,),
    ).fetchone()


def list_acquisition_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*,
               (SELECT COUNT(*) FROM acquisition_page p
                 WHERE p.acquisition_id = a.acquisition_id) AS pages_confirmed
          FROM acquisition a
         ORDER BY a.acquisition_id
        """
    ).fetchall()


def list_pages(conn: sqlite3.Connection, acquisition_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT page_no, request_cursor, next_cursor, version,
               accepted_names, synonym_edges, confirmed_at
          FROM acquisition_page
         WHERE acquisition_id = ?
         ORDER BY page_no
        """,
        (acquisition_id,),
    ).fetchall()


def acquisition_view(conn: sqlite3.Connection, acquisition_id: int) -> dict | None:
    row = get_acquisition(conn, acquisition_id)
    if row is None:
        return None
    pages = []
    accepted_count = 0
    edge_count = 0
    for p in list_pages(conn, acquisition_id):
        names = json.loads(p["accepted_names"])
        edges = json.loads(p["synonym_edges"])
        accepted_count += len(names)
        edge_count += len(edges)
        pages.append(
            {
                "page_no": p["page_no"],
                "request_cursor": p["request_cursor"],
                "next_cursor": p["next_cursor"],
                "version": p["version"],
                "accepted_count": len(names),
                "edge_count": len(edges),
                "confirmed_at": p["confirmed_at"],
            }
        )
    view = dict(row)
    view["accepted_count"] = accepted_count
    view["edge_count"] = edge_count
    view["pages"] = pages
    return view


# ---------- 创建 / 继续 ----------

def create_acquisition(
    conn: sqlite3.Connection, payload: AcquisitionCreate
) -> int:
    source_url = (
        payload.source_url
        or os.environ.get("ECO_SOURCE_URL")
        or DEFAULT_SOURCE_URL
    )
    with immediate_tx(conn):
        # 记录“开始拉取”时的分类表版本，用于首次发布时检测期间是否被更正。
        base_revision_id = services.latest_revision_id(conn)
        cur = conn.execute(
            """
            INSERT INTO acquisition(source_url, note, status, base_revision_id)
            VALUES (?, ?, 'in_progress', ?)
            """,
            (source_url, payload.note, base_revision_id),
        )
        acquisition_id = cur.lastrowid
    advance_acquisition(conn, acquisition_id)
    return acquisition_id


def continue_acquisition(conn: sqlite3.Connection, acquisition_id: int) -> None:
    if get_acquisition(conn, acquisition_id) is None:
        raise NotFoundError(f"采集任务不存在：acquisition_id={acquisition_id}")
    advance_acquisition(conn, acquisition_id)


def advance_acquisition(conn: sqlite3.Connection, acquisition_id: int) -> None:
    """尽量推进采集：拉页直到结束、被拒绝或遇到可重试错误。"""
    lock = _lock_for(acquisition_id)
    if not lock.acquire(blocking=False):
        raise ConflictError("该采集任务正在被另一个请求处理，请稍后重试")
    try:
        while True:
            row = get_acquisition(conn, acquisition_id)
            if row is None:
                raise NotFoundError(f"采集任务不存在：acquisition_id={acquisition_id}")
            status = row["status"]
            if status in ("completed", "rejected"):
                return
            if status == "ready":
                try:
                    _publish(conn, acquisition_id)
                except (sqlite3.Error, OSError) as exc:
                    # 写入失败：事务已整体回滚，分类表与供数保持原样；
                    # 采集仍为 ready，可直接重试。
                    _record_error(
                        conn,
                        acquisition_id,
                        "write_failed",
                        f"发布写入失败，可重试：{exc}",
                    )
                return
            if row["pages_confirmed"] >= MAX_PAGES:
                _reject(
                    conn,
                    acquisition_id,
                    "page_limit",
                    f"分页数超过上限 {MAX_PAGES}，拒绝继续拉取",
                )
                return

            page_no = row["pages_confirmed"] + 1
            request_cursor = row["next_cursor"]
            try:
                page = fetch_source_page(row["source_url"], request_cursor)
            except SourceError as exc:
                _record_error(conn, acquisition_id, exc.code, str(exc))
                return

            outcome = _confirm_page(
                conn, acquisition_id, page_no, request_cursor, page
            )
            if outcome != "confirmed":
                return
            # 最后一页已在同一事务中置为 ready；下一轮循环负责发布。
    finally:
        lock.release()


def _record_error(
    conn: sqlite3.Connection, acquisition_id: int, code: str, detail: str
) -> None:
    """记录可重试错误：保持 in_progress，游标与已确认页不动。"""
    with immediate_tx(conn):
        conn.execute(
            """
            UPDATE acquisition
               SET error_code = ?, error_detail = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE acquisition_id = ? AND status IN ('in_progress', 'ready')
            """,
            (code, detail, acquisition_id),
        )


def _reject(
    conn: sqlite3.Connection, acquisition_id: int, code: str, detail: str
) -> None:
    with immediate_tx(conn):
        conn.execute(
            """
            UPDATE acquisition
               SET status = 'rejected', error_code = ?, error_detail = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE acquisition_id = ? AND status IN ('in_progress', 'ready')
            """,
            (code, detail, acquisition_id),
        )


def _confirm_page(
    conn: sqlite3.Connection,
    acquisition_id: int,
    page_no: int,
    request_cursor: str | None,
    page: SourcePage,
) -> str:
    """在短事务内确认一页并推进游标；最后一页同时置 ready。"""
    outcome = "confirmed"
    with immediate_tx(conn):
        row = conn.execute(
            "SELECT * FROM acquisition WHERE acquisition_id = ?",
            (acquisition_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"采集任务不存在：acquisition_id={acquisition_id}")
        if row["status"] != "in_progress":
            return "stopped"
        if row["next_cursor"] != request_cursor:
            # 游标已被其他请求推进（极端并发），放弃这一页，避免重复写入。
            return "stale"

        # 跨页版号必须一致：一旦不同立即拒绝，不能拼版。
        if row["version"] is not None and page.version != row["version"]:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected',
                       error_code = 'version_mismatch',
                       error_detail = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (
                    f"第 {page_no} 页版号 {page.version} 与此前版号 "
                    f"{row['version']} 不一致，拒绝拼版发布",
                    acquisition_id,
                ),
            )
            return "version_mismatch"

        # 游标必须前进；原地打转的游标会导致重复页/无限拉取，直接拒绝。
        if page.next_cursor is not None and page.next_cursor == request_cursor:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected',
                       error_code = 'source_protocol_error',
                       error_detail = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (
                    f"第 {page_no} 页返回的 next_cursor 未前进（{page.next_cursor}），"
                    "拒绝继续拉取",
                    acquisition_id,
                ),
            )
            return "cursor_stalled"

        version = row["version"] or page.version
        next_cursor = page.next_cursor
        final = next_cursor is None
        conn.execute(
            """
            INSERT INTO acquisition_page(
                acquisition_id, page_no, request_cursor, next_cursor, version,
                accepted_names, synonym_edges
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acquisition_id,
                page_no,
                request_cursor,
                next_cursor,
                version,
                json.dumps(page.accepted_names, ensure_ascii=False),
                json.dumps(
                    [e.model_dump() for e in page.synonym_edges],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            UPDATE acquisition
               SET version = ?, next_cursor = ?, status = ?,
                   error_code = NULL, error_detail = NULL,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE acquisition_id = ?
            """,
            (
                version,
                next_cursor,
                "ready" if final else "in_progress",
                acquisition_id,
            ),
        )
    return outcome


# ---------- 发布 ----------

def _publish(conn: sqlite3.Connection, acquisition_id: int) -> None:
    """整版到齐后发布新修订。

    整个发布过程在一个 IMMEDIATE 事务内：
    - 若已发布过，直接返回（幂等，不新增修订、不使后来结果过期）；
    - 若采集开始后分类表被其他操作更正过，拒绝覆盖；
    - 校验不通过则拒绝，不产生修订；
    - 全部通过才写新修订并使旧供数过期；任何写入异常整体回滚。
    """
    with immediate_tx(conn):
        row = conn.execute(
            "SELECT * FROM acquisition WHERE acquisition_id = ?",
            (acquisition_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"采集任务不存在：acquisition_id={acquisition_id}")
        if row["status"] == "completed":
            # 幂等重试：返回同一修订，保持后来生成的结果有效。
            return
        if row["status"] != "ready" or row["next_cursor"] is not None:
            return

        pages = list_pages(conn, acquisition_id)
        if not pages:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected', error_code = 'no_pages',
                       error_detail = '没有已确认的分页数据，拒绝发布',
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (acquisition_id,),
            )
            return

        # 防御性复核：所有已确认页版号一致。
        versions = {p["version"] for p in pages}
        if len(versions) != 1:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected', error_code = 'version_mismatch',
                       error_detail = '已确认页存在不同版号，拒绝拼版发布',
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (acquisition_id,),
            )
            return

        accepted_names: list[str] = []
        synonym_edges: list[tuple[str, str]] = []
        for p in pages:
            accepted_names.extend(json.loads(p["accepted_names"]))
            synonym_edges.extend(
                (e["synonym"], e["target"])
                for e in json.loads(p["synonym_edges"])
            )

        # 首次发布前检查：开始拉取后若分类表被任何其他操作更正过，
        # 即使内容改回原样也拒绝覆盖（用修订号单调递增判断，不比对内容）。
        current_revision_id = services.latest_revision_id(conn)
        if current_revision_id != row["base_revision_id"]:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected', error_code = 'taxonomy_changed',
                       error_detail = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (
                    "采集开始后分类表已被其他操作更正（当前修订 "
                    f"{current_revision_id}，开始时 {row['base_revision_id']}），"
                    "拒绝覆盖",
                    acquisition_id,
                ),
            )
            return

        # 沿用原有整版校验（接受名非空/不重复、边不重复、整版至多 40 条）。
        try:
            services.validate_taxonomy_data(accepted_names, synonym_edges)
        except ConflictError as exc:
            conn.execute(
                """
                UPDATE acquisition
                   SET status = 'rejected', error_code = 'validation_error',
                       error_detail = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                 WHERE acquisition_id = ?
                """,
                (str(exc), acquisition_id),
            )
            return

        note = row["note"] or f"采集 #{acquisition_id} 版号 {row['version']}"
        revision_id = services.insert_taxonomy_revision(
            conn, accepted_names, synonym_edges, note
        )
        conn.execute(
            """
            UPDATE acquisition
               SET status = 'completed', published_revision_id = ?,
                   error_code = NULL, error_detail = NULL,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
             WHERE acquisition_id = ?
            """,
            (revision_id, acquisition_id),
        )
