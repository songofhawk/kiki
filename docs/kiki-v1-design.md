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

## 二、 数据库表结构 DDL (核心 SQLite 表)

```sql
-- 1. 话题表 (Topics)
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT CHECK(status IN ('active', 'archived', 'deleted')) DEFAULT 'active',
    unread INTEGER DEFAULT 0,
    workspace_dir TEXT, -- 话题专属自动子目录 (<kiki_workspace>/topics/<topic_id>)
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

-- 6. 工作空间目录挂载表 (WorkspaceMounts)
CREATE TABLE IF NOT EXISTS workspace_mounts (
    id TEXT PRIMARY KEY,
    scope TEXT CHECK(scope IN ('global', 'topic')) NOT NULL, -- 主信息流或单话题
    topic_id TEXT,
    path TEXT NOT NULL,
    label TEXT,
    is_primary INTEGER DEFAULT 0, -- 是否作为该作用域下 Agent 的主工作目录 (cwd)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);
```

### 工作空间与跨 Agent 目录协同机制
* **Kiki 专属工作空间根目录**：默认位于 `~/.kiki/workspace`（可通过环境变量 `KIKI_WORKSPACE_ROOT` 覆盖），与 Kiki 源码仓库完全解耦。自动初始化以下布局：
  * `rules/`：全局规则文件，供 Kiki 与所有外部 Agent 共享遵循；
  * `shared/`：主信息流及跨话题共享资源；
  * `topics/<topic_id>/`：每个话题自动创建的专属子目录，内含 `artifacts/`（任务产物）与 `files/`（协作工作文件）。
* **用户自定义目录挂载**：支持为主信息流（`scope='global'`）及单个话题（`scope='topic'`）添加本地目录。当话题指定了外部目录时，Agent 默认以该外部目录作为主工作目录（`cwd`），同时将话题专属子目录、`shared/`、`rules/` 及其他挂载目录作为附加目录透传。
* **跨 Agent 透传协议**：
  1. 内置 `WorkspaceToolExecutor` 按任务动态绑定当前话题的允许目录集，产物归档至话题 `artifacts/`；
  2. 外部 CLI Agent 通过进程 `cwd`、原生多目录参数（如 Claude Code / Codex 的 `--add-dir`）、标准化环境变量（`KIKI_WORKSPACE_ROOT`、`KIKI_TOPIC_DIR`、`KIKI_ARTIFACTS_DIR`、`KIKI_SHARED_DIR`、`KIKI_RULES_DIR`、`KIKI_PRIMARY_DIR`、`KIKI_EXTRA_DIRS`）以及 Prompt 工作区声明四重透传。

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

原始手绘视觉参考稿存放于 `reference/kiki`（只读源素材）；工程化生成的正视图/多角度手绘序列帧原图存放于 `assets/sprites/`，经去背与重心对齐后的透明动态 WebP（`.webp`）输出至 `assets/kiki/` 与 `frontend/public/kiki/`。同时，界面中的「客户头像」由独立组件 `UserAvatar` 承载（支持预设风格与自定义头像上传），与 Kiki 的全身伴宠形象完全解耦。

| 系统状态 / 场景 | Kiki 姿态编码 (`KikiPose`) | 视角与动态表现（透明多帧动态 WebP） | 前端展示位置与交互行为 |
| :--- | :--- | :--- | :--- |
| **Idle（日常待机）** | `idle` | 正视图全身坐姿：自然呼吸、眨眼、耳尖轻颤与尾巴摇摆 | 常驻于左下角伴宠小窝、空状态欢迎舞台与已完成助手消息左侧。 |
| **Idle Lazy（慵懒小动作）** | `idle_lazy` | 正面微侧全身：困倦眯眼 $\rightarrow$ 张嘴打大哈欠 $\rightarrow$ 趴卧打盹冒鼻泡 | 左下角伴宠小窝或欢迎舞台处于待机时定时随机触发（点击摸摸 Kiki 亦可触发）。 |
| **Idle Playful（调皮小动作）** | `idle_playful` | 正视图全身：竖耳歪头 $\rightarrow$ 舔爪洗脸 $\rightarrow$ 伸出肉垫拍屏幕逗弄 | 左下角伴宠小窝处于待机时随机轮换触发，支持用户点击互动击掌。 |
| **Thinking（路由与思考）** | `thinking` | 正视图端坐歪头：托腮思索、眼神流转与头顶思考云朵浮动 | 当 Decision Model 判断路由、Fast-Path 流式生成或低置信度提示时展示。 |
| **Delivering（运送消息/派单）** | `delivering` | 微侧正脸斜向奔跑：戴邮差帽背挎包叼信封，四肢交替迈步飞奔 | 消息发送分流中、`SessionRun` 排队派单（`queued`）及右上角任务完成捷报 Toast 时展示。 |
| **Working（专注干活）** | `working` | 正视图坐于笔记本电脑后：戴报童帽左右爪交替快速敲击键盘、抬头巡检 | 后台重型任务执行中（`running`）时，在 `RunStatusCard` 与左下角伴宠小窝同步展示。 |
| **Waiting Input（人工拍板）** | `waiting_input` | 正视图端坐举爪：挥动肉垫小爪招手、眨眼示意与头顶问号跳动 | HITL 挂起审批卡片与左下角伴宠小窝高亮展示，提醒用户一键拍板。 |

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

### 5. HITL 人工拍板与服务器桌面接管卡片
当 `status == 'waiting_input'` 时：
* **常规审批拍板（`kind == 'approval'`）**：触发高亮黄色边框卡片，卡片左侧为举爪 Kiki (`waiting_input`)，展示提示文本与快捷选项按钮（如 `[ 同意重启 ]` `[ 拒绝并终止 ]`），点击直接推送 `/resume` 信号。
* **服务器桌面接管（`kind == 'desktop_takeover'`，模仿 Grok Bot）**：
  * 当内置 Agent 遇到登录/2FA、图形验证码、OAuth 授权或桌面弹窗等必须人工介入的场景时，调用 `request_desktop_takeover` 挂起任务，并在卡片上渲染 `[ 接管服务器桌面 ]` 主按钮与操作指引；
  * 用户也可随时点击顶部工具栏「桌面接管」按钮主动查看或接管 Kiki 服务器桌面；
  * 后端通过 `/api/desktop/status`、`/api/desktop/sessions` 及 `/api/desktop/sessions/{id}/ws` 提供独占会话锁、短时令牌鉴权、只读/控制模式切换、多显示器与 FPS 调节、剪贴板同步及空闲自动回收（V1 原生支持 macOS CoreGraphics + `screencapture` 与测试用 Mock 驱动）；
  * 在接管弹窗内处理完毕后点击 `[ 我已处理完成，交还给 Kiki ]`，自动结束接管会话并恢复挂起的 `SessionRun`。

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