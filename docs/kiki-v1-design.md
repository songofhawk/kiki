# Kiki (Personal Agent) V1 设计与开发全景文档

> **项目定位**：基于“前台轻量陪聊 + 后台异步重型 Agent”双径架构的个人智能体系统。前台通过 Decision Model 实现对话的无感自动归类与 Topic 分流；后台异步执行重型任务，并通过交互卡片与手绘风格小猫 Kiki 进行状态联动。

---

## 一、 系统整体架构概览

```
                        +----------------------------+
                        |     用户输入 (User Input)   |
                        +--------------+-------------+
                                       |
                                       v
                        +----------------------------+
                        |  Decision Model (Router)   |
                        |   判断意图 / 分流 Topic     |
                        +--------------+-------------+
                                       |
                +----------------------+----------------------+
                |                                             |
                v                                             v
     【Fast-Path / 轻量路线】                       【Heavy-Path / 重型路线】
   (直接前台回复 / 挂载 Topic)                     (创建 SessionRun 放入队列)
                |                                             |
                v                                             v
     返回用户聊天气泡 (Kiki 处于 Idle)             后台 TaskManager 调度执行 Agent
                                                              |
                                                              v
                                                向前端 SSE/WS 推送 SessionRun 状态
                                                (Kiki 处于 Running / Waiting Input)
```

---

## 二、 数据库表结构 DDL (5 张核心 SQLite 表)

```sql
-- 1. 话题表 (Topics)
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT CHECK(status IN ('active', 'archived', 'deleted')) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 消息记录表 (Messages)
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    role TEXT CHECK(role IN ('user', 'assistant', 'system')) NOT NULL,
    content TEXT NOT NULL,
    metadata JSON, -- 存放路由置信度 confidence、关联的 session_run_id 等
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

-- 3. 后台重型任务运行表 (SessionRuns)
CREATE TABLE IF NOT EXISTS session_runs (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    trigger_message_id TEXT NOT NULL,
    agent_type TEXT NOT NULL, -- 如 'gemini_agent' (默认内置), 'claude_code', 'pi'
    status TEXT CHECK(status IN ('queued', 'running', 'waiting_input', 'completed', 'failed', 'cancelled')) DEFAULT 'queued',
    current_step TEXT,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    FOREIGN KEY(trigger_message_id) REFERENCES messages(id) ON DELETE CASCADE
);

-- 4. 任务步骤日志表 (StepLogs)
CREATE TABLE IF NOT EXISTS step_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    action_type TEXT NOT NULL, -- 如 'tool_call', 'shell_exec', 'file_edit'
    detail TEXT NOT NULL,
    status TEXT CHECK(status IN ('success', 'failed', 'in_progress')) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES session_runs(id) ON DELETE CASCADE
);

-- 5. 任务产出物表 (Artifacts)
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL, -- 如 'code', 'markdown', 'pdf', 'image'
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES session_runs(id) ON DELETE CASCADE
);
```

---

## 三、 大模型接入方案与 Decision Model 路由设计

### 1. 模型选型与 SDK 接入规范
* **默认模型**：统一优先采用 **Gemini 3.8 Flash**（Model ID：`gemini-3.8-flash`，支持 1M 输入上下文、64K 最大输出、Structured Outputs、Function Calling 及分级推理控制）。
* **免费额度（Free Tier）与演进**：
  * 通过 [Google AI Studio](https://aistudio.google.com/) 创建未绑定结算账户的项目 API Key，即可在 **Free Tier（免费层）** 的 Standard 模式下零成本调用 `gemini-3.8-flash`（输入、输出、思考 Token 及 Context Caching 均免费），非常适合个人 Agent 原型与日常轻量使用。
  * **免费层边界注意**：
    1. 受项目级速率配额（RPM / TPM / RPD）限制，触发限流时返回 `429 RESOURCE_EXHAUSTED`；
    2. 免费层**不支持**内置 `google_search` / `google_maps` Grounding 及 Batch/Flex 模式；
    3. 免费层请求内容默认可能用于 Google 产品改进（Interactions 默认服务端保留 1 天，Kiki 自建 SQLite 历史可设 `store=False`），切勿直接传入高敏生产密钥。后续绑定结算升级至 Paid Tier 后自动解除训练用途并解锁更高配额。
* **SDK 与配置隔离**：
  * 后端采用最新官方 **`google-genai >= 3.2.0`** Python SDK，统一使用 GA 版 **Interactions API**（`client.aio.interactions.create`，替代旧版 `generate_content`）以适配 FastAPI 异步与 SSE 流。
  * 密钥与模型名通过本地 `.env` 注入（严禁纳入版本控制）：
    ```ini
    GEMINI_API_KEY=your_ai_studio_api_key
    GEMINI_MODEL=gemini-3.8-flash
    ```

### 2. Kiki 三条核心调用链路配置
仅需单个 `GEMINI_API_KEY`，即可驱动 Kiki 前后台完整闭环：

| 调用链路 | 模型 ID | `thinking_level` | 关键特性与 Interactions API 调用方式 |
| :--- | :--- | :--- | :--- |
| **① Decision Model（意图路由）** | `gemini-3.8-flash` | `low` | `client.aio.interactions.create` + `response_format={"type": "text", "mime_type": "application/json", "schema": RouteDecision.model_json_schema()}`，低延迟稳定输出路由 JSON（注：`3.8-flash` 支持 `low`/`medium`/`high`，不支持 `minimal`）。 |
| **② Fast-Path（前台轻量陪聊）** | `gemini-3.8-flash` | `low` | `client.aio.interactions.create(..., stream=True)` 监听 `step.delta` 文本增量事件，注入 Kiki 小猫性格 `system_instruction` 与当前 Topic 上下文，通过 SSE 实时推送聊天气泡。 |
| **③ Heavy-Path（后台重型任务）** | `gemini-3.8-flash` | `medium` / `high` | 默认内置 `gemini_agent` 执行器，基于 `tools` Function Calling（受控文件读写、Shell 执行、`request_human_approval` 人工拍板挂起）处理 `function_call` / `function_result` 步骤循环，每步落库 `step_logs` 并推送 SSE；同时保留 `agent_type` 扩展对接 `claude_code` 等外部 Agent 的能力。 |

### 3. Decision Model 路由逻辑与 Prompt

每当用户在全局流发送新消息时，先调用决策模型判断其所属 Topic 与执行路径：

```text
[System Prompt]
你是一个智能意图分流与 Topic 判定引擎。
你的职责：分析用户的新消息，决定它是属于现有的某个 Topic、需要新建一个 Topic，还是仅为前台轻量闲聊。

现有活跃 Topic 列表：
{{ACTIVE_TOPICS_JSON}}

请输出以下格式的 JSON，切勿包含其他多余字符：
{
  "path": "fast" | "heavy",
  "topic_action": "reuse" | "create" | "none",
  "topic_id": "Existing Topic ID if reuse, else null",
  "new_topic_title": "Short title if create, else null",
  "confidence": 0.0 - 1.0,
  "reasoning": "简短判定理由"
}

判定规则：
1. 若输入为简单的问候、感叹、前台对话交互，path 为 'fast'，topic_action 为 'none'。
2. 若输入涉及代码重构、长文生成、数据分析、多步骤操作，path 为 'heavy'。
3. 置信度 confidence < 0.65 时，前端将展示 Kiki 思考并提供无感纠错胶囊。
```

---

## 四、 Kiki 猫咪 UI 形象与状态机映射

视觉图源文件存放于 `reference/kiki`（包含 2x2 网格手绘风高性格小猫）。

| 系统状态 / 事件 | Kiki 状态编码 | UI 形象剪裁区域 | 前端展示位置与视觉行为 |
| :--- | :--- | :--- | :--- |
| **Idle（待机）** | `idle` | 左上角（揣手手趴姿） | 常驻于底部输入框右侧或侧边栏底部，呈静止趴姿，偶现极轻微呼吸/眨眼微动。 |
| **Routing / Thinking（路由与思考）** | `thinking` | 右上角（傲娇歪头+思考小云朵） | 当 Decision Model 正在路由判断，或 Fast-Path 正在生成前台回答时展示。 |
| **Running（后台跑腿干活）** | `running` | 左下角（戴邮差帽叼信封奔跑） | 嵌入在消息流的 `SessionRun` 进度卡片左侧，配有小腿微动动画与 SSE 原地刷新文案。 |
| **Waiting Input（HITL 人工挂起）** | `waiting_input` | 右下角（端坐举爪+头顶问号） | 高亮暂停卡片左侧，举爪提示用户选择快捷按钮或拍板授权。 |

---

## 五、 产品深度交互设计规范

### 1. 竞品扬长避短策略
* **吸收 Meta Muse 优势**：采用“三层状态披露（Three-layer Status）”，绝不拉长默认聊天气泡；把冗余 log 隐蔽在点击下钻卡片中。
* **吸收 xAI Grok Bot 优势**：丝滑的 HITL（Human-in-the-Loop）接管，将决策项封装成一键快捷按钮。
* **解决共同痛点**：通过 Decision Model 的自动 Topic 胶囊，彻底解决 Muse 主/支线割裂以及 Grok Bot 单窗口内上下文混乱的问题。

### 2. 双态空间布局（全局流 + Topic Focus）
* ** All Stream (默认全局流)**：只有一个中央对话流，用户无需先点“New Chat”，随时输入。每条消息气泡右上角自动带有一个 `#Topic胶囊`。
* ** Topic Focus Mode (话题专注模式)**：点击消息气泡上的 `#Topic胶囊` 或侧边栏某一 Topic，界面平滑收拢为该 Topic 的专属历史流。此时发送消息自动锁在该 Topic，绕过 Decision Model。

### 3. 零打断纠错机制 (Zero-Interrupt Correction)
* **高置信度（Confidence $\ge$ 0.65）**：静默打上 `#Topic胶囊`，正常流转。
* **低置信度（Confidence $< 0.65$）**：系统自动分配至最匹配话题，并在胶囊旁显示 `Thinking Kiki` 微图标。鼠标悬停/点击胶囊触发轻量下拉菜单：
  * `[ 拆为新话题 ]`
  * `[ 移至其他话题... ]`
* **拖拽兜底**：任何消息气泡均可拖拽至左侧 Topic 列表直接改绑。

### 4. 三层渐进式任务卡片 (Three-Layer Run Card)
针对 Heavy-Path 产生的 `SessionRun`：
* **L1 默认层（高度 48px）**：
  * 左：奔跑 Kiki 图像 (`running`)
  * 中：实时刷新的单行 `current_step`（如 `正在读取 error.log...`）
  * 右：运行耗时计时器与 `取消` 按钮。
* **L2 展开层（点击卡片展开）**：
  * 展示已执行步骤的时间线列表（Step Logs）。
  * 展示当前已生成的产物列表（Artifacts）。
* **L3 极客层（点击右上角 `>_` 终端图标）**：
  * 右侧弹出 Log Drawer，展示后台 Agent 原生的 Terminal NDJSON/Shell 标准输出流。

### 5. HITL 人工拍板卡片
当 `status == 'waiting_input'` 时：
* 触发高亮黄色边框卡片，卡片左侧为举爪 Kiki (`waiting_input`)。
* 提示文本：“Kiki 需要你拍板：是否允许重启 3 个异常容器？”
* 卡片底部渲染快捷选项按钮（如 `[ 同意重启 ]` `[ 拒绝并终止 ]`）。用户点击按钮直接推送 `--resume` 信号，无需手打文本。

### 6. 跨话题异步通知
当用户正在 Topic B 聊天，而后台 Topic A 的 `SessionRun` 完成时：
* 屏幕右上角滑出轻量 Toast 通知：“`#排查502` 已搞定：定位到上游连接池耗尽”，停留 3 秒消失。
* 左侧 Topic A 列表图标亮起小猫爪未读红点。
* 点击 Toast 可直接跳转到该 Topic。回写到消息流的结论严格控制在 200 字以内。

---

## 六、 前端 4 大核心 React 组件设计

1. **`<KikiAvatar status={currentStatus} size="md" />`**
   * 封装状态：`'idle' | 'thinking' | 'running' | 'waiting_input'`。
   * 负责从 `reference/kiki` 中按坐标裁剪对应的猫咪图像，并附带平滑过渡 CSS 动画。

2. **`<UnifiedMessageList />`**
   * 渲染全局消息流或 Topic 过滤流。
   * 包含消息气泡、`TopicBadge` 以及挂载的 `RunStatusCard`。

3. **`<TopicBadge topic={topic} confidence={confidence} onReassign={...} />`**
   * 渲染彩色 Topic 胶囊。当 `confidence < 0.65` 时显示纠错下拉菜单。

4. **`<RunStatusCard sessionRun={sessionRun} />`**
   * 管理 L1、L2、L3 三层状态的切换折叠，建立与后台 SSE 服务端推送的连接。

---

## 七、 MVP 开发指南 (Antigravity 启动 Prompt)

在 Antigravity 中启动该项目开发时，请使用以下指令：

```text
@docs/kiki-v1-design.md @reference/kiki
请完整阅读设计全景文档，并参考素材图片，按以下顺序开始构建：

1. 项目结构初始化：
   - 创建 backend/ (FastAPI + SQLite + `google-genai` SDK)，提供 `.env.example` 配置 `GEMINI_API_KEY` 与 `GEMINI_MODEL=gemini-3.8-flash`
   - 创建 frontend/ (React + Tailwind CSS)
   
2. 后端开发：
   - 在 backend/ 运行 5 张 SQLite 表建表 DDL（数据库文件置于 `data/` 目录）。
   - 封装统一大模型客户端 (`llm.py`)，默认接入 `gemini-3.8-flash` 异步接口。
   - 实现 Decision Model 路由模块 (`router.py`)，基于 Structured Outputs (`thinking_level="low"`) 输出置信度与 Topic 方案，并实现 Fast-Path 流式陪聊回复。
   - 实现 TaskManager 调度器 (`task_manager.py`)、默认内置 `gemini_agent` 多步工具循环 (`thinking_level="medium"`) 与 SSE 广播模块。

3. 前端 UI 开发：
   - 开发 `KikiAvatar` 组件，实现 4 种猫咪状态的图幅裁剪与显示。
   - 开发 `UnifiedMessageList` 与 `TopicBadge`（含低置信度纠错菜单）。
   - 开发 `RunStatusCard` 支持 L1/L2/L3 三层展开。
```