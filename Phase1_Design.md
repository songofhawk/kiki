# Personal Agent (PA) V1 系统架构设计文档

## 1. 设计目标与核心原则

本系统旨在构建一个“统一 IM 交互流 + 自动话题路由 + 异步后台执行”**的个人智能体（Personal Agent）。用户只需在单一聊天窗口中自然交流，系统自动将对话归类到对应的逻辑话题（Topic），并在需要时将重型任务异步派发给后台 Coding/Execution Agent（如 Claude Code、Pi、Codex）执行，做到**前台不阻塞、话题不串台、后台可插拔。

### 核心架构原则

1. **三层彻底解耦**：交互记忆层、决策路由层、异步执行层各自独立。不绑定重型 Agent 框架的前端或存储结构，避免被单一框架的会话模型锁死。
2. **确定性优先，模型兜底**：显式引用、界面锁定等确定性规则 0ms 拦截；仅在语义模糊的全局流输入时调用轻量级 Decision Model。
3. **长短生命周期分离**：将“跨天持续的逻辑会话”与“单次操作系统子进程执行”彻底拆分为 `AgentSession` 与 `SessionRun`，杜绝僵尸进程与状态机混乱。
4. **可纠错闭环**：接受路由模型存在约 5% 的误分类，通过可变 `topic_id` 与审计表提供低成本的人工纠错与数据回流机制。

---

## 2. 系统总体架构

```text
+-----------------------------------------------------------------------+
|                       前端交互层 (Unified IM UI)                       |
|  - 全局 IM 主流视图 & 侧边栏 Topic 列表                                |
|  - 实时进度卡片 (SSE 原地刷新) & 拖拽改归属纠错                        |
+-----------------------------------+-----------------------------------+
                                    | REST API / SSE
+-----------------------------------v-----------------------------------+
|                      前台编排层 (ConversationService)                  |
|                                                                       |
|  [1. Fast-Path 规则拦截] ──(未命中)──▶ [2. Decision Model 路由器]      |
|       (引用回复 / 界面锁定)              (混合召回 + 结构化 JSON 输出) |
|                                                │                      |
|  [3. 记忆与状态管理] ◀─────────────────────────┘                      |
|       - 维护 Topics / Messages / RoutingDecisions                     |
|       - 异步刷新 rolling_summary & key_entities                        |
+-----------------------------------+-----------------------------------+
                                    | 提交 SessionRun (携带 topic_id)
+-----------------------------------v-----------------------------------+
|                        异步执行层 (TaskManager)                        |
|  - 同 Topic 互斥锁 (asyncio.Lock) + 全局并发限流 (Semaphore)           |
|  - 进程组生命周期管理 (start_new_session + SIGTERM/SIGKILL)            |
|                                                                       |
|  +-----------------------------------------------------------------+  |
|  |                  Executor Adapter (NDJSON 翻译层)                |  |
|  +-----------------+-----------------------+-----------------------+  |
|  | ClaudeCode CLI  |     Pi (--mode rpc)   |    Codex (exec)       |  |
|  +-----------------+-----------------------+-----------------------+  |
+-----------------------------------------------------------------------+

```

---

## 3. 核心领域模型与数据库设计

### 3.1 三层核心实体的生命周期区分

| 实体 | 所属层级 | 生命周期 | 核心职责 |
| --- | --- | --- | --- |
| **`Topic`** | 对话记忆层 | 长（数天~数周） | 面向人的语义话题，聚合相关消息与滚动摘要。 |
| **`AgentSession`** | 异步执行层 | 长（与 Topic 同步） | 绑定后台执行器的持久上下文索引（`external_session_id`）与工作目录。 |
| **`SessionRun`** | 异步执行层 | 短（秒~数十分钟） | 对应操作系统里**一次具体的子进程拉起**（内含外部 Agent 自己的 N 个内部 `turn`）。 |

关系映射：`topics (1) ──▶ (N) messages`、`topics (1) ──▶ (N) agent_sessions (1) ──▶ (N) session_runs`。

