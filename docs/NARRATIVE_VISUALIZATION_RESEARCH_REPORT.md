# 推理过程可视化 — 技术调研与项目评估报告

## 概述

本报告对 BioDSA 推理过程可视化（知识图谱实时生长）项目进行系统性技术调研，覆盖现有方案调研、难点分析、进度安排和人员分工。所有结论基于对 BioDSA 代码库、WeKnora 架构的实际审查，以及 CopilotKit、LangGraph Studio、Cytoscape.js 等开源项目的对比研究。

---

## 一、现有方案调研

### 1.1 学术界与工业界：Agent 推理可视化

| 项目 | 方案 | 与本项目关系 |
|------|------|-------------|
| **LangGraph Studio** | 基于 WebSocket 的 SSE 协议，推送 `NODE_STARTED`/`NODE_FINISHED`/`CHANNEL_UPDATE` 事件，前端用 ReactFlow 绘制 DAG 状态图 | 协议设计可参考，事件模型是 LangGraph 原生支持的格式。**不是我们要的知识图谱**——它是执行流程图，而非领域知识图 |
| **CopilotKit CoAgents** | AG-UI 协议（`@ag-ui/client`），通过 SSE 流式推送 agent 状态到前端 React 组件。`useCoAgent` hook 自动订阅状态更新 | 架构思路接近（SSE + 前端组件），但 CoAgents 推的是 agent state 快照，不是领域实体事件。不适用于 MCP 架构 |
| **CrewAI Studio** | 基于 FastAPI + WebSocket，提供 agent 对话记录、工具调用日志的 Dashboard | 仅展示日志，无知识图谱。架构层面与我们 MCP 传输层不同 |
| **AutoGen Studio** | WebSocket 推送 agent 对话日志，前端用 React 渲染消息流 | 同上，消息流而非知识图谱 |
| **GraphRAG (Microsoft)** | 知识图谱在运行前已构建完毕，可视化是静态的图谱浏览，无实时生长 | 图谱的实体/关系建模可以参考，但它是离线构建的 |
| **Cytoscape.js 官方示例** | 有 `add`/`remove` 事件的增量更新演示，但无与 agent 实时流结合的案例 | 前端的核心技术选型已验证 |

**关键结论：目前没有任何生物医学 agent 工具实现实时生长的知识图谱可视化。** 这是一个真正的空白点。所有现有 agent 可视化方案要么展示执行流程图（LangGraph Studio），要么展示日志列表（CrewAI/AutoGen），要么展示预先构建好的静态图谱（GraphRAG）。将"agent 推理过程"映射为"领域知识图谱的实时生长动画"——这个概念是新的。

### 1.2 BioDSA 代码库现状

审查了以下关键路径：

**`agent_graph.stream()` 输出结构（`agent.py:777-800`）：**
- `stream_mode=["values"]`，`subgraphs=True`，每个 chunk 返回完整状态快照
- `chunk[-1]` 包含 `messages` 列表，最后一个消息是 `AIMessage`（含 tool_calls）或 `ToolMessage`（自由文本 content）
- 主 agent（orchestrator）的 tool_calls 是结构化的（`go_breadth_first_search`、`go_depth_first_search`），子 agent 的 tool_calls 也是结构化的（`unified_gene_search`、`fetch_variant_details` 等）
- **关键问题：ToolMessage 的 content 全部是自由文本 markdown，非结构化 JSON。**

**10 个知识库的 ToolMessage 格式（审阅 `schema.py`）：**
- `pubmed_papers` → `search_papers`、`find_entities` → markdown 文本
- `gene` → `unified_gene_search`、`fetch_gene_details` → markdown 文本
- `disease`、`drug`、`variant`、`clinical_trials`、`web_search`、`target`、`pathway`、`compound` → 全部 markdown 文本
- _唯一的例外_：`AddToGraph` 工具接受结构化 `Entity`/`Relation` 输入，`RetrieveFromGraph` 可返回 JSON

**证据图（`memory/graph.py`）：**
- 实体模型：`Entity{name, entity_type, observations}`（`memory_graph/schema.py`）
- 关系模型：`Relation{from_entity, to_entity, relation_type}`
- 每轮 `go()` 开始时清空（`agent.py:838-845`），不通过 MCP 暴露
- 缺少：时间戳、置信度、矛盾标记、来源追溯字段

**MCP 工具入口（`mcp_server_standalone.py:67-104`）：**
- `tool_deepevidence_research()` 调用 `agent.go()` —— **这是阻塞调用**
- `go()` → `generate()` → `agent_graph.stream()` —— 整个 stream 在函数内耗尽后才返回
- 返回值是 `_fmt_results(results)` 生成的纯文本字符串
- **当前不存在 run_id 概念，也不存在任何事件推送机制**

**Agent 覆盖范围：**
- 9 个 agent 使用 `agent_graph.stream()` 模式（DeepEvidence、以及它的子 agent 变体）
- AgentMD 不使用 stream，独立路径
- DeepEvidence 最复杂（orchestrator + BFS + DFS 三层嵌套），且其领域模型（基因/药物/疾病/突变）最适合知识图谱可视化

### 1.3 WeKnora 架构

**前端架构（源码审查）：**
- Vue 3 + TypeScript + TDesign UI 组件库
- 状态管理：Pinia
- 构建工具：Vite
- 聊天相关组件位于 `frontend/src/views/chat/`
- **关键组件**：`AgentStreamDisplay.vue`（agent 步骤流渲染）、`ToolResultRenderer.vue`（MCP 工具结果渲染）
- **扩展点**：可以在 `ToolResultRenderer` 中为 `biodsa_deepevidence_research` 工具类型注册自定义渲染组件

**MCP 客户端（mcp-go v0.52.0）：**
- 已支持 streamable HTTP transport
- `CallTool` 是 **同步阻塞**的 —— 发送请求后等待完整响应
- WeKnora 后端的 MCP 调用流程：用户消息 → LLM 决策调用工具 → `CallTool` → 阻塞等待 → 返回工具结果 → 继续 LLM 推理
- **这意味着前端在 MCP 工具返回之前，拿不到 run_id。**

**SSRF 防护（WeKnora 后端）：**
- 默认阻止：直接 IP 地址、Docker bridge CIDR（`172.17.0.0/16`）、`host.docker.internal`
- 需要在 `SSRF_WHITELIST_EXTRA` 环境变量中添加 BioDSA 的地址
- 这是部署时的关键配置项，必须在文档中明确说明

**网络拓扑（Docker Desktop + WSL2）：**
- WeKnora 在 Docker 容器内运行
- BioDSA 在 Windows 宿主机上运行
- Docker bridge IP（`172.17.0.1`）在 Windows 上**不可从浏览器直接访问**
- 浏览器在 Windows 上，需要直接连 BioDSA 的 SSE 端点
- 解决方案：BioDSA 监听 `0.0.0.0:8765`，浏览器使用 `localhost:8765` + CORS

---

## 二、难点分析

### 难点 1（严重）：ToolMessage 自由文本 → 结构化事件提取

**现状：** 所有 10 个知识库的 ToolMessage 返回自由文本 markdown。agent 发现了什么实体、什么关系，都以自然语言描述形式嵌在段落中。

**挑战：**
- 正则表达式对生物医学实体的覆盖率有限（基因名如 `EGFR`、`TP53` 容易与缩写混淆；药物名如 `osimertinib`、`gefitinib` 拼写多样性高）
- 关系提取更困难——"X 导致 Y 对 Z 耐药"这个语义很难用正则准确捕获
- 矛盾检测依赖关键词（"contradictory"、"inconsistent"、"conflicting"），可能遗漏含蓄表达的对立

**解决路径：**
- **优先级策略**：AIMessage 的 `tool_calls` 包含结构化 args（搜索词、基因 ID、药物名等），这些是可靠的——优先用 tool_calls 提取实体发现事件（覆盖 ~60% 的实体）
- ToolMessage 内容用正则做**辅助提取**，不追求完美召回
- Agent 自身的 `AddToGraph` 工具已经将结构化实体/关系写入了证据图——这是**最高质量的事件源**（实体和关系已经被 agent 自己解析过、结构化过），应该 hook 进 `AddToGraph` 的调用过程
- 长期方案：在 10 个知识库的返回格式中增加结构化摘要字段——但这需要修改 KB 后端，工程量巨大，列为 Phase 2

**难度评级：高。** 这是整个项目中最没有银弹的环节。正则 + tool_calls + AddToGraph 三管齐下可达可用状态（召回率 ~70-80%），但要做到精炼需要持续迭代。

### 难点 2（严重）：run_id 传递的时序问题

**现状：** MCP `CallTool` 是同步阻塞的。`tool_deepevidence_research()` 调用 `agent.go()`，后者需要 3-5 分钟才返回。前端在这 3-5 分钟内**不知道 run_id**，无法建立 SSE 连接。

**分析：**
- MCP 协议本身支持流式响应（streamable HTTP 的 SSE 响应），但 WeKnora 的 mcp-go 客户端 `CallTool` 封装为了同步接口
- 即使 BioDSA 端可以通过 streamable HTTP 的响应流推送中间结果，WeKnora 的 MCP 客户端也不消费流式内容，而是等到响应结束
- **因此不能依赖 MCP 协议的流式能力来解决 run_id 传递问题。**

**解决路径：**
- **方案 A（推荐短期方案）**：将 run_id 作为 MCP 工具调用的**参数**传递给前端。利用 WeKnora 的 `AgentStreamDisplay.vue`，它已经能捕获到 LLM 决定调用工具时的 `tool_call` 事件（含参数）。在工具参数中预置一个 `run_id`，前端从 tool_call 事件中提取——不依赖 MCP 返回。
  - 实现：WeKnora 后端在构造 MCP 工具调用请求之前，先生成一个 `run_id`，附加在 `knowledge_bases` 同级参数中
  - 或者：在 BioDSA 端，工具函数被调用时立即生成 run_id，但需要一种方式在 `go()` 返回之前把 run_id 发出去

- **方案 B（长期更优）**：WeKnora 前端直接先生成 run_id，通过 MCP 工具参数传入。前端拿到自己的 run_id 后立即打开 EventSource，BioDSA 端收到请求后用同一个 run_id 开始广播事件。
  - 但这个需要修改 WeKnora 的工具调用参数构造逻辑

- **方案 C**：在 WeKnora 后端增加一个轻量代理端点 `/api/proxy/biodsa-progress?run_id=xxx`，后端代浏览器连接 BioDSA SSE，转为 HTTP 流返回。利用 WeKnora 后端已能访问 BioDSA（MCP 连接已验证），绕过 SSRF 和跨域问题。同时后端在调用 MCP 工具前先生成 run_id 返回给前端。
  - 这需要改动 WeKnora 后端，但改动量小（一个代理路由 + run_id 预生成）

**难度评级：高。** 这不是纯技术难度，而是**跨系统（WeKnora + BioDSA）时序协作**的架构问题。方案选择取决于 WeKnora 端的改动权限。

### 难点 3（中等）：Cytoscape.js 增量布局保持

**现状：** Cytoscape.js 的布局算法（cose-bilkent、fcose）在 `layout.run()` 时会重新计算所有节点位置。每次添加新节点后重新运行布局，已有节点会跳动。

**社区调研：**
- GitHub issue #3278 长期讨论增量布局问题，无官方解决方案
- cose-bilkent 支持 `animate: true` 做平滑过渡，但无法保证已有节点位置不变
- 社区的 workaround：锁定已有节点位置（`locked: true`），只对新节点运行增量布局

