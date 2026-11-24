# Kiki — 一个有温度的 Personal Agent

> 从多机工程控制面出发，向“好用的个人智能体”做一次减法与重构。

---

## 这只小猫从哪里来

**Kiki** 最初源自 [songofhawk/dsh-alpha](https://github.com/songofhawk/dsh-alpha) 的一个分支。

`dsh-alpha` 是围绕 DeepSeek Harness（DSH）构建的多机、多 Agent 编排与控制平台，解决的是跨笔记本、构建机、GPU 节点以及多种编码 CLI 之间的任务路由、流式调度与安全审批问题。它很强大，但它的视角是**工程控制面**——面向的是“如何调度一群机器与进程”。

当我们把目光从集群与控制面收回到**个人日常使用**时，一个更朴素也更棘手的问题浮现了出来：

> **如果一个 Agent 每天都陪在你的桌面上，它到底应该怎样与你对话、替你干活，又不至于把你的注意力撕成碎片？**

带着这个问题，我们从 `dsh-alpha` 分叉出了 Kiki，并结合对近期几类代表性 Personal Agent 与前沿架构的观察，重新设计了它的交互与内核。

---

## 站在谁的肩膀上

### 1. DeepSeek Super-Agent 论文：注意力才是真正的瓶颈

DeepSeek Harness 团队在技术报告 *Exploring Super-agent Design: Towards a More Autonomous Agent System*（及其原型 `dsh-superagent`）中提出了一个非常直击本质的判断：**制约 AI 辅助生产力的瓶颈，往往不再是模型本身的能力，而是人类的注意力带宽**。一个人同时盯住 3 到 5 个活跃的 Agent 会话就已经接近极限，传统“开十几个终端或聊天窗口、人肉来回切上下文”的范式注定无法规模化。

论文给出的解法是 **Router 与 Worker 的分层异步架构**：由一个常驻的 Router 负责接收输入、甄别意图与分流派单；背后按需拉起隔离的 Worker 会话并发执行重型任务，人类只在需要拍板或审阅结果时介入。

Kiki 将这一思想落到了个人对话场景中，演化出**“前台轻量陪聊（Fast-Path）+ 后台异步重型任务（Heavy-Path）”的双径架构**：
- 你发出的每一句话，先经过轻量级 **Decision Model（Router）** 做意图判定与话题归类；
- 闲聊、问候与轻量问答走 **Fast-Path**，毫秒级流式回应；
- 代码重构、深度调研、多步工具执行等复杂诉求则进入 **Heavy-Path**，转化为后台 `SessionRun` 队列，由 `TaskManager` 异步调度内置的 `gemini_agent` 或外部专业 Agent（如 Codex、Claude Code、Antigravity、DeepSeek Harness 等）在后台默默推进。

### 2. Meta Muse 与 xAI Grok Bot：在轻盈与可控之间找平衡

在产品交互上，Kiki 深受 **Meta Muse** 与 **xAI Grok Bot** 这类新一代 Personal Agent 的启发，也尝试去解决它们各自带来的使用摩擦：

- **向 Muse 学“信息克制”**：好的个人助手不该像命令行一样把满屏日志糊在脸上。Kiki 采用了**三层渐进式任务卡片（Three-layer Run Card）**——默认只用一行 `48px` 的紧凑摘要告诉你“正在做什么”；点击才展开步骤时间线与产物列表；只有极客排障时才点开右侧的原始终端日志抽屉。
- **向 Grok Bot 学“丝滑接管”**：自主 Agent 最怕两件事——要么擅自做主把事情搞砸，要么卡住后让你重新解释半天上下文。Kiki 为后台任务设计了显式的 **HITL（Human-in-the-Loop）人工拍板卡片**，当任务进入 `waiting_input` 状态时，关键决策被封装成一键快捷按钮，点一下即可唤醒后台继续执行。
- **解决共同痛点：“全局流 + 自动 Topic 胶囊”**：传统聊天工具要求你每次聊新事都先点一次 `New Chat`，否则单窗口里很快就会上下文混杂、记忆串台。Kiki 反其道而行之——默认只有一个 **All Stream（全局流）**，想到什么随时发；Decision Model 会在后台无感地为每条消息打上 `#Topic` 胶囊。置信度不够时，它不会打断你，只在胶囊旁露出思考小图标，允许你一键拆分成新话题或拖拽改绑；而当你点击任意胶囊时，整个界面又会平滑收拢为该话题的 **Topic Focus（专注模式）**，后续对话自动锁定上下文、绕过路由直达目标。

---

## 我们对 Personal Agent 交互的一点理解

工具可以绝对理性，但**个人智能体（Personal Agent）**每天和同一个人朝夕相处，它应该有一点**呼吸感与生命感**。

在 Kiki 里，我们没有用一个冷冰冰的加载转圈来表示“系统正在处理”，而是请来了一只手绘风格的小猫 **Kiki** 作为整个系统的状态映射：

- **日常待机（`idle` / `idle_lazy` / `idle_playful`）**：它安安静静蹲在左下角的小窝里，偶尔打个大哈欠冒鼻泡，或者竖起耳朵舔爪、伸出肉垫拍拍屏幕；
- **路由思考（`thinking`）**：Decision Model 正在帮你分辨话题与路径时，它会端坐歪头、托腮思索；
- **派单奔跑（`delivering`）**：消息分流或重型任务入队时，它戴上邮差帽、叼着信封斜向飞奔；
- **专注干活（`working`）**：后台 Agent 执行工具链期间，它戴上报童帽坐在笔记本电脑前左右爪交替敲键盘；
- **请求拍板（`waiting_input`）**：遇到关键决策挂起时，它会举起肉垫小爪向你招手，头顶跳出小问号提醒你点一下确认。

当你正在 `#话题 B` 聊天，而后台 `#话题 A` 的重型任务悄悄跑完时，屏幕右上角会滑出一张轻量捷报 Toast，左侧话题列表亮起一颗小猫爪红点——你不用守着它，它干完活自然会来叫你。

---

## 仓库结构与快速开始

### 核心目录

- `backend/`：FastAPI + SQLite + `google-genai`（Interactions API）后端，包含 `router.py`（Decision Model 与 Fast-Path）、`task_manager.py`（后台调度）、`dream.py`（闲置记忆整理）、`search.py`（多源网页检索）以及 `adapters/`（多重型 Agent 适配器）。
- `frontend/`：React + TypeScript + Tailwind CSS 前端，包含全局流 / 话题专注双态布局、三层渐进式任务卡片与手绘 Kiki 动态 WebP 伴宠状态机。
- `docs/kiki-v1-design.md`：[Kiki V1 设计与开发全景文档](docs/kiki-v1-design.md)。
- `reference/kiki/` & `assets/`：原始手绘视觉参考与工程化序列帧素材。

### 本地运行

1. **配置环境变量**
   ```bash
   cp .env.example .env
   # 在 .env 中填入 GEMINI_API_KEY（以及可选的外部 CLI Agent / 检索 API 配置）
   ```

2. **启动后端**
   ```bash
   cd backend
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```

3. **启动前端**
   ```bash
   cd frontend
   npm install
   npm run dev
   ```