### 3.2 数据库表结构（PostgreSQL / SQLite 通用）

```sql
-- ==========================================
-- 1. 对话与记忆层 (前台 PA 管理)
-- ==========================================

CREATE TABLE topics (
    id              TEXT PRIMARY KEY,              -- ULID
    title           TEXT NOT NULL,                 -- 简短标题（<=10字）
    status          TEXT NOT NULL DEFAULT 'active',-- active | dormant | archived
    rolling_summary TEXT NOT NULL DEFAULT '',      -- 滚动摘要（<=150字，专供路由召回与冷启动）
    key_entities    JSONB NOT NULL DEFAULT '[]',   -- 关键实体/标签
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE messages (
    id                  TEXT PRIMARY KEY,          -- ULID
    topic_id            TEXT NOT NULL REFERENCES topics(id),
    role                TEXT NOT NULL,             -- user | assistant | system
    content             TEXT NOT NULL,
    reply_to_message_id TEXT REFERENCES messages(id), -- 显式引用，用于 0ms 路由
    related_run_id      TEXT,                      -- 弱关联 session_runs.id，避免循环外键
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE routing_decisions (
    id                  TEXT PRIMARY KEY,          -- ULID
    message_id          TEXT NOT NULL UNIQUE REFERENCES messages(id),
    route_source        TEXT NOT NULL,             -- rule_reply | rule_pin | decision_model
    candidate_topic_ids JSONB NOT NULL DEFAULT '[]',
    action              TEXT NOT NULL,             -- append | new_topic
    predicted_topic_id  TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 1.0, -- 0.0 ~ 1.0
    needs_heavy_agent   BOOLEAN NOT NULL DEFAULT FALSE,
    reasoning           TEXT,
    is_overridden       BOOLEAN NOT NULL DEFAULT FALSE, -- 人工纠错标记
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ==========================================
-- 2. 异步执行层 (后台 TaskManager 管理)
-- ==========================================

CREATE TABLE agent_sessions (
    id                  TEXT PRIMARY KEY,          -- ULID
    topic_id            TEXT NOT NULL REFERENCES topics(id),
    executor_type       TEXT NOT NULL,             -- pi | claude_code | codex
    external_session_id TEXT,                      -- 外部 Agent 的 session_id，用于 --resume
    work_dir            TEXT NOT NULL,             -- 绑定的工作目录 / 沙箱路径
    status              TEXT NOT NULL DEFAULT 'active', -- active | archived
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE session_runs (
    id                  TEXT PRIMARY KEY,          -- ULID
    agent_session_id    TEXT NOT NULL REFERENCES agent_sessions(id),
    trigger_message_id  TEXT NOT NULL REFERENCES messages(id),
    prompt              TEXT NOT NULL,             -- 本次喂给后台 Agent 的实际指令
    status              TEXT NOT NULL DEFAULT 'queued',
    -- queued | running | waiting_input | completed | failed | cancelled
    current_step        TEXT NOT NULL DEFAULT '',  -- 实时进度文案（SSE 原地刷新）
    result_summary      TEXT,                      -- 本次运行的最终输出/结论
    artifacts           JSONB NOT NULL DEFAULT '[]',
    error_message       TEXT,
    pid                 INTEGER,
    exit_code           INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

-- ==========================================
-- 3. 核心索引
-- ==========================================

CREATE INDEX idx_topics_active ON topics(status, last_active_at DESC);
CREATE INDEX idx_messages_global_time ON messages(created_at DESC);
CREATE INDEX idx_messages_topic_time ON messages(topic_id, created_at ASC);
CREATE INDEX idx_sessions_topic_status ON agent_sessions(topic_id, status);
CREATE INDEX idx_runs_session_time ON session_runs(agent_session_id, created_at DESC);
CREATE INDEX idx_runs_active_status ON session_runs(status)
    WHERE status IN ('queued', 'running', 'waiting_input');

```

---

## 4. Decision Model 自动分话题设计

目标是单次路由延迟 **< 500ms**，输入总 Token 预算控制在 **1500 ~ 2500 Tokens**。