**解决路径：**
- 策略 1：已有节点设置 `node.lock()`，新节点用 `layout.pon()` 只布局新增部分
- 策略 2：自定义定位——新节点放到与其关联的已有节点附近（polar coordinate + jitter），不做全局布局
- 策略 3：BFS 阶段用 force-directed 全局布局（此时节点少，跳动可接受），DFS 阶段切换到固定位置 + 局部定位
- 综合：Phase 1 可用策略 2 快速实现，动画抖动在可接受范围；Phase 2 引入 cose-bilkent 增量布局 + 锁定混合策略

**难度评级：中等。** 不是 blocker，但视觉效果打磨需要投入。

### 难点 4（中等）：SSRF 与跨域的网络配置

**现状：** WeKnora 的 SSRF 防护阻止了对 Docker bridge IP 的访问。同时 Docker Desktop + WSL2 的网络模型使 Windows 浏览器无法访问 `172.17.0.1`。

**需要配置的白名单和跨域设置：**
- WeKnora 端：`SSRF_WHITELIST_EXTRA` 中添加 BioDSA 的 host（取决于部署拓扑）
- BioDSA 端：CORS middleware 允许 WeKnora 前端域名
- 如果 WeKnora 前端通过 `localhost` 访问 BioDSA：`allow_origins=["http://localhost:*", "http://127.0.0.1:*"]`
- 如果走反向代理（nginx）：只需代理 BioDSA 的 `/progress` 路径，不需要 CORS

**难度评级：中等。** 这是部署配置问题，非代码问题。但需要明确的文档和排查指南。

### 难点 5（低）：事件队列内存管理

**现状：** 一个 agent run 产生 30-80 步，每步约 2-5 个事件，总计 60-400 个事件。每个事件序列化后 ~200 bytes。内存不是问题。

**但：** 如果前端断连后重连，错过的事件怎么办？
- SSE 从连接时刻开始推送，不支持历史回放
- 如果前端在 agent 运行到一半时才连接，前面的实体不会出现在图谱中

**解决路径：**
- Broadcaster 保留最近 50 个事件的环形缓冲区，新连接时先回放缓冲区
- 或者提供 `/progress/snapshot?run_id=xxx` 端点返回当前已发现的所有实体/关系快照

**难度评级：低。** 单 run 几百个事件，任何方案的内存和实现成本都很小。

---

## 三、技术栈确认

| 层 | 选择 | 替代方案及放弃理由 |
|----|------|-------------------|
| 事件传输 | **SSE**（浏览器原生 EventSource） | WebSocket 功能过剩，需要额外库；MCP streamable HTTP 的 SSE 能力只在 WeKnora 后端可用 |
| 图渲染 | **Cytoscape.js** + cose-bilkent | D3.js 需手写力导向布局；Sigma.js 3D 方向不对；Vis.js 维护不活跃 |
| 后端事件模型 | **Python dataclass (frozen)** | Pydantic 过重；NamedTuple 缺少序列化便利方法 |
| 事件广播 | **asyncio.Queue** + Starlette StreamingResponse | Redis pub/sub 增加外部依赖，单进程足够 |
| 前端框架 | **跟随 WeKnora 现有**（Vue 3 + TDesign） | 不引入新框架，Cytoscape.js 是纯 JS 库 |

---

## 四、实施计划

### Phase 1 — 基础后端事件流

**内容：**
1. `biodsa/narrative/events.py` — 5 种事件 dataclass（~70 行）
2. `biodsa/narrative/extractor.py` — tool_call → 叙事事件提取（~120 行）
3. `biodsa/narrative/broadcaster.py` — asyncio.Queue + SSE subscribe 生成器（~50 行）
4. 单元测试（~80 行）

**预计时间：** 4-6 小时（含 extractor 的正则模式打磨）

