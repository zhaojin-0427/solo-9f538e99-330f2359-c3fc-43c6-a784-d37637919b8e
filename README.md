# 生态监测名称归并供数 API

把野外观测记录中的旧物种名（同义名）沿分类表的指向关系追踪到**接受名**，生成可重读、
可追溯修订的供数结果。技术栈：FastAPI + SQLite，本地单容器部署。

## 业务规则

### 观测批次
- `POST /batches` 一次录入一批观测，**至多 100 条**，每条含批内唯一编号 `code` 与物种名 `name`。
- 批内编号重复返回 `409`，且整批回滚、不留部分数据。

### 分类表修订（可整版更正）
- `POST /taxonomies` 整版提交分类表：接受名列表 + **至多 40 条**同义名指向关系。
- 同义关系为有向边（`synonym → target`），**允许分叉**（一个名称指向多个目标），也允许链式与菱形合流。
- 每次提交生成一个新的不可变修订；旧修订标记 `superseded`，引用旧修订的有效结果立即置为 `expired`。

### 名称追踪与供数判定
对每条观测的名称，沿所有可达路径追踪：

| 情况 | 处理 | reason_code |
|---|---|---|
| 所有路径无环且汇聚到**同一个**接受名 | 纳入供数 `included`，记录接受名 | — |
| 不同路径通向不同接受名（分叉冲突） | 待核 `pending` | `divergent_accepted` |
| 可达子图中存在环（含自环） | 待核 `pending` | `cycle` |
| 没有可达的接受名（含部分路径断在非接受名） | 待核 `pending` | `no_accepted_name` |

### 结果
- `POST /results` 生成结果，**固定观测批次与分类表修订**，返回结果号 `result_id`。
- `GET /results/{result_id}` 按结果号重读；分类表更正后旧结果标 `expired` 但内容冻结、仍可读取。
- 生成与更正交错时：写操作经 SQLite `BEGIN IMMEDIATE` 串行化；针对旧修订生成的结果直接落为
  `expired`，**旧修订永远不会覆盖当前供数**；任何失败整体回滚，不产生部分结果。
- 同一批次新生成的当前结果会使该批次旧的 `active` 结果过期，保证每批次至多一份当前供数。

### 分类中心采集（免手工抄表）
从分类中心分页源拉取接受名与同义关系，整版到齐并校验后自动发布分类表新修订。

- `POST /acquisitions` 新建采集并立即尝试拉取；`POST /acquisitions/{id}/continue` 继续；
  `GET /acquisitions`、`GET /acquisitions/{id}` 查询进度、已确认页与发布结果。
- 分页源单页返回：`version`（整版版号）、`accepted_names`、`synonym_edges`、
  `next_cursor`（`null` 表示最后一页）。请求形如 `GET {source_url}?cursor=...`。
- **逐页确认、断点续拉**：每页在独立短事务中落盘后才推进游标。断网或服务重启后，
  已确认页与游标仍然保留，继续时从游标处续拉，不重取已确认页。
  断网只记录 `error_code=source_unavailable`，采集保持 `in_progress` 可重试。
- **不得拼版**：同一整版所有页版号必须一致；跨页版号不同立即 `rejected`
  （`error_code=version_mismatch`），不发布任何修订。
- **拉取期间照常人工更正与供数**：HTTP 拉取不持数据库写锁，`POST /taxonomies`、
  `POST /results` 均可正常使用。
- **首次发布守卫**：全部页到齐并通过原有整版校验（接受名非空/不重复、同义关系不重复、
  整版至多 40 条）后，才在单个事务内发布新修订。若采集开始后分类表被其他操作更正过，
  即使内容改回原样也拒绝覆盖（`error_code=taxonomy_changed`）。
- **发布事务**：发布成功使旧修订 `superseded`、旧供数 `expired`；写入失败整体回滚，
  原分类与供数不变、采集保持 `ready` 可重试（`error_code=write_failed`）。
  已完成的采集再次继续是幂等的：返回首次发布的同一修订号，不会新增修订，
  也不会使之后生成的新供数过期。
- 采集状态：`in_progress`（仍可续拉）/ `ready`（到齐待发布）/ `completed`（已发布）/
  `rejected`（版号不一致、校验失败或分类表被改，拒绝发布）。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs   # Linux 用 xdg-open
```

Compose 同时启动：

- `eco-api`（端口 8000）：本应用，默认从 `http://eco-source:9001/pages` 采集；
- `eco-source`（端口 9001）：本地分类中心分页源，可换版、可断流。

SQLite 数据保存在命名卷 `eco_data`（容器内 `/data/eco.db`），容器重建不丢数据。

分页源控制接口（演示换版/断流）：

```bash
# 切换整版（variant 奇数/偶数会让“分叉名”分别指向单一/两个接受名）
curl -s -X POST localhost:9001/control/version -H 'Content-Type: application/json' \
  -d '{"version":"2026-11-16.v2","variant":2}'
# 断流（后续分页请求返回 503）
curl -s -X POST localhost:9001/control/outage -H 'Content-Type: application/json' \
  -d '{"enabled":true}'
# 恢复
curl -s -X POST localhost:9001/control/outage -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
```

> 换版会让分页源从新版第 1 页重新开始，因此用于验证“新采集”；若在同一个
> 未完成采集跨页换版，版号不一致会被拒绝发布。

### 本地直接运行

```bash
pip install -r requirements.txt
# 另开终端启动本地分页源（可选，默认端口 9001）
python -m mock_source.server
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可通过环境变量指定库路径：ECO_DB_PATH=/path/to/eco.db
# 分页源地址：ECO_SOURCE_URL（默认 http://localhost:9001/pages）
```

## /docs 操作流程（Swagger UI）

1. `POST /batches` 录入观测批次；
2. `POST /taxonomies` 提交分类表（更正时再次调用即可，自动出新修订）；
   或 `POST /acquisitions` 从分类中心分页源自动采集（可断点续拉）；
3. `POST /results` 生成供数结果；
4. `GET /results/{result_id}` 查询明细，或 `GET /results?status=active` 查看当前供数；
5. `GET /acquisitions/{id}` 查看采集进度、已确认页和发布的修订号。

## curl 示例

```bash
# 1. 录入观测（6 条，覆盖纳入/分叉/环/无接受名/合流/直达）
curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "note": "第一批",
  "observations": [
    {"code": "o1", "name": "旧名甲"},
    {"code": "o2", "name": "分叉名"},
    {"code": "o3", "name": "环名x"},
    {"code": "o4", "name": "无人名"},
    {"code": "o5", "name": "菱形名"},
    {"code": "o6", "name": "接受名A"}
  ]}'

# 2. 提交分类表（10 条边，含分叉与环）
curl -s -X POST localhost:8000/taxonomies -H 'Content-Type: application/json' -d '{
  "note": "v1",
  "accepted_names": ["接受名A", "接受名B"],
  "synonym_edges": [
    {"synonym": "旧名甲", "target": "中间名"},
    {"synonym": "中间名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "环名x", "target": "环名y"},
    {"synonym": "环名y", "target": "环名x"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"}
  ]}'

# 3. 生成供数结果（revision_id 缺省取最新修订）
curl -s -X POST localhost:8000/results -H 'Content-Type: application/json' \
  -d '{"batch_id": 1}'

# 4. 按结果号重读
curl -s localhost:8000/results/1

# 5. 从本地分页源采集整版分类表（自动拉完校验后发布新修订）
curl -s -X POST localhost:8000/acquisitions \
  -H 'Content-Type: application/json' -d '{"note":"首次采集"}'

# 6. 断流后继续（已确认页与游标保留，从游标续拉）
curl -s -X POST localhost:8000/acquisitions/1/continue
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/batches` | 录入观测批次（≤100 条） |
| GET | `/batches` | 批次列表 |
| GET | `/batches/{id}` | 批次明细 |
| POST | `/taxonomies` | 整版提交分类表，生成新修订（边 ≤40） |
| GET | `/taxonomies` | 修订列表 |
| GET | `/taxonomies/{id}` | 修订完整内容 |
| POST | `/results` | 生成供数结果 |
| GET | `/results` | 结果列表（可按 `batch_id`、`status` 过滤） |
| GET | `/results/{id}` | 按结果号重读（含每条观测的追踪结论与原因） |
| POST | `/acquisitions` | 新建分类中心采集并尝试拉取（支持断点续拉） |
| POST | `/acquisitions/{id}/continue` | 继续采集（已完成的幂等返回同一修订） |
| GET | `/acquisitions` | 采集任务列表 |
| GET | `/acquisitions/{id}` | 采集进度、已确认页与发布结果 |

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

- `tests/test_resolver.py`：解析算法（链、分叉合流、分叉冲突、环、断链等 11 个用例）；
- `tests/test_api.py`：端到端业务场景（更正过期、旧修订不得激活、上限校验、404、结果冻结等）；
- `tests/test_acquisition.py`：采集（正常发布并过期旧供数、断流保留已确认页并续拉、
  进程重启后用新连接续拉、跨页版号不一致拒绝、拉取期间人工更正/供数可用、
  发布前分类表被改（即使改回）拒绝覆盖、写入失败回滚并幂等重试、40 条上限校验）；
- `tests/test_concurrency.py`：4 线程 100 次生成/更正交错，校验无部分结果、至多一份当前供数、
  active 必属最新修订、结果与串行重算一致。

## 目录结构

```
app/
  database.py    # SQLite 连接、建表、IMMEDIATE 写事务
  models.py      # Pydantic v2 请求/响应模型与上限校验
  resolver.py    # 名称追踪：可达性 + Kahn 环检测 + 汇聚判定
  services.py    # 事务化业务服务（批次/修订/结果）
  acquisition.py # 分页采集：逐页确认、断点续拉、版号校验、发布
  main.py        # FastAPI 路由
mock_source/
  server.py      # 本地分页源：可换版、可断流（Compose 一并启动）
tests/           # pytest 测试
Dockerfile, Dockerfile.source, docker-compose.yml, requirements.txt
```