### 4.1 两级路由链路

1. **确定性快速通道（Fast-Path，0ms）**：
* `reply_to_message_id` 非空 -> 直接继承被引用消息的 `topic_id`。
* 前端在特定 Topic 视图内发消息 -> 直接锁定该 `topic_id`。


2. **Decision Model 上下文组装（三段式）**：
* **时间差信号**：当前时间 + 距离上一条全局消息的 `delta_t`（短间隔强倾向延续，长间隔强倾向切换）。
* **短期全局窗口**：最近 4~6 条全局 IM 消息（附带 `topic_id` 和 `role`），解决代词指代。
* **混合召回候选池（Top 8~10）**：最近活跃的 5 个 `active` 话题 + 基于当前消息 FTS/BM25（或轻量 Embedding）召回的 3~5 个历史 `active/dormant` 话题，去重合并。



### 4.2 结构化输出 Schema（Pydantic）

```python
class RoutingDecision(BaseModel):
    reasoning: str = Field(description="1句话简述判断依据：结合时间间隔、指代关系与候选摘要。")
    action: Literal["append", "new_topic"]
    target_topic_id: Optional[str] = Field(None, description="action='append' 时必填，须在候选池内。")
    new_topic_title: Optional[str] = Field(None, description="action='new_topic' 时必填，<=10字。")
    confidence: float = Field(ge=0.0, le=1.0)
    needs_heavy_agent: bool = Field(description="需读写文件、跑命令、深度调研或耗时>15s时为 true。")
    heavy_task_brief: Optional[str] = Field(None, description="needs_heavy_agent=true 时提炼的一句话任务指令。")

```

### 4.3 置信度门控与纠错闭环

* **Confidence Gate**：若 `confidence < 0.65`，默认先归入最近活跃话题保证对话不中断，同时前端在消息旁挂载轻量胶囊（`归类存疑 · 点此移至新话题`）。
* **Override Hook**：用户手动更改消息归属时，后端更新 `messages.topic_id` -> 标记 `routing_decisions.is_overridden = true` -> 异步重算新旧两个 Topic 的 `rolling_summary`。`is_overridden = true` 的记录沉淀为路由 Prompt 的 Eval 评测集。

---

## 5. 异步执行层与通信协议

### 5.1 Executor Adapter 与统一事件流

所有后台执行器（`claude -p --output-format stream-json`、`codex exec --json`、`pi --mode rpc`）均由 Adapter 封装，将各家私有 NDJSON 输出翻译为 5 种内部标准事件：

```json
{"type": "started",        "run_id": "run_01", "topic_id": "top_01", "payload": {"external_session_id": "ses_abc"}}
{"type": "progress",       "run_id": "run_01", "topic_id": "top_01", "payload": {"step": "正在读取 /var/log/nginx/error.log"}}
{"type": "input_required", "run_id": "run_01", "topic_id": "top_01", "payload": {"prompt": "发现 3 个异常容器，是否重启？"}}
{"type": "completed",      "run_id": "run_01", "topic_id": "top_01", "payload": {"summary": "...", "artifacts": []}}
{"type": "failed",         "run_id": "run_01", "topic_id": "top_01", "payload": {"error": "Timeout after 600s"}}

```

### 5.2 `SessionRun` 状态机

```text
[QUEUED] ──(获取锁与信号量)──▶ [RUNNING]
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
     [WAITING_INPUT]         [COMPLETED]            [FAILED]
            │
  (用户在 Topic 回复，派生新 Run 恢复 external_session_id，旧 Run 置为 COMPLETED)

```

*(任意非终态均可由用户主动触发转为 `CANCELLED`。)*

### 5.3 并发、进程与上下文治理

