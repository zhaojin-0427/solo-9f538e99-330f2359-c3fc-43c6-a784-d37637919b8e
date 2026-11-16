"""本地分类中心分页源（演示/联调用）。

- 按游标分页返回「版号 + 接受名 + 同义关系 + 下一页游标」；
- 支持运行时换版：POST /control/version 直接切换整版数据，
  换版后游标从新版第 1 页重新开始（演示跨页版号不一致会被拒绝）；
- 支持断流：POST /control/outage {"enabled": true} 让后续分页请求返回 503，
  用于验证采集器保留已确认页并在恢复后从游标续拉。

本模块只依赖标准库（http.server），不引入额外依赖。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE_SIZE_DEFAULT = int(os.environ.get("SOURCE_PAGE_SIZE", "3"))


class SourceState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.version = os.environ.get("SOURCE_VERSION", "2026-11-16.v1")
        self.accepted_names: list[str] = []
        self.synonym_edges: list[dict[str, str]] = []
        self.outage = os.environ.get("SOURCE_OUTAGE", "0") == "1"
        self.page_size = PAGE_SIZE_DEFAULT
        self.requests = 0
        self.load_builtin(self.version, variant=1)

    # ---- 数据装载 ----
    def load_builtin(self, version: str, variant: int) -> None:
        accepted = ["接受名A", "接受名B", "接受名C"]
        edges = [
            {"synonym": "旧名甲", "target": "中间名"},
            {"synonym": "中间名", "target": "接受名A"},
            {"synonym": "菱形名", "target": "s1"},
            {"synonym": "菱形名", "target": "s2"},
            {"synonym": "s1", "target": "接受名A"},
            {"synonym": "s2", "target": "接受名A"},
            {"synonym": "环名x", "target": "环名y"},
            {"synonym": "环名y", "target": "环名x"},
        ]
        if variant % 2 == 0:
            # 偶数版：分叉名同时指向 A、B（会产生 divergent_accepted 待核）
            edges.append({"synonym": "分叉名", "target": "接受名A"})
            edges.append({"synonym": "分叉名", "target": "接受名B"})
        else:
            # 奇数版：分叉名只指向 A
            edges.append({"synonym": "分叉名", "target": "接受名A"})
        self.version = version
        self.accepted_names = accepted
        self.synonym_edges = edges

    def set_payload(self, payload: dict) -> None:
        self.version = str(payload["version"])
        self.accepted_names = list(payload.get("accepted_names", []))
        self.synonym_edges = [
            {"synonym": e["synonym"], "target": e["target"]}
            for e in payload.get("synonym_edges", [])
        ]

    def combined(self) -> list[dict]:
        """把接受名与同义关系统一编码为可分页的记录流。"""
        records: list[dict] = [
            {"kind": "accepted", "name": name} for name in self.accepted_names
        ]
        records += [{"kind": "edge", **e} for e in self.synonym_edges]
        return records

    def page(self, cursor: str | None) -> dict:
        with self.lock:
            self.requests += 1
            if self.outage:
                raise RuntimeError("outage")
            records = self.combined()
            try:
                offset = int(cursor) if cursor not in (None, "") else 0
            except ValueError:
                offset = 0
            if offset < 0:
                offset = 0
            chunk = records[offset: offset + self.page_size]
            nxt = offset + len(chunk)
            next_cursor = str(nxt) if nxt < len(records) else None
            accepted = [r["name"] for r in chunk if r["kind"] == "accepted"]
            edges = [
                {"synonym": r["synonym"], "target": r["target"]}
                for r in chunk
                if r["kind"] == "edge"
            ]
            return {
                "version": self.version,
                "accepted_names": accepted,
                "synonym_edges": edges,
                "next_cursor": next_cursor,
            }


STATE = SourceState()


class Handler(BaseHTTPRequestHandler):
    server_version = "EcoMockSource/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "version": STATE.version,
                             "outage": STATE.outage})
            return
        if parsed.path == "/control/state":
            with STATE.lock:
                self._send(200, {
                    "version": STATE.version,
                    "outage": STATE.outage,
                    "page_size": STATE.page_size,
                    "record_count": len(STATE.combined()),
                    "requests": STATE.requests,
                })
            return
        if parsed.path == "/pages":
            query = parse_qs(parsed.query)
            cursor = (query.get("cursor") or [None])[0]
            try:
                page = STATE.page(cursor)
            except RuntimeError:
                self._send(503, {"detail": "分页源临时不可用（断流演示）"})
                return
            self._send(200, page)
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"detail": "invalid json"})
            return

        if parsed.path == "/control/version":
            # {"version": "...", "variant": 1|2} 或 {"version":..., "accepted_names": [...], "synonym_edges":[...]}
            version = str(payload.get("version") or "custom")
            with STATE.lock:
                if "accepted_names" in payload or "synonym_edges" in payload:
                    STATE.set_payload({"version": version, **payload})
                else:
                    STATE.load_builtin(version, int(payload.get("variant", 1)))
                state = {
                    "version": STATE.version,
                    "record_count": len(STATE.combined()),
                }
            self._send(200, state)
            return
        if parsed.path == "/control/outage":
            with STATE.lock:
                STATE.outage = bool(payload.get("enabled", True))
                outage = STATE.outage
            self._send(200, {"outage": outage})
            return
        if parsed.path == "/control/page_size":
            with STATE.lock:
                STATE.page_size = max(1, int(payload.get("size", PAGE_SIZE_DEFAULT)))
                size = STATE.page_size
            self._send(200, {"page_size": size})
            return
        self._send(404, {"detail": "not found"})


def main() -> None:
    host = os.environ.get("SOURCE_HOST", "0.0.0.0")
    port = int(os.environ.get("SOURCE_PORT", "9001"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"mock source listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