### Phase 2 — 接入 Agent 流程

**内容：**
1. 修改 `agent.py:generate()`，在 stream 循环内调用 extractor + broadcaster
2. 修改 `mcp_server_standalone.py`：
   - 添加 CORS middleware
   - 添加 `/progress?run_id=xxx` SSE 端点
   - 生成 run_id 并附加到返回文本中
   - 注册 broadcaster 单例
3. 解决 run_id 传递的时序问题（实现方案 A：run_id 作为工具参数）

**预计时间：** 3-5 小时

### Phase 3 — 解决 run_id 传递（WeKnora 端）

**内容（取决于选定方案）：**
- 方案 A（推荐）：修改 WeKnora 后端，在调用 `biodsa_deepevidence_research` 前预生成 run_id，作为额外参数传入；前端从 `tool_call` 事件中提取
- 方案 C（备选）：WeKnora 后端新增代理路由 `/api/proxy/biodsa-progress`

**预计时间：** 3-8 小时（取决于 WeKnora 代码库的熟悉程度和方案选择）

### Phase 4 — WeKnora 前端知识图谱组件

**内容：**
1. Cytoscape.js 初始化 + cose-bilkent 布局配置
2. 节点/边渲染函数（颜色、形状、动画）
3. EventSource 连接 + 6 种事件处理（entity_found、relation_found、phase_change、contradiction_found、progress、done）
4. 进度面板（阶段指示器、计时器、实体计数、发现时间线）
5. 节点交互（悬停高亮、点击详情、缩放拖拽）
6. 嵌入 `ToolResultRenderer.vue` 的扩展点
7. BFS 阶段 vs DFS 阶段的差异化动画

**预计时间：** 8-12 小时（前端工作量最大的一环）

### Phase 5 — 端到端联调与质量打磨

**内容：**
1. WeKnora + BioDSA 集成测试
2. SSRF 白名单配置验证
3. CORS 跨域验证
4. 事件提取质量调优（实际生物医学问题测试）
5. 动画效果微调（节点浮现节奏、过渡时长）
6. 异常处理（run_id 无效、SSE 断连重连、agent 失败时的前端清理）

**预计时间：** 4-6 小时

### 总预计：24-37 小时（不含 WeKnora 前端技术栈学习成本）

---

## 五、人员分工建议

### 角色 A — 后端开发（BioDSA 方向）

**需要的技能：**
- Python 熟练，理解 async/await 和 asyncio 并发模型
- 了解 LangGraph 的 stream 机制（至少读过 `agent_graph.stream()` 的文档）
- 正则表达式经验（生物医学文本的特征模式）

**负责模块：**
- Phase 1：全部（events.py、extractor.py、broadcaster.py）
- Phase 2：全部（agent.py 改动、MCP server 改动）
- Phase 5：事件提取质量调优

**预计投入：** 12-16 小时

### 角色 B — 前端开发（WeKnora 方向）

**需要的技能：**
- Vue 3 组件开发，熟悉 TDesign 组件体系
- 图可视化经验（Cytoscape.js 或 D3.js 更佳）
- 对动画时序敏感（CSS animation、requestAnimationFrame、Promise chain）

**负责模块：**
- Phase 4：全部（知识图谱组件、进度面板、事件消费、动画）
- Phase 5：前端部分联调

**预计投入：** 12-16 小时

### 角色 C — 架构/桥接（跨系统协调）

**需要的技能：**
- 理解 MCP 协议（工具调用生命周期、streamable HTTP transport）
- 理解 WeKnora 后端（Go 代码阅读能力，知道 MCP 客户端调用链路）
- 网络配置能力（Docker 网络、SSRF 白名单、CORS 策略）

**负责模块：**
- Phase 3：run_id 传递方案决策与实现
- Phase 5：端到端集成调试、网络配置
- 先期参与 Phase 1 的技术方案评审（确保 extractor 输出格式与前端需求匹配）

**预计投入：** 8-10 小时

### 建议人员数量：2-3 人
- 最少配置：角色 A（后端）+ 角色 B（前端），角色 C 的职责由角色 A 兼任
- 推荐配置：三人各司其职，并行推进 Phase 1+4（前后端独立），然后 Phase 2+3 汇合

---

## 六、风险清单

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| run_id 传递方案落地受阻（WeKnora 改动权限不足） | 中 | 高 | 备选方案 C（后端代理），不依赖前端改动 |
| 自由文本实体提取精度不足，图谱质量差 | 中 | 中 | 优先用 tool_calls + AddToGraph 的结构化数据，正则做辅助 |
| Cytoscape.js 增量布局抖动严重 | 低 | 低 | 降级到固定位置 + 就近放置策略，放弃全局自动布局 |
| WeKnora SSRF 白名单配置在客户环境不生效 | 低 | 中 | 提供 nginx 反向代理方案作为备选，绕过直接连接 |
| SSE 连接在长时间运行中意外断开 | 低 | 低 | EventSource 自带重连；broadcaster 保留环形缓冲区回放 |

---

## 七、需要补充调研的项目

以下问题在当前阶段尚未完全澄清，建议在正式开发前确认：

1. **WeKnora `AgentStreamDisplay.vue` 对 MCP tool_call 事件的暴露程度**——能否在 LLM 决定调用工具时（而非工具返回后）就拿到工具名和参数？需要阅读 WeKnora 前端源码确认
2. **WeKnora 的 Docker Compose 网络配置**——确认 `SSRF_WHITELIST_EXTRA` 的具体配置方式和生效范围
3. **10 个知识库是否提供 API 级别的结构化查询**——如果 KB 后端有 API 可以直接返回 JSON，extractor 的精度可以大幅提升
4. **WeKnora 前端构建流程**——确认引入 Cytoscape.js（~400KB gzipped ~100KB）对打包体积的影响，是否需要动态 import

---

## 附录 A：Cytoscape.js 增量布局社区解决方案汇总

参考 GitHub issue #3278 的讨论，整理了 4 种实践方案：

1. **锁定 + pon**：`existingNodes.lock()`，`layout.pon(newNodesOnly).run()`——最常用，但布局质量随节点增多而下降
2. **animate + fit**：每次 `layout.run()` 后接受抖动，依靠 `animate: true` 做平滑过渡——简单粗暴，适合节点数 < 50
3. **scratch 记录位置**：run 前 `node.scratch('pos', node.position())`，run 后恢复——可维持大致位置但布局会变
4. **手动定位**：放弃自动布局，根据关联节点用三角函数计算新节点位置——最可控，但需要手写定位逻辑

**本次建议：** Phase 1 用方案 4（手动定位，简单可控），Phase 2 切换到方案 1（锁定 + pon，适合节点增多后的场景）。

## 附录 B：已审阅的 BioDSA 关键文件清单

| 文件 | 内容 | 行数 |
|------|------|------|
| `biodsa/agents/deepevidence/agent.py` | DeepEvidence agent，包含 `generate()` 和 `go()` | 871 |
| `biodsa/agents/deepevidence/orchestrator_tool.py` | BFS/DFS tool 定义 | ~200 |
| `biodsa/agents/deepevidence/schema.py` | KB → tool 映射、KNOWLEDGE_BASE_LIST | ~80 |
| `biodsa/memory/graph.py` | AddToGraph/RetrieveFromGraph 工具 | ~150 |
| `biodsa/memory/memory_graph/schema.py` | Entity/Relation 数据模型 | ~50 |
| `scripts/mcp_server_standalone.py` | MCP server，tool handler，transport | ~450 |
| `frontend/src/views/chat/` (WeKnora) | 聊天组件、agent 流渲染 | ~2000 |
| `internal/mcp/` (WeKnora) | mcp-go 客户端封装 | ~500 |
