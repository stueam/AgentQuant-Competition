# 后端接口

## HTTP

[OpenAPI 快照](openapi.json)是当前实现的 HTTP 契约。开发时也可以打开 API 的 `/docs`。前端仅通过相对路径调用 API，Vite 将 `/api` 请求转发给后端。

| 操作 | 请求 | 成功响应 |
| --- | --- | --- |
| 查内置策略及数据 | `GET /api/v1/catalog` | `200` 目录 |
| 创建提交 | `POST /api/v1/submissions` | `201` 提交 |
| 查提交历史 | `GET /api/v1/submissions`，可传 `offset`、`limit` | `200` 提交列表 |
| 发起验证 | `POST /api/v1/runs`，带 `Idempotency-Key` | `202` 任务 |
| 查任务状态 | `GET /api/v1/runs/{run_id}` | `200` 任务 |
| 查完整结果 | `GET /api/v1/runs/{run_id}/result` | `200` 结果。结果未就绪返回 `409`。 |
| 分项查看结果 | `GET /api/v1/runs/{run_id}/metrics`、`/equity`、`/trades` | `200` 摘要、净值或成交。成交支持 `offset`、`limit`，响应包含 `total`。 |

业务错误返回 `code` 与 `message`。请求字段校验采用 OpenAPI 中定义的 FastAPI `422` 结构。业务错误还包含 `request_id`，响应头返回 `X-Request-ID`。验证幂等键按队伍隔离，同队同键同参数返回相同任务，同队同键异参返回冲突。

### 登录与组队

组织者在 API 使用的数据库上运行 `uv run --locked python -m agentquant.accounts participant`，生成 24 小时内有效的一次性注册邀请码。命令也接受 `organizer` 和 `reviewer`。

1. 通过 `POST /api/v1/auth/register` 提交 `username`、`password` 和 `invite_code`。
2. 通过 `POST /api/v1/auth/login` 提交账号密码，保留响应设置的会话 Cookie。响应或 `GET /api/v1/me` 提供 `csrf_token`。已登录的写请求带上 `X-CSRF-Token`。
3. 通过 `POST /api/v1/teams` 创建队伍并取得组队码。队友通过 `POST /api/v1/teams/join` 加入，队内成员通过 `GET /api/v1/teams/{id}` 查看成员。使用 `POST /api/v1/auth/logout` 退出。

设置 `AGENTQUANT_ALLOW_DEMO=0` 要求登录及组队后才能访问提交和验证接口。默认的匿名样例使用独立的数据范围。HTTPS 环境设置 `AGENTQUANT_SECURE_COOKIE=1`。

内置策略提交请求示例。

```json
{"name":"因子排序基线","source":{"kind":"baseline","baseline_id":"factor-rank"}}
```

拿到提交 ID 后创建任务。状态为 `QUEUED`、`RUNNING`、`SUCCEEDED` 或 `FAILED`。成功后请求结果。

```json
{"submission_id":"2556983a-4687-41ef-be99-455e8f80a071","dataset_id":"synthetic-v1"}
```

### 策略 ZIP 和提交版本

1. 调用 `POST /api/v1/uploads` 登记 `size_bytes` 和小写十六进制 `sha256`。
2. 以 `application/zip` 调用 `PUT /api/v1/uploads/{id}/content` 写入压缩包原始字节。服务端核验大小和校验值。
3. 带 `Idempotency-Key` 调用 `POST /api/v1/uploads/{id}/complete` 做结构检查。同队同键同上传可重试。ZIP 须在根目录提供 `strategy.py`，压缩大小最多 2 MiB，解压大小最多 16 MiB，最多 128 个条目。
4. 在 `POST /api/v1/submissions` 中用 `source: {"kind":"archive","upload_id":"…"}` 创建不可覆盖的提交。响应包含队内版本号和服务端核验过的校验值。只有本队可以查询上传或下载已校验文件。

ZIP 目前可上传、校验和存档。沙箱试运行接入前，ZIP 提交发起验证或选择最终版本会返回 `STRATEGY_SMOKE_TEST_REQUIRED`。内置策略可走完整的验证和正式评测流程。

### 赛事日程和正式评测

联调赛事 ID 为 `track-2`。组织者通过 `PUT /api/v1/admin/competitions/track-2` 一次性配置带时区的 `validation_deadline`、`official_ready_at`，以及 `daily_validation_limit` 和 `quant_weight_bps`。前端通过 `GET /api/v1/competitions/track-2` 读取服务器时间与阶段。验证额度按北京时间的自然日计算，赛事尚未配置时每队每日可创建 20 个任务。

1. 参赛队通过赛事的 `/datasets` 和 `/datasets/{id}/download` 下载验证样例。正式样例仅由 Worker 读取。
2. 队长通过赛事的 `/final-selection` 选择本队提交，第一次携带 `revision: 0`。后续变更携带服务器返回的当前修订号。
3. 验证截止后，组织者调用 `POST /api/v1/admin/competitions/track-2/freeze`。队伍通过赛事的 `/freeze-receipt` 查回执。到截止时刻，新的验证任务和选择变更即时关闭。
4. 到正式数据就绪时间后，组织者调用 `POST /api/v1/admin/official-runs` 创建批次。批次锁定独立样例的校验值，Worker 对每支已封榜队伍执行正式任务。组织者可以轮询批次，查询成功结果，带理由重跑失败任务。

### 报告评审和榜单

1. 已封榜队伍通过 `POST /api/v1/reports` 提交 `title` 和 `body`，每次生成新版本。组织者通过 `GET /api/v1/admin/reports` 查报告，并用 `POST /api/v1/admin/review-assignments` 将固定报告版本分配给评委。
2. 评委通过 `GET /api/v1/reviews/assignments` 读取本人的任务，通过 `PUT /api/v1/reviews/{id}` 保存草稿或提交评分。第一次携带 `revision: 0`。提交时填写研究过程、反馈利用、效果、创新和复现五项分数，各为 0 至 100。
3. 组织者通过 `POST /api/v1/admin/leaderboards` 生成待发布版本。参与排名的队伍须有成功的最新正式任务，最新报告至少有一名评委且所有已分配评委均已提交。用 `POST /api/v1/admin/leaderboards/{id}/publish` 发布；报告或评分有变更时须重新生成。任何人可以通过赛事的 `/leaderboard` 读取已发布版本。

如队伍缺交报告，或最新一次正式评测已失败且无需继续重跑，组织者应在发榜前调用 `PUT /api/v1/admin/runs/{run_id}/leaderboard-exclusion`，提交 `status` 和 `reason`。评测成功且无报告时使用 `REPORT_MISSING`，评测失败时使用 `OFFICIAL_RUN_FAILED`。理由与操作者留在审计记录中。公开榜单在 `unranked` 中显示队名和状态，该队无排名及总分。队伍补交报告后进入正常评审流程，同队再次重跑后按最新任务判断。发榜后确认记录与正式批次锁定。

联调量化分为将 `50 + 100 × 净收益率` 限定在 0 至 100。报告分为所有评委分项的平均值。两项按 `quant_weight_bps` 加权，按总分、净收益率、报告分降序排名，均相同则并列。各分数四舍五入至两位小数。此计分版本为 `demo-score-v1`，正式口径由组委会确认。

调整路由和模型后，从仓库根目录更新契约，并在前端目录重新生成类型。

```powershell
uv run --locked python -m agentquant.api contracts/openapi.json
npm --prefix apps/web run generate:api
```

## 进程和 Python 交接

| 生产方 → 消费方 | 契约 |
| --- | --- |
| API → Worker | `RunSpec`，固定运行 ID、提交、数据、规则版本与种子。当前执行的策略引用为 `baseline_id`。正式运行同时固定数据校验值。 |
| Worker → API | `EvaluationResult`，含摘要、逐日净值、成交。货币和比率为十进制字符串。完成时校验任务租约。 |
| 数据 → 评测 | `Dataset`，首轮使用 [synthetic-v1.json](../src/agentquant/synthetic-v1.json)，含日期、决策时间、可见因子及成交参考数据。 |
| 评测 → 策略 | `Snapshot` 和 `StrategyContext`。快照含当时可见的匿名因子、可选标的、现金和持仓。 |
| 策略 → 评测 | `KEEP` 或 `TARGET`。目标权重用整数基点，每个标的只出现一次，合计最多 10,000。 |

```json
{"action":"TARGET","weights":[{"asset_id":"A001","weight_bps":5000}]}
```

类型定义见 [contracts.py](../src/agentquant/contracts.py)、[sdk.py](../src/agentquant/sdk.py) 和 [evaluator.py](../src/agentquant/evaluator.py)。接口有变化时，提交模型、OpenAPI 快照、生成类型和受影响的测试。

联调使用本地 SQLite 和人工数据，Worker 运行服务端内置策略。

## 数据接入交接

数据工作包交付标准化数据包和版本清单。清单至少记录数据集 ID、模式版本、数据分区、覆盖交易日、可用时间口径、文件校验值和发布范围。训练和验证下载包与主办方评测快照按发布范围交付。供应方凭据保留在采集侧。

赛务后端按数据集 ID 登记版本及授权信息。评测 Worker 按运行清单固定的数据集 ID 读取快照，校验版本和模式后交给评测器。前端通过业务 API 展示已授权的目录和下载入口。当前 [synthetic-v1.json](../src/agentquant/synthetic-v1.json) 可用于提前联调，真实数据包以评测输入模式和赛前数据规则验收。
