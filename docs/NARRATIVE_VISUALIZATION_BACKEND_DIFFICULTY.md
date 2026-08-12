# 推理过程可视化 — 架构与难点（总览）

## 最终架构

```
用户提问
    → WeKnora 后端: 生成 session_id
    → BioDSA POST /mcp (MCP tool call, args 带 session_id)
        → agent.go() 执行                                    ← 阻塞 3-5 分钟
        → generate() 中 stream 循环 → extract_events → broadcaster.emit_sync
        → 返回最终文本结果

同时：
    WeKnora 后端: goroutine 连 BioDSA GET /progress?session_id=xxx    ← 并行运行
        → 收到 SSE 事件 → EventBus.Emit(EventAgentGraph)
            → AgentStreamHandler → StreamManager → SSE → WeKnora 前端

agent 结束后：
    BioDSA: RunComplete 事件 → /progress 流中推送最终图谱数据
    WeKnora 前端: 收到 RunComplete → 调 POST /api/biodsa/graph/snapshot 落库
    用户再次打开对话 → GET /api/biodsa/graph/{session_id} → 恢复图谱
```

**两条连接：**
| 连接 | 发起方 | 路由 | 用途 |
|------|--------|------|------|
| MCP 工具调用 | WeKnora 后端 → BioDSA | `POST /mcp` | 触发 agent，等待最终结果 |
| 图谱事件流 | WeKnora 后端 → BioDSA | `GET /progress?session_id=xxx` | 实时图谱事件 |

**不需要浏览器直连 BioDSA，不需要 CORS，不需要改 mcp-go，不走 MCP 协议通道。**

---

## BioDSA 侧

### 1. 事件模型

`biodsa/narrative/events.py` — 7 种 frozen dataclass：

| 事件 | 来源 | 含义 |
|------|------|------|
| `EntitySearching` | KB 工具 tool_calls args | agent 开始搜索某实体 |
| `LiteratureSearching` | `search_papers` / `find_entities` tool_calls | agent 在搜文献 |
| `EntityConfirmed` | `add_to_graph` tool_calls args | agent 确认某实体重要 |
| `RelationFound` | `add_to_graph` tool_calls args | 两个实体间的关系 |
| `PhaseChange` | `go_breadth_first_search` / `go_depth_first_search` | 阶段切换 |
| `Progress` | 每 N 个 step 一次 | 进度统计 |
| `RunComplete` | agent 结束时 | 最终图谱完整快照 |

`entity_type` 需要归一化（AddToGraph 用 `GENE`/`VARIANT`/`DRUG`，KB 工具用 `gene`/`drug`/`disease`）。

### 2. 事件提取器

`biodsa/narrative/extractor.py` — `extract_events(message, step_num) -> list[NarrativeEvent]`

- 纯函数，输入 AIMessage，返回事件列表
- tool_name → (entity_type, arg_key) 映射表
- `add_to_graph` 特殊处理：entities → EntityConfirmed，relations → RelationFound
- `search_papers` / `find_entities` → LiteratureSearching（不是确定实体）

不需要正则解析 ToolMessage。所有信息来自结构化 tool_calls args。

### 3. SSE 广播器

`biodsa/narrative/broadcaster.py`

- `create_run(session_id)` / `close_run(session_id)`
- `emit_sync(session_id, event)` — 线程安全（从线程池内调用）
- `subscribe(session_id)` — async generator，SSE 格式
- 环形缓冲区（最近 50 条，WeKnora 断连重连时回放）

### 4. Stream Hook

`biodsa/agents/deepevidence/agent.py`

- `DeepEvidenceAgent.__init__` 新增可选参数：`broadcaster`、`session_id`
- `generate()` 内 stream 循环：每个 chunk 后 `extract_events()` + `broadcaster.emit_sync()`

### 5. MCP Server

`scripts/mcp_server_standalone.py`

- tool schema 新增 `session_id` 可选参数
- `tool_deepevidence_research` 内：`agent.go()` 丢 `run_in_executor`（线程池）
- Starlette 新增 `GET /progress?session_id=xxx` 路由
- broadcaster 单例生命周期

**总改动：~300 行新增 + ~50 行改动现有代码。**

---

## WeKnora 侧

### 1. 新事件类型

`event.go` — 新增 `EventAgentGraph`

`types` — 新增 `ResponseTypeGraph`

### 2. AgentStreamHandler

`agent_stream_handler.go` — 订阅 `EventAgentGraph`，转发到 StreamManager

### 3. MCP 工具执行时的并行 SSE 读取

`mcp_tool.go` — 在 `Execute()` 中，针对 `biodsa_deepevidence_research`：

```go
// 生成 session_id
sessionID := uuid.New().String()
input["session_id"] = sessionID

// 启动 goroutine 并行读 BioDSA /progress
ctx, cancel := context.WithCancel(ctx)
defer cancel()
go func() {
    url := service.URL + "/progress?session_id=" + sessionID
    // SSE 读取循环
    // 每个事件 → eventBus.Emit(EventAgentGraph)
}()

// 正常走 CallTool（阻塞）
result, err := client.CallTool(callCtx, name, input)
```

EventBus 通过 `ToolExecFromContext(ctx)` 获取，已在 `MCPTool.Execute` 中可用。

### 4. 持久化表

```sql
CREATE TABLE biodsa_graph_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     INTEGER NOT NULL,
    session_id    VARCHAR(64) NOT NULL,
    message_id    BIGINT,
    graph_data    JSONB NOT NULL,
    status        VARCHAR(16) DEFAULT 'running',
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE(session_id)
);
```

### 5. 图谱 API

```
POST   /api/biodsa/graph/snapshot   — 前端收到 RunComplete 后落库
GET    /api/biodsa/graph/{session_id} — 查询图谱
```

---

## 数据流时序

```
t=0s    WeKnora 后端生成 session_id
        goroutine 启动 → GET /progress?session_id=xxx（等待 BioDSA 开始推）
        MCP CallTool(args 含 session_id)  → BioDSA

t=3s    BioDSA: agent 开始跑，broadcaster.create_run(session_id)
        /progress SSE 连接活跃

t=5s    extract_events 产生第一个 EntitySearching → emit_sync → SSE
        WeKnora goroutine 收到 → EventBus.Emit(EventAgentGraph)
        → AgentStreamHandler → StreamManager → SSE → 前端

t=10s   PhaseChange: broad_search
t=15s   EntitySearching: EGFR (gene)
t=20s   EntityConfirmed: EGFR (gene)   ← add_to_graph 写入
t=25s   RelationFound: EGFR→T790M
...

t=180s  agent 跑完
        RunComplete 事件 → SSE
        goroutine 收到 → cancel() 终止 SSE 连接
        CallTool 返回最终文本结果

t=181s  前端收到 RunComplete → POST /api/biodsa/graph/snapshot → 落库
```

---

## 难点

### 难点 1（中等）：并发

`agent.go()` 同步阻塞，需要丢线程池跑。`broadcaster.emit_sync()` 需要在 `asyncio.Queue` 上做线程安全的 `put_nowait`。

### 难点 2（中等）：Subgraphs chunk 结构

`stream_mode=["values"]` + `subgraphs=True` 时，每个 chunk 来自不同子 agent（orchestrator、bfs_agent、dfs_agent）。extractor 需要根据 tool name 映射分属哪层，但逻辑是确定的——每个 chunk 的 `messages[-1]` 就是该 agent 最后一条消息。

### 难点 3（低）：entity_type 归一化

KB 工具的 entity_type 和 AddToGraph 的 entity_type 是两套命名体系（`"gene"` vs `"GENE"`）。维护一个映射表即可。

---

## 非难点

- **ToolMessage 自由文本提取** — 不需要。tool_calls + add_to_graph 都是结构化的
- **run_id 时序** — WeKnora 生成 session_id，不存在时序问题
- **CORS / SSRF** — 浏览器不直连 BioDSA，WeKnora 后端→BioDSA 的网络已是通的
- **mcp-go fork** — 不走 MCP 协议推送图谱事件，不需要