1. **双层锁顺序**：`async with topic_lock, global_semaphore:`。先锁同 Topic 防止并发写冲突，再占全局并发槽位（如单机限制 `max_concurrency = 3`），避免单话题排队耗尽全局并发。
2. **防孤儿进程**：拉起子进程强制设置 `start_new_session=True`；退出、超时或取消时，在 `finally` 中向整个进程组发 `SIGTERM`，宽限 5 秒后发 `SIGKILL`。
3. **防缓冲区溢出**：`asyncio.create_subprocess_exec` 显式配置 `limit=10 * 1024 * 1024`（10MB），防止 Agent 单行输出大段代码触发默认的 64KB `LimitOverrunError`。
4. **进度节流**：Adapter 过滤细碎 token 流，仅上报工具调用/阶段切换；`TaskManager` 对 `progress` 事件做 1.5 秒节流，通过 SSE 原地刷新前端卡片，**不写入 `messages` 表**。
5. **长周期 `--resume` 与 Session 重置**：跨天拉起时，直接读取 `agent_sessions.external_session_id` 派生新的 `SessionRun`；当外部上下文膨胀或需切换执行器时，将旧 `agent_sessions.status` 置为 `archived`，新建 `AgentSession` 并以 `topics.rolling_summary` 冷启动。

---

## 6. 核心业务流程

### 6.1 收到用户消息（主流程）

1. **路由**：过 Fast-Path 或 Decision Model，确定 `topic_id` 与 `needs_heavy_agent`，写入 `messages` 与 `routing_decisions`。
2. **HITL 优先检查**：查询该 `topic_id` 下是否存在 `status = 'waiting_input'` 的 `session_runs`：
* **若存在**：将旧 Run 置为 `completed`，直接携带其 `external_session_id` 和用户回复，派生新 `SessionRun` 恢复执行。
* **若不存在**：根据 `needs_heavy_agent` 决定走轻量对话还是派发重型任务。


3. **派发重型任务**：
* 查找该 `topic_id` 下 `status = 'active'` 的 `agent_sessions`（无则新建）。
* 打包最小完备上下文（`rolling_summary` + 最近 5 条消息 + `heavy_task_brief`），提交 `TaskManager` 创建 `SessionRun`。
* 前台 PA 立即返回轻量确认消息，挂载进度卡片。



### 6.2 任务完成回写（Write-back）

1. 更新 `session_runs` 状态、产出物与耗时字段。
2. 若后台输出冗长，由轻量模型提炼为 <=200 字的口语化结论（附完整报告折叠链接），以 `role = 'assistant'` 写入 `messages` 表。
3. 通过 SSE 推送新消息；若用户当前停留在其他 Topic，前端仅在侧边栏亮起未读红点，不打断当前对话。
4. 异步触发原 `topic_id` 的 `rolling_summary` 重算，将任务结论纳入长期记忆。

---

## 7. 接口契约（V1 最小集合）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/messages` | 发送用户消息，触发路由、轻量回复或后台 `SessionRun` 派发 |
| `PATCH /api/messages/{id}/topic` | 手动纠错：将消息改绑至目标 Topic，并触发摘要重算与审计标记 |
| `GET /api/topics` | 获取话题列表（支持按 `status` 过滤及未读状态） |
| `GET /api/topics/{id}/messages` | 获取指定话题的消息流及关联的 `SessionRun` 卡片状态 |
| `POST /api/topics/{id}/sessions/reset` | 归档当前 `AgentSession`，强制下次重型任务轻装冷启动 |
| `POST /api/runs/{id}/cancel` | 取消运行中或排队中的 `SessionRun`（触发进程组强杀） |
| `GET /api/events` | 全局 SSE 通道，推送 `progress` 卡片刷新、`completed` 回写及跨话题提醒 |

---

## 8. V1 实施路线建议

1. **Phase 1（核心骨架）**：建好 5 张表（SQLite 起步即可），跑通 `FastAPI + TaskManager + ClaudeCodeAdapter` 的无头执行与 SSE 进度推流。
2. **Phase 2（自动路由）**：接入 Decision Model 与三段式上下文组装，上线 Fast-Path 规则与低置信度门控。
3. **Phase 3（记忆与纠错）**：补齐后台异步 `rolling_summary` 刷新任务、前端一键改归属纠错，以及 Pi (`--mode rpc`) / Codex 的 Adapter 扩展。