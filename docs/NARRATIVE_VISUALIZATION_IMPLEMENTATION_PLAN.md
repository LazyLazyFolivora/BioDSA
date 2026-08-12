# BioDSA × WeKnora 知识图谱在线输出 —— 实现方案定稿

> 目标：DeepEvidence 推理过程中，实时把构建中的知识图谱推送给 WeKnora 并落库，支持中途查询与刷新恢复。
>
> 传输方式：MCP Streamable HTTP（复用现有的工具调用连接，不另开旁路 SSE）。
>
> 范围限定：仅 DeepEvidence，不改 `BaseAgent`，不改其余 6 个智能体。

---

## 零、一句话结论

链路上有 **3 个真正的断点**：

1. BioDSA 发通知时不带 `related_request_id`，导致事件被静默丢弃；
2. WeKnora 从未注册通知处理器，收到也会丢；
3. 缺少 `session_id → EventBus` 的路由表，无法把事件送回正确的对话。

其余都是增量开发。

调研中还发现一个**必须纠正的前提**：WeKnora 现有的"推理步骤"其实**不支持中途 DB 查询**（运行中只在 Redis 里，DB 要等回答结束才写），所以图谱持久化不能照抄它，需要做成独立表 + 增量写入。

---

## 一、调研结论：WeKnora 的"推理步骤"到底怎么存、怎么查

### 1.1 存储：没有独立表，是 `messages` 上的一个 JSONB 列

| 层 | 位置 |
|---|---|
| 迁移 | `migrations/versioned/000001_agent.up.sql:161-163`（`ALTER TABLE messages ADD COLUMN agent_steps JSONB`），GIN 索引在 `:339-343` |
| Model | `internal/types/message.go:220`（`Message.AgentSteps`）、`:301-330`（`Valuer`/`Scanner`）、`:344-346`（`BeforeCreate` 初始化空数组） |
| 结构体 | `internal/types/agent.go` 的 `AgentStep` / `ToolCall` |
| Repository | `internal/application/repository/message.go` 的 `UpdateMessage` |
| Service | `internal/application/service/message.go:229-253` |
| 接口 | `internal/types/interfaces/message.go:29-36` |

一次问答的全部推理步骤是**一个整体 JSONB blob**，随 message 行一起更新。

### 1.2 落库时机：整个运行期间只写一次，且在最后

`internal/handler/session/agent_stream_handler.go:618-626`：

```go
// Update agent steps if provided
if data.AgentSteps != nil {
    if steps, ok := data.AgentSteps.([]types.AgentStep); ok {
        h.assistantMessage.AgentSteps = agenttools.SanitizeAgentStepsForStorage(steps)
    }
}
```

这里只是把 steps 挂到**内存里**的 message 对象上。真正的 DB 写发生在 `internal/handler/session/qa.go:1368-1371` 的 `completeAssistantMessage` → `UpdateMessage`。

**结论：推理进行中，数据库里 `agent_steps` 是空的。**

### 1.3 中途查询靠的是 Redis，不是数据库

| 组件 | 位置 |
|---|---|
| 接口 | `internal/types/interfaces/stream_manager.go:26-31`（`AppendEvent` / `GetEvents(fromOffset)`） |
| Redis 实现 | `internal/stream/redis_manager.go`，RPush 追加、LRange 按 offset 增量读，TTL 默认 24 小时（`:39-41`） |
| 内存实现 | `internal/stream/memory_manager.go` |
| 断线续传路由 | `internal/router/routes_chat.go:74` → `GET /api/v1/sessions/continue-stream/:session_id?message_id=xxx` |
| 续传 handler | `internal/handler/session/stream.go:35`，先 `GetEvents(..., 0)` 全量重放（`:116`），再按 ticker 增量拉（`:175`） |

一个硬约束：续传接口要求该 message **尚未完成**，否则直接 404 `Incomplete message not found`（`stream.go:110`）。

### 1.4 完整链路

```
Agent Engine
  │ eventBus.Emit(EventAgentThought / ToolCall / ToolResult ...)
  ▼
AgentStreamHandler.handleXxx （Subscribe 注册在 agent_stream_handler.go:100-115）
  ├─► streamManager.AppendEvent(...) → Redis List → SSE handler 轮询 GetEvents → 前端
  └─► 累积到内存 assistantMessage → 回答结束 UpdateMessage → messages.agent_steps
```

### 1.5 这对我们的设计意味着什么

照抄推理步骤的话，中途查询只能落在 Redis 上，而它有三个致命限制：TTL 到期即失、服务重启即失、message 完成后接口直接 404。DeepEvidence 单次要跑几十分钟（`defaultToolExecTimeout` 已调到 3000s），跑完之后回看历史图谱是刚需。

所以**图谱要比推理步骤做得更强**：走独立表 + 逐条增量 INSERT。理由：

1. 图谱事件天然是 append-only 增量，适合行存；
2. 一次 upsert 一行，而不是重写整个 JSONB blob。DeepEvidence 一跑几百个实体，重写 blob 是 O(n²) 的写放大；
3. 中途查询就是一条 `SELECT ... WHERE message_id = ? AND seq > ?`，不依赖 Redis、不依赖 message 未完成；
4. 天然带单调 `seq`，正好满足"按队列渲染"的消费方式。

---

## 二、总体数据流

```
BioDSA (Python)
  DeepEvidenceAgent.generate()
    └─ extract_events(last_message) → NarrativeEvent
       └─ broadcaster.emit_sync(session_id, evt)        [worker 线程]
          └─ loop.call_soon_threadsafe → asyncio.Queue
             └─ _stream_to_mcp() 消费，分配单调 seq
                └─ session.send_notification(n, related_request_id=REQ_ID)   ★断点1
                   ↓ HTTP POST 响应流（SSE 帧）
WeKnora (Go)
  StreamableHTTP.handleSSEResponse → notificationHandler                      ★断点2
    └─ graphstream.Dispatch(session_id, payload)                              ★断点3
       └─ Sink{EventBus} → eventBus.Emit(EventAgentGraph)
          ├─ streamManager.AppendEvent(ResponseTypeAgentGraph) → Redis → SSE → 前端实时
          └─ agentGraphService.Record(...) → agent_graph_* 四张表 → 查询 API
```

---

## 三、BioDSA 侧改造

改动限定在三个文件：`scripts/mcp_server_standalone.py`、`biodsa/agents/deepevidence/agent.py`、`biodsa/narrative/broadcaster.py`。
`narrative/events.py` 和 `narrative/extractor.py` 完全不动。

### 3.1 【必改】通知携带 `related_request_id`

**文件**：`scripts/mcp_server_standalone.py:105-120`

**当前**：

```python
try:
    ctx = request_ctx.get()
    mcp_session = ctx.session
    _sid = session_id
    async def _stream_to_mcp():
        async for event in _broadcaster.subscribe_events(_sid):
            notification = Notification(
                method="notifications/biodsa/graph_event",
                params={
                    "session_id": _sid,
                    "event": event.to_dict(),
                },
            )
            await mcp_session.send_notification(notification)
    consumer_task = asyncio.create_task(_stream_to_mcp())
    logging.info("DeepEvidence: MCP notification consumer started for session=%s", session_id)
except LookupError:
    logging.warning("DeepEvidence: no MCP request context, graph events will not be streamed")
```

**改成**：

```python
try:
    ctx = request_ctx.get()
    mcp_session = ctx.session
    _sid = session_id
    _req_id = ctx.request_id          # 关键：本次 tools/call 的 JSON-RPC id

    async def _stream_to_mcp():
        seq = 0
        async for event in _broadcaster.subscribe_events(_sid):
            seq += 1
            notification = Notification(
                method="notifications/biodsa/graph_event",
                params={
                    "session_id": _sid,
                    "seq": seq,
                    "event": event.to_dict(),
                },
            )
            await mcp_session.send_notification(
                notification,
                related_request_id=_req_id,
            )

    consumer_task = asyncio.create_task(_stream_to_mcp())
```

**为什么**：不带 `related_request_id` 时，Python SDK 把通知投递到"广播通道"（对应客户端的 GET 长连接）。而 WeKnora 的 mcp-go 客户端**从不开 GET 流**，所以这些通知会被静默丢弃——这是当前整条链路不通的首要原因。带上之后，通知会写进本次 `tools/call` 的 POST 响应 SSE 流。

顺带在这里分配 `seq`：消费者是单个 asyncio task、消费的是 FIFO 队列，所以计数器天然单调有序，正好作为 WeKnora 侧的游标和幂等键。

**已验证**：mcp-go 的 `handleSSEResponse`（`streamable_http.go:692-725`）确实会把 POST 响应流里 `id` 为空的消息当作通知，投给 `notificationHandler`。链路成立。

### 3.2 【必改】图谱缓存目录按 session 隔离

**文件**：`biodsa/agents/deepevidence/agent.py`

**当前**（`__init__`，`:125-129`）：

```python
if evidence_graph_cache_dir is None:
    # assign a default value
    evidence_graph_cache_dir = get_default_memory_graph_cache_dir()

self.evidence_graph_cache_dir = evidence_graph_cache_dir
```

**当前**（`go()`，`:874-883`）：

```python
if clear_evidence_graph_cache:
    # remove everything under the evidence_graph_cache_dir
    if self.evidence_graph_cache_dir is not None:
        if os.path.exists(self.evidence_graph_cache_dir):
            shutil.rmtree(self.evidence_graph_cache_dir)
            os.makedirs(self.evidence_graph_cache_dir, exist_ok=True)
        # Clear the cached KnowledgeGraphManager instance to avoid reusing stale data
        clear_manager_cache(cache_dir=self.evidence_graph_cache_dir)
    else:
        raise ValueError("evidence_graph_cache_dir is not set")
```

**改成**（只动 `__init__`，`go()` 一行不改）：

```python
if evidence_graph_cache_dir is None:
    evidence_graph_cache_dir = get_default_memory_graph_cache_dir()
    if session_id:
        # 每个 MCP 会话独占一个图谱目录，避免并发运行时 go() 里的
        # rmtree 抹掉别人正在写的图谱
        evidence_graph_cache_dir = os.path.join(
            evidence_graph_cache_dir, "sessions", session_id
        )
        os.makedirs(evidence_graph_cache_dir, exist_ok=True)
self.evidence_graph_cache_dir = evidence_graph_cache_dir
```

**为什么**：`go()` 无条件 `rmtree` 整个缓存目录，而默认目录是**全局共享**的 `~/.biodsa_memory/memory_graph`。两个用户同时提问，后启动的那个会把前一个正在构建的图谱整个删掉——数据直接损坏。隔离之后 `rmtree` 只作用于自己的子目录，`go()` 里的逻辑保持原样即可，`clear_manager_cache(cache_dir=...)` 也是按目录做键的，自动跟着隔离。

**配套**：在 `scripts/mcp_server_standalone.py` 的 `finally`（`:155-164`）里加上目录清理，否则 `~/.biodsa_memory/memory_graph/sessions/` 会无限增长：

```python
finally:
    if agent is not None:
        agent.sandbox = None
    if session_id:
        _broadcaster.close_run(session_id)
        if consumer_task is not None:
            try:
                await consumer_task
            except Exception:
                logging.warning("Graph event consumer task failed", exc_info=True)
        # 图谱已随 RunComplete 快照送达客户端，本地副本不再需要
        try:
            if agent is not None and agent.evidence_graph_cache_dir:
                shutil.rmtree(agent.evidence_graph_cache_dir, ignore_errors=True)
        except Exception:
            logging.warning("Failed to clean session graph dir", exc_info=True)
```

注意顺序：必须在 `await consumer_task` **之后**再删目录。RunComplete 快照是从 `results.evidence_graph_data` 读的（已经在内存里，`:137`），但保险起见仍按此顺序。

### 3.3 【必改】`emit_sync` 的线程安全

**文件**：`biodsa/narrative/broadcaster.py:47-67`

**当前**：

```python
def emit_sync(self, session_id: str, event: NarrativeEvent) -> None:
    """
    Thread-safe push into the asyncio queue.

    Called from the agent stream loop (inside a thread-pool executor).
    asyncio.Queue.put_nowait is explicitly documented as thread-safe.
    """
    queue = self._queues.get(session_id)
    if queue is None:
        logger.warning("Broadcaster emit for unknown session: %s", session_id)
        return
    # Ring buffer for late subscribers
    buf = self._buffers.get(session_id)
    if buf is not None:
        buf.append(event)
        if len(buf) > RING_BUFFER_SIZE:
            buf.pop(0)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("Broadcaster queue full for session: %s", session_id)
```

docstring 里 "asyncio.Queue.put_nowait is explicitly documented as thread-safe" 这句话是**错的**。CPython 官方文档明确写着 asyncio 队列 *is not thread-safe*。`put_nowait` 内部会调用 `_wakeup_next(self._getters)`，进而 `future.set_result()`——从非事件循环线程调用会导致 waiter 唤醒丢失，表现为事件卡住直到下一个事件到来才被顺带唤醒（图谱一顿一顿地跳着长）。这里的调用点 `agent.py:812` 正是在 `run_in_executor` 的工作线程里。

**改成**（`create_run` 记录事件循环，`emit_sync` 走 `call_soon_threadsafe`）：

```python
def __init__(self) -> None:
    self._queues: dict[str, asyncio.Queue] = {}
    self._buffers: dict[str, list[NarrativeEvent]] = {}
    self._loops: dict[str, asyncio.AbstractEventLoop] = {}

def create_run(self, session_id: str) -> None:
    if session_id not in self._queues:
        self._queues[session_id] = asyncio.Queue()
        self._buffers[session_id] = []
        # create_run 一定在事件循环线程里被调用（MCP 工具入口）
        self._loops[session_id] = asyncio.get_running_loop()
        logger.info("Broadcaster run created: session_id=%s", session_id)

def close_run(self, session_id: str) -> None:
    queue = self._queues.pop(session_id, None)
    self._buffers.pop(session_id, None)
    self._loops.pop(session_id, None)
    if queue is not None:
        try:
            queue.put_nowait(SENTINEL)
        except asyncio.QueueFull:
            pass
        logger.info("Broadcaster run closed: session_id=%s", session_id)

def emit_sync(self, session_id: str, event: NarrativeEvent) -> None:
    """Push an event from any thread into the session's asyncio queue."""
    queue = self._queues.get(session_id)
    loop = self._loops.get(session_id)
    if queue is None or loop is None:
        logger.warning("Broadcaster emit for unknown session: %s", session_id)
        return
    buf = self._buffers.get(session_id)
    if buf is not None:
        buf.append(event)
        if len(buf) > RING_BUFFER_SIZE:
            buf.pop(0)
    try:
        loop.call_soon_threadsafe(queue.put_nowait, event)
    except RuntimeError:
        # 事件循环已关闭（运行被取消），丢弃即可
        logger.warning("Broadcaster loop closed for session: %s", session_id)
```

`call_soon_threadsafe` 对同一个来源线程保证 FIFO，所以事件顺序不受影响。`close_run` 仍在事件循环线程里调用，保持 `put_nowait` 即可。

### 3.4 【Bug】Progress 计数恒为 0

**文件**：`biodsa/agents/deepevidence/agent.py:813-834`

```python
# periodic progress
if step_num % 5 == 0:
    from biodsa.narrative.events import Progress, PhaseChange
    counts = {
        "entity_searching": 0, "entity_confirmed": 0,
        "relation_found": 0, "current_phase": "",
    }
    for evt in events:
        tn = type(evt).__name__
        if tn in counts:
            counts[tn] = counts.get(tn, 0) + 1
```

这里有**两个叠加的 bug**：

1. `type(evt).__name__` 返回的是 `"EntityConfirmed"`（驼峰），而 `counts` 的键是 `"entity_confirmed"`（下划线）。`if tn in counts` 永远为 False，所有计数恒为 0，`entities_found` / `relations_found` 永远发 0。
2. 即使键名对上了，累加也只发生在 `step_num % 5 == 0` 的那一步，且只统计**当步**的 `events`。中间 4 步的事件从来没被计入，会漏掉 80% 的计数。

**改成**（把累加提到每步都做，与 Progress 发送解耦）：

```python
from biodsa.narrative.events import Progress, PhaseChange

# 每一步都累加，而不是只在发 Progress 的那步
for evt in events:
    tn = type(evt).__name__
    if tn in ("EntitySearching", "EntityConfirmed", "RelationFound"):
        self._event_counts[tn] = self._event_counts.get(tn, 0) + 1
    if isinstance(evt, PhaseChange):
        self._current_phase = evt.phase

if step_num % 5 == 0:
    self.broadcaster.emit_sync(self._session_id, Progress(
        step=step_num,
        total_steps_estimate=30,
        entities_found=self._event_counts.get("EntityConfirmed", 0),
        relations_found=self._event_counts.get("RelationFound", 0),
        current_phase=getattr(self, "_current_phase", ""),
    ))
```

配套在 `__init__`（`:139` 附近）加 `self._current_phase: str = ""`，因为原来 `current_phase` 是每 5 步重新算的局部变量，跨步会丢。

### 3.5 【Bug】`values` 模式导致事件重复

**文件**：`biodsa/agents/deepevidence/agent.py:782-812`

```python
for streamed_chunk in self.agent_graph.stream(
    inputs,
    stream_mode = ["values"],
    subgraphs=True,
    config={
        "recursion_limit": 100
    }
):
    chunk = streamed_chunk[-1]
    step_num += 1
    last_message = chunk['messages'][-1]
```

`stream_mode="values"` 在**每个 superstep 后都吐出完整 state**。如果某个节点只改了 state 的其他键（比如 `total_input_tokens`、`code_execution_results`）而没有追加消息，`chunk['messages'][-1]` 就还是上一条 AIMessage，`extract_events` 会把同一批 `tool_calls` 再解析一遍，重复发出 `EntityConfirmed` / `RelationFound`。叠加 `subgraphs=True`，同一条消息还可能在子图层和父图层各出现一次。

**改成**（按 message id 去重）：

```python
step_num = 0
t_start = time.time()
seen_message_ids: set[str] = set()
for streamed_chunk in self.agent_graph.stream(...):
    chunk = streamed_chunk[-1]
    step_num += 1
    last_message = chunk['messages'][-1]
    ...
    all_results.append(chunk)
    # ── narrative events ──
    msg_id = getattr(last_message, "id", None)
    is_new_message = msg_id is None or msg_id not in seen_message_ids
    if msg_id is not None:
        seen_message_ids.add(msg_id)

    if self.broadcaster is not None and self._session_id and is_new_message:
        try:
            ...原有事件提取与发送逻辑...
```

注意 `all_results.append(chunk)` 保持在去重之外——`go()` 依赖 `results[-1]` 拿最终 state，不能动。

### 3.6 【Bug】ring buffer 重放会重复投递

**文件**：`biodsa/narrative/broadcaster.py:94-113`

```python
queue = self._queues.get(session_id)
if queue is None:
    return

buf = self._buffers.get(session_id, [])
for event in buf:
    yield event

while True:
    event = await queue.get()
    if event is SENTINEL:
        return
    yield event
```

`emit_sync` 会**同时**写 ring buffer 和 queue。`subscribe_events` 先把 buffer 全部 yield 一遍，然后再从 queue 里取——而那些事件还都在 queue 里。所以订阅开始时 buffer 中已有的事件（最多 50 条）会被投递两次。

目前之所以没暴露，是因为 `create_run` 之后立刻 `asyncio.create_task(_stream_to_mcp())`，此时 buffer 是空的。但这是个定时炸弹：只要将来订阅稍晚一点（比如加了鉴权、加了 await），前 50 条事件立刻翻倍。

**改成**：`subscribe_events` 不做 buffer 重放（它服务的是"从头就在的"MCP 消费者）：

```python
async def subscribe_events(self, session_id: str):
    """Yield raw NarrativeEvent objects for MCP notification delivery.

    No ring-buffer replay: the MCP consumer subscribes at run creation, so
    replaying would double-deliver everything still queued.
    """
    queue = self._queues.get(session_id)
    if queue is None:
        return
    while True:
        event = await queue.get()
        if event is SENTINEL:
            return
        yield event
```

`subscribe()`（SSE 那条路径，`:69-92`）有同样的问题，但它当前没有被 MCP 服务器使用，本方案范围内建议只改 `subscribe_events`。

---

## 四、WeKnora 侧改造

### 4.1 自动注入 `session_id`（用 ToolCallID）

**文件**：`internal/agent/tools/mcp_tool.go:103-114`（现有文件加代码）

`MCPInput = map[string]any`（`:17`），所以注入很直接。在 `json.Unmarshal` 之后加：

```go
// BioDSA-style servers stream incremental graph events as MCP notifications
// tagged with a caller-supplied session id. Reuse ToolCallID so each tool
// call gets a unique, already-known correlation key.
if meta, ok := ToolExecFromContext(ctx); ok && meta.ToolCallID != "" {
    if _, present := input["session_id"]; !present && t.declaresSessionID() {
        input["session_id"] = meta.ToolCallID
        graphstream.Register(meta.ToolCallID, &graphstream.Sink{
            Ctx:                context.WithoutCancel(ctx),
            EventBus:           meta.EventBus,
            SessionID:          meta.SessionID,
            AssistantMessageID: meta.AssistantMessageID,
        })
        defer graphstream.Unregister(meta.ToolCallID)
    }
}
```

`declaresSessionID()` 是新增的小方法，检查 `t.mcpTool.InputSchema` 的 `properties` 里有没有 `session_id`。**必须做这个检查**——盲目往所有 MCP 工具的参数里塞 `session_id`，会让 schema 严格校验的服务端直接报参数错误。

`context.WithoutCancel(ctx)` 很关键：通知是在 mcp-go 的 SSE 读取 goroutine 上回调的，而工具执行的 ctx 在工具返回后会被取消。用 `WithoutCancel` 既保留了 tenant_id / request_id 等 value，又不会被提前取消。

### 4.2 全局注册表：session_id → EventBus

**新建文件**：`internal/graphstream/registry.go`

放在顶层 `internal/graphstream` 而不是 `internal/mcp` 或 `internal/agent/tools`，是为了避免包循环：`internal/agent/tools` 和 `internal/mcp` 都要引用它，而它只依赖 `internal/event`。

```go
package graphstream

// Sink routes graph notifications from one MCP tool call back to the
// EventBus of the agent turn that issued it.
type Sink struct {
    Ctx                context.Context
    EventBus           *event.EventBus
    SessionID          string
    AssistantMessageID string
}

var (
    mu    sync.RWMutex
    sinks = make(map[string]*Sink) // toolCallID -> sink
)

func Register(toolCallID string, s *Sink) { ... }
func Unregister(toolCallID string)        { ... }

// Dispatch turns one notification payload into an EventBus event.
// Returns false when no live tool call matches (late / stale notification).
func Dispatch(params map[string]any) bool { ... }
```

**为什么必须有这张表**：MCP 客户端是**按 service.ID 缓存并全局共享**的——`internal/mcp/manager.go:46-49`：

```go
if service.AuthConfig.IsOAuth() {
    return service.ID + "\x00" + principal.Normalize().StorageID()
}
return service.ID
```

同一个 BioDSA 服务的**所有租户、所有并发会话共用一个 client 实例**，因此 `OnNotification` 回调也只有一份。不按 payload 里的 `session_id` 路由，事件就会串台到别人的对话里。

### 4.3 注册 `OnNotification` 处理器

**文件**：`internal/mcp/client.go:237`（现有文件加代码）

当前只注册了连接丢失回调。在 `OnConnectionLost` 旁边加：

```go
mcpClient.OnNotification(func(n mcp.JSONRPCNotification) {
    if n.Method != graphstream.NotificationMethod { // "notifications/biodsa/graph_event"
        return
    }
    if !graphstream.Dispatch(n.Params.AdditionalFields) {
        logger.Debugf(context.Background(),
            "[MCP] dropped graph notification with no live tool call: service=%s", config.Service.Name)
    }
})
mcpClient.OnConnectionLost(instance.onConnectionLost)
```

**已验证的三点**（均直接读过 mcp-go v0.52.0 源码）：

1. `Client.OnNotification(func(mcp.JSONRPCNotification))` 存在（`client/client.go:129-135`），且 `Start()` 会把 transport 层的 handler 接到这些回调上（`client/client.go:106-112`），注册时机在 Start 前后都可以。
2. 自定义 params 字段落在 `NotificationParams.AdditionalFields map[string]any`（`mcp/types.go:223-229`），`UnmarshalJSON` 会把除 `_meta` 外的所有字段塞进去（`mcp/types.go:253-283`）。所以 `session_id` / `seq` / `event` 都能拿到。
3. **POST 响应流上的通知确实会走到这个 handler**：`handleSSEResponse`（`streamable_http.go:689-725`）注释明写 "processes an SSE stream for a specific request"，对 `message.ID.IsNil()` 的消息调用 `c.notificationHandler`。

### 4.4 新增事件类型与响应类型

| 文件 | 改动 |
|---|---|
| `internal/event/event.go:57` 附近 | 加 `EventAgentGraph EventType = "agent_graph"` |
| `internal/event/event_data.go` | 加 `AgentGraphData` 结构体（字段见 §5.3） |
| `internal/types/chat.go:161` 附近 | 加 `ResponseTypeAgentGraph ResponseType = "agent_graph"` |

### 4.5 在 AgentStreamHandler 里接住它

**文件**：`internal/handler/session/agent_stream_handler.go`

`Subscribe()`（`:100-115`）加一行：

```go
h.eventBus.On(event.EventAgentGraph, h.handleAgentGraph)
```

新增 handler，完全对齐现有 `handleToolCall`（`:198-207`）的写法，只是多一路落库：

```go
// handleAgentGraph forwards one incremental knowledge-graph event to the SSE
// stream and persists it so the graph survives a page refresh.
func (h *AgentStreamHandler) handleAgentGraph(ctx context.Context, evt event.Event) error {
    data, ok := evt.Data.(event.AgentGraphData)
    if !ok {
        return nil
    }

    if err := h.streamManager.AppendEvent(h.ctx, h.sessionID, h.assistantMessageID, interfaces.StreamEvent{
        ID:        evt.ID,
        Type:      types.ResponseTypeAgentGraph,
        Content:   "",
        Done:      false,
        Timestamp: time.Now(),
        Data: map[string]interface{}{
            "seq":          data.Seq,
            "graph_event":  data.EventType,
            "tool_call_id": data.ToolCallID,
            "payload":      data.Payload,
        },
    }); err != nil {
        logger.GetLogger(h.ctx).Error("Append agent graph event to stream failed", "error", err)
    }

    if h.agentGraphService != nil {
        if err := h.agentGraphService.Record(h.ctx, h.sessionID, h.assistantMessageID, data); err != nil {
            logger.GetLogger(h.ctx).Error("Persist agent graph event failed", "error", err)
        }
    }
    return nil
}
```

**结构体与构造函数也要改**：`AgentStreamHandler` 加 `agentGraphService interfaces.AgentGraphService` 字段（`:27-32` 区域），`NewAgentStreamHandler`（`:79-93`）加一个参数，调用点在 `internal/handler/session/qa.go` 里。允许为 nil，nil 时只走 SSE 不落库，方便灰度。

### 4.6 落库：表结构

**新建**：

- `migrations/custom/versioned/000004_agent_graph_stream.up.sql` / `.down.sql`
- `migrations/custom/sqlite/000004_agent_graph_stream.up.sql` / `.down.sql`

编号接在现有 `000003_remove_graph_kb_id` 之后。注意**必须放在 `migrations/custom/` 下**，这是该 fork 用来隔离自有改动、避免和上游 `migrations/versioned/` 冲突的约定。

四张表，职责分明：

```sql
-- Custom: 000004_agent_graph_stream
-- Per-turn incremental knowledge graph produced by streaming MCP agents.
-- agent_graph_events is the append-only source of truth; nodes/edges are a
-- derived projection kept in sync on write, mirroring the graph_sync design.

-- ① 运行头：一次工具调用 = 一行
CREATE TABLE IF NOT EXISTS agent_graph_runs (
    id               VARCHAR(36) PRIMARY KEY,
    tenant_id        INTEGER      NOT NULL,
    session_id       VARCHAR(36)  NOT NULL,
    message_id       VARCHAR(36)  NOT NULL,
    tool_call_id     VARCHAR(128) NOT NULL,
    status           VARCHAR(32)  NOT NULL DEFAULT 'running',  -- running|completed|failed
    phase            VARCHAR(64)  NOT NULL DEFAULT '',
    step             INTEGER      NOT NULL DEFAULT 0,
    entity_count     INTEGER      NOT NULL DEFAULT 0,
    relation_count   INTEGER      NOT NULL DEFAULT 0,
    last_seq         BIGINT       NOT NULL DEFAULT 0,
    duration_seconds DOUBLE PRECISION,
    started_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_graph_runs_tool_call ON agent_graph_runs (tool_call_id);
CREATE INDEX IF NOT EXISTS idx_agent_graph_runs_message ON agent_graph_runs (message_id);

-- ② 事件流：前端"按队列渲染"的数据源，也是唯一真相
CREATE TABLE IF NOT EXISTS agent_graph_events (
    id           VARCHAR(36) PRIMARY KEY,
    tenant_id    INTEGER      NOT NULL,
    session_id   VARCHAR(36)  NOT NULL,
    message_id   VARCHAR(36)  NOT NULL,
    tool_call_id VARCHAR(128) NOT NULL,
    seq          BIGINT       NOT NULL,
    event_type   VARCHAR(64)  NOT NULL,
    event_id     VARCHAR(64)  NOT NULL DEFAULT '',
    payload      JSONB        NOT NULL DEFAULT '{}',
    emitted_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- (tool_call_id, seq) 唯一 → 重复通知天然幂等
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_graph_events_dedup ON agent_graph_events (tool_call_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_graph_events_cursor ON agent_graph_events (message_id, seq);

-- ③ 节点投影
CREATE TABLE IF NOT EXISTS agent_graph_nodes (
    id           VARCHAR(36) PRIMARY KEY,
    tenant_id    INTEGER      NOT NULL,
    session_id   VARCHAR(36)  NOT NULL,
    message_id   VARCHAR(36)  NOT NULL,
    tool_call_id VARCHAR(128) NOT NULL,
    entity_name  VARCHAR(500) NOT NULL,
    entity_type  VARCHAR(100) NOT NULL DEFAULT '',
    status       VARCHAR(32)  NOT NULL DEFAULT 'searching',  -- searching|confirmed
    source_kb    VARCHAR(100) NOT NULL DEFAULT '',
    observations JSONB        NOT NULL DEFAULT '[]',
    first_seq    BIGINT       NOT NULL,
    last_seq     BIGINT       NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_graph_nodes_unique ON agent_graph_nodes (message_id, entity_name);
CREATE INDEX IF NOT EXISTS idx_agent_graph_nodes_cursor ON agent_graph_nodes (message_id, last_seq);

-- ④ 边投影
CREATE TABLE IF NOT EXISTS agent_graph_edges (
    id            VARCHAR(36) PRIMARY KEY,
    tenant_id     INTEGER      NOT NULL,
    session_id    VARCHAR(36)  NOT NULL,
    message_id    VARCHAR(36)  NOT NULL,
    tool_call_id  VARCHAR(128) NOT NULL,
    source_entity VARCHAR(500) NOT NULL,
    target_entity VARCHAR(500) NOT NULL,
    relation_type VARCHAR(200) NOT NULL DEFAULT '',
    first_seq     BIGINT       NOT NULL,
    last_seq      BIGINT       NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_graph_edges_unique
    ON agent_graph_edges (message_id, source_entity, target_entity, relation_type);
CREATE INDEX IF NOT EXISTS idx_agent_graph_edges_cursor ON agent_graph_edges (message_id, last_seq);
```

设计要点：

- **`(tool_call_id, seq)` 唯一索引**是幂等的关键。BioDSA 侧的 seq 单调递增，重发/重连导致的重复写入会被 `ON CONFLICT DO NOTHING` 直接吃掉。
- **`(message_id, entity_name)` 唯一**：`EntitySearching` 先建一个 `status='searching'` 的节点，后续 `EntityConfirmed` upsert 同一行把状态升级为 `confirmed` 并补上 observations。前端因此天然拿到"搜索中 → 已确认"的状态机，而不需要自己合并。
- **`first_seq` / `last_seq`**：既是增量游标，也让前端能知道某个节点是第几步出现的。

### 4.7 落库：分层代码

严格对齐 `graph_sync` 那一套（`types` → `types/interfaces` → `application/repository` → `application/service` → `handler` → `router` → `container`）：

| 文件 | 新建/修改 | 内容 |
|---|---|---|
| `internal/types/agent_graph.go` | 新建 | `AgentGraphRun` / `AgentGraphEvent` / `AgentGraphNode` / `AgentGraphEdge` 四个 GORM 模型 + `TableName()`；`AgentGraphSnapshot` 响应结构；事件类型常量 |
| `internal/types/interfaces/agent_graph.go` | 新建 | `AgentGraphRepository`（`UpsertRun`、`InsertEvent`、`UpsertNode`、`UpsertEdge`、`GetRun`、`ListEvents`、`ListNodes`、`ListEdges`）+ `AgentGraphService`（`Record`、`GetSnapshot`） |
| `internal/application/repository/agent_graph.go` | 新建 | GORM 实现。写入统一用 `clause.OnConflict`，照抄 `repository/graph_sync.go:27-40` 的写法 |
| `internal/application/service/agent_graph.go` | 新建 | `Record()` 做事件类型分发；`GetSnapshot()` 做游标查询 |
| `internal/handler/agent_graph.go` | 新建 | `AgentGraphHandler.GetGraph`，照抄 `handler/graph_sync.go` 的 swagger 注释与 `c.Error(apperrors.…)` 错误风格 |
| `internal/router/agent_graph_routes.go` | 新建 | `RegisterAgentGraphRoutes` |
| `internal/router/router.go:253` 附近 | 修改 | 加一行 `RegisterAgentGraphRoutes(v1, params.AgentGraphHandler, rbacGuards)`，并在 `params` 结构体加字段 |
| `internal/container/container.go:177` / `:225` / `:387` 附近 | 修改 | 三处 `must(container.Provide(...))`：repository、service、handler |
| `internal/handler/session/qa.go` | 修改 | `NewAgentStreamHandler` 调用点补参数 |

`Record()` 里的类型分发逻辑：

| BioDSA event_type | 动作 |
|---|---|
| `EntitySearching` | 写 events；upsert node（`status='searching'`，冲突时**不覆盖**已有的 `confirmed`）；更新 `run.last_seq` |
| `LiteratureSearching` | 只写 events（不产生节点） |
| `EntityConfirmed` | 写 events；upsert node（`status='confirmed'`，覆盖 type/observations）；`run.entity_count` 重算 |
| `RelationFound` | 写 events；upsert edge；`run.relation_count` 重算 |
| `PhaseChange` | 写 events；更新 `run.phase` |
| `Progress` | 写 events；更新 `run.step` |
| `RunComplete` | 写 events；**用快照做一次全量对账**（把 entities/relations 全部 upsert 进 nodes/edges，补齐增量遗漏的）；`run.status='completed'`、写 `completed_at` / `duration_seconds` |

RunComplete 的对账是"保留完整快照"这个决策的兑现点：即使中间丢了几条通知，收尾时也能补齐。因为 nodes/edges 用的是唯一索引 upsert，重复对账是幂等的。

### 4.8 查询 API

**路由**（新建 `internal/router/agent_graph_routes.go`）：

```go
// RegisterAgentGraphRoutes exposes the incremental knowledge graph built by
// streaming MCP agents during one assistant turn.
//
// Route:
//
//	GET /api/v1/sessions/:id/messages/:message_id/graph
func RegisterAgentGraphRoutes(r *gin.RouterGroup, h *handler.AgentGraphHandler, g *rbacGuards) {
    if h == nil {
        return
    }
    r.GET("/sessions/:id/messages/:message_id/graph", g.Viewer(), h.GetGraph)
}
```

**通配符命名必须是 `:id` 和 `:message_id`**，不能自己发挥。gin 要求同一 HTTP 方法的 radix 树里通配符名字一致，而现有 GET 树已经被 `sessions.GET("/:id/messages/:message_id/suggestions", ...)`（`internal/router/routes_chat.go:78`）定死了。用 `:session_id` 会在启动时 panic。

**请求**：

```
GET /api/v1/sessions/{session_id}/messages/{message_id}/graph?after_seq=0&include=nodes,edges,events
```

- `after_seq`（默认 0）：增量游标。前端拿到上次的 `last_seq` 传回来，只取新增部分。
- `include`（默认 `nodes,edges,run`）：`events` 需显式请求，因为长跑任务的事件量远大于节点数。

**响应**：

```json
{
  "success": true,
  "data": {
    "run": {
      "tool_call_id": "call_abc123",
      "status": "running",
      "phase": "deep_dive",
      "step": 25,
      "entity_count": 47,
      "relation_count": 62,
      "last_seq": 184,
      "started_at": "2026-08-12T10:00:00Z",
      "completed_at": null,
      "duration_seconds": null
    },
    "nodes": [
      {
        "entity_name": "EGFR",
        "entity_type": "gene",
        "status": "confirmed",
        "source_kb": "gene",
        "observations": ["Receptor tyrosine kinase ..."],
        "first_seq": 12,
        "last_seq": 31
      }
    ],
    "edges": [
      {
        "source_entity": "Osimertinib",
        "target_entity": "EGFR T790M",
        "relation_type": "inhibits",
        "first_seq": 44,
        "last_seq": 44
      }
    ],
    "events": [],
    "last_seq": 184
  }
}
```

关键性质：**运行中和运行后调用完全同一个接口、返回同一个结构**，只是 `run.status` 从 `running` 变成 `completed`。这正好补上了 `continue-stream` 那个"message 必须未完成否则 404"的缺口。

---

## 五、数据契约

### 5.1 BioDSA 发出的通知（线上格式）

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/biodsa/graph_event",
  "params": {
    "session_id": "call_abc123",
    "seq": 42,
    "event": {
      "event_id": "3f2b9c1e4a7d4f8fa1b2c3d4e5f60718",
      "timestamp": 1786608000.123,
      "event_type": "EntityConfirmed",
      "entity_name": "EGFR",
      "entity_type": "gene",
      "observations": ["Receptor tyrosine kinase, frequently mutated in NSCLC"]
    }
  }
}
```

`params.session_id` = WeKnora 注入的 `ToolCallID`。`event` 对象由 `NarrativeEvent.to_dict()` 产生（`biodsa/narrative/events.py:35-38`）：`asdict()` 的全部字段 + 追加的 `event_type`（即类名）。

### 5.2 七种事件类型的字段

所有类型都含 `event_id: str`、`timestamp: float`、`event_type: str`。以下是各自独有的字段（定义见 `biodsa/narrative/events.py:41-98`）：

| event_type | 独有字段 | 语义 |
|---|---|---|
| `EntitySearching` | `entity_name: str`、`entity_type: str`、`source_kb: str`、`search_term: str` | 正在某个知识库里查一个具名实体 |
| `LiteratureSearching` | `query: str`、`source_kb: str` | 自由文本检索，不产生具体实体 |
| `EntityConfirmed` | `entity_name: str`、`entity_type: str`、`observations: list[str]` | 实体已确认并写入证据图谱 |
| `RelationFound` | `source_entity: str`、`target_entity: str`、`relation_type: str` | 发现一条关系 |
| `PhaseChange` | `phase: str`、`search_target: str`、`knowledge_bases: list[str]` | BFS↔DFS 切换，`phase` ∈ {`broad_search`, `deep_dive`} |
| `Progress` | `step: int`、`total_steps_estimate: int`、`entities_found: int`、`relations_found: int`、`current_phase: str` | 每 5 步一次的进度快照 |
| `RunComplete` | `entities: list[dict]`、`relations: list[dict]`、`total_steps: int`、`duration_seconds: float`、`final_response_preview: str` | 收尾，带完整图谱快照供对账 |

注意 `EntitySearching.entity_type` 是**未归一化**的（extractor 里从工具名硬编码来的：`gene`/`drug`/`disease`/`variant`/`target`/`compound`/`pathway`，见 `biodsa/narrative/extractor.py:22-44`），而 `EntityConfirmed.entity_type` 经过 `_normalize_entity_type()` 归一化（`events.py:13-27`，比如 `PROTEIN`→`gene`、`CHEMICAL`→`compound`）。两者可能对不上同一个词表，前端如果按类型上色需要注意；投影表里以 `EntityConfirmed` 的为准（upsert 时覆盖）。

### 5.3 字段映射

| BioDSA | WeKnora `event.AgentGraphData` | DB 落点 |
|---|---|---|
| `params.session_id` | `ToolCallID` | 四张表的 `tool_call_id` |
| `params.seq` | `Seq` | `agent_graph_events.seq`、`nodes/edges.last_seq`、`runs.last_seq` |
| `event.event_type` | `EventType` | `agent_graph_events.event_type` |
| `event.event_id` | `EventID` | `agent_graph_events.event_id` |
| `event.timestamp` | `Timestamp float64` | `agent_graph_events.emitted_at`（Unix 秒转 `TIMESTAMPTZ`） |
| `event` 其余全部字段 | `Payload map[string]any` | `agent_graph_events.payload` (JSONB) |
| —（Sink 携带） | `SessionID` / `AssistantMessageID` | `session_id` / `message_id` |
| —（ctx 携带） | — | `tenant_id`（`types.MustTenantIDFromContext`） |

投影映射：

| 事件字段 | → 表列 |
|---|---|
| `EntityConfirmed.entity_name` | `agent_graph_nodes.entity_name` |
| `EntityConfirmed.entity_type` | `agent_graph_nodes.entity_type` |
| `EntityConfirmed.observations` | `agent_graph_nodes.observations` (JSONB 数组) |
| `EntitySearching.source_kb` | `agent_graph_nodes.source_kb` |
| `RelationFound.source_entity` | `agent_graph_edges.source_entity` |
| `RelationFound.target_entity` | `agent_graph_edges.target_entity` |
| `RelationFound.relation_type` | `agent_graph_edges.relation_type` |

---

## 六、落地顺序与验证

分五步，每步都能独立验证。不通就地停下，不要往下叠。

### 第 1 步：BioDSA 侧五处改动 + 用 curl 证明通知上了响应流

改 §3.1–3.6，重启 BioDSA MCP 服务（`--transport streamable-http`）。

**这一步的验证最关键，而且不需要动 WeKnora**。用原始 HTTP 直接打，肉眼确认通知帧和最终结果在同一条流上：

```bash
# ① 握手，拿 Mcp-Session-Id
curl -i -X POST http://<biodsa-host>:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-03-26",
        "capabilities":{},
        "clientInfo":{"name":"curl","version":"0"}}}'

# ② 从响应头里抄出 Mcp-Session-Id，发 initialized
curl -X POST http://<biodsa-host>:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Session-Id: <上一步的值>' \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# ③ 调工具，-N 关闭缓冲，实时看 SSE 帧
curl -N -X POST http://<biodsa-host>:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Session-Id: <同一个值>' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
        "name":"biodsa_deepevidence_research",
        "arguments":{
          "research_question":"EGFR inhibitor resistance mechanisms in NSCLC",
          "knowledge_bases":["gene","drug"],
          "session_id":"manual-test-001"}}}'
```

**通过标准**：③ 的输出里，在最终 `"id":2` 的结果之前，能持续看到

```
event: message
data: {"jsonrpc":"2.0","method":"notifications/biodsa/graph_event","params":{"session_id":"manual-test-001","seq":1,"event":{...}}}
```

`seq` 从 1 开始严格递增、不跳号、不重复。如果一条通知都看不到，说明 `related_request_id` 没生效（回去查 `ctx.request_id` 取值）。如果 `seq` 有重复的 `event_id`，说明 §3.5 或 §3.6 的去重没做干净。

顺便验 §3.4：等到 `seq` 走过若干个 `Progress` 事件，检查 `entities_found` 是否随时间增长而不是恒为 0。

### 第 2 步：WeKnora 收到通知（只加日志，先不落库）

做 §4.1 注入、§4.2 注册表、§4.3 `OnNotification`，`Dispatch` 里先只打日志：

```go
logger.Infof(ctx, "[GraphStream] recv seq=%d type=%s tool_call=%s", seq, eventType, toolCallID)
```

**验证**：在 WeKnora 前端发起一次会走 BioDSA 工具的对话，`docker logs weknora-app | grep GraphStream`，应看到连续递增的 seq。

**这一步最容易踩的两个坑**：

- `session_id` 没注进去 → BioDSA 那边 `session_id` 为 None，压根不会启动通知消费者。先在 BioDSA 日志里确认有 `MCP notification consumer started for session=call_xxx`。
- 注入了但 `declaresSessionID()` 判断写错，导致别的 MCP 服务收到未知参数报错。灰度期间可以先只对特定 service name 生效。

### 第 3 步：接到 EventBus 和 SSE

做 §4.4、§4.5（`agentGraphService` 先传 nil）。

**验证**：浏览器开 DevTools → Network → 找到 `chat` 那条 SSE 请求 → 看 EventStream 面板，应该出现 `"response_type":"agent_graph"` 的帧，且和 `thinking` / `tool_call` 帧交错出现。到这一步"在线输出"就已经通了。

### 第 4 步：落库

做 §4.6、§4.7，跑迁移。

**验证**：

```sql
SELECT status, step, entity_count, relation_count, last_seq
  FROM agent_graph_runs WHERE message_id = '<msg>';

SELECT seq, event_type FROM agent_graph_events
  WHERE message_id = '<msg>' ORDER BY seq LIMIT 20;

-- 幂等性：seq 不应有空洞，也不应有重复
SELECT count(*), count(DISTINCT seq), min(seq), max(seq)
  FROM agent_graph_events WHERE message_id = '<msg>';
```

`count(*) = count(DISTINCT seq) = max(seq)` 就说明既没丢也没重。

再验对账：跑完之后比对 `entity_count` 和 `RunComplete.entities` 的长度，两者应一致（不一致说明增量漏了，但对账已经补上，可以查 events 表定位漏在哪一段 seq）。

### 第 5 步：查询接口

做 §4.8。

**验证**：

```bash
# 运行中（另开一个终端，趁 DeepEvidence 还在跑）
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8080/api/v1/sessions/$SID/messages/$MID/graph"
# 期望 run.status == "running"，nodes 非空且随时间增长

# 增量
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8080/api/v1/sessions/$SID/messages/$MID/graph?after_seq=100"
# 期望只返回 last_seq > 100 的节点/边

# 跑完之后再查，验证"刷新页面还能拿到"
# 期望 run.status == "completed"，且和运行中最后一次拿到的是同一份图谱
```

---

## 七、风险与注意事项

**MCP 客户端是全局共享的，这是最大的正确性风险。** `manager.go:46-49` 显示非 OAuth 服务的缓存键就是 `service.ID`，所有租户所有会话共用一个 client、一个 `OnNotification` 回调。注册表的 `session_id` 路由不是优化，是正确性前提。测试时务必并发跑两个会话，确认事件不串台。

**通知回调在别的 goroutine 上，ctx 生命周期要小心。** 回调发生在 mcp-go 的 SSE 读取 goroutine，而不是工具执行 goroutine。用工具执行的 ctx 去写 DB，工具一返回 ctx 就被取消，最后几条事件（包括 RunComplete）会写失败。必须用 `context.WithoutCancel(ctx)`——它保留 tenant_id 等 value，但不跟着取消。

**事件顺序在 BioDSA 侧是保证的，在 WeKnora 侧不是。** 通知在单条 SSE 流上按序到达、`readSSE` 也是单 goroutine 顺序回调，所以到 `Dispatch` 为止有序。但如果 EventBus 是异步模式（`NewAsyncEventBus`，`event.go:108-114`），handler 会各自起 goroutine，顺序就没了。落库时**不要依赖到达顺序**，一切以 payload 里的 `seq` 为准——这也是为什么 `seq` 要由 BioDSA 分配而不是 WeKnora 自增。

**RunComplete 可能很大。** 几百个实体加上每个实体的 observations，单条通知可能几百 KB 到几 MB。好消息是 mcp-go 的 `readSSE` 用的是 `bufio.Reader.ReadString`（`streamable_http.go:773-777`）而不是 `bufio.Scanner`，**没有 64KB 的 token 上限**，缓冲区会动态增长。但仍要注意：这条消息会整个进内存、整个写 JSONB。建议 `agent_graph_events.payload` 对 `RunComplete` 只存统计字段，完整快照直接消费进 nodes/edges 后丢弃，不要原样入库。

**事件量估算。** 一次 DeepEvidence 大约 30-100 个 superstep，每步 0-5 个事件，加上每 5 步一个 Progress，量级在几百条。对 Redis List 和 Postgres 都不构成压力。但如果将来放开 `recursion_limit`（现在是 100，`agent.py:787`），要重新评估。

**并发跑同一 session_id 的可能性。** `ToolCallID` 由 LLM 生成，理论上唯一，但不同 provider 的格式差异很大，有的会重复（比如某些本地模型固定生成 `call_1`）。`agent_graph_runs.tool_call_id` 上的唯一索引会直接冲突。建议注入时用 `meta.AssistantMessageID + ":" + meta.ToolCallID` 拼一个复合 key，比裸 ToolCallID 稳。

**BioDSA 的 `_broadcaster` 是进程级全局单例**（`mcp_server_standalone.py:37`）。按 session_id 分队列，并发本身没问题。但如果同一个 `session_id` 被复用（见上一条），`create_run` 里的 `if session_id not in self._queues` 会静默跳过，第二个运行的事件会全部灌进第一个运行的队列。复合 key 同时也解决了这个问题。

**灰度策略。** `agentGraphService` 允许为 nil、`declaresSessionID()` 做 schema 检查、`Dispatch` 找不到 sink 就丢弃——这三处的降级路径要真的测一遍。任何一环出问题，最坏结果应该是"图谱不显示"，而不是"对话崩了"。所有新增的 DB 写入和 SSE 追加都要 `logger.Error` 后继续，绝不能 return error 把 agent 主流程带崩——参照 `agent_stream_handler.go` 现有各 handler 的处理方式，它们全都是记日志后 `return nil`。

**别碰的地方。** 按既定范围，`BaseAgent` 和其余 6 个智能体一行不改。所有 BioDSA 侧改动限定在 `deepevidence/agent.py`、`narrative/broadcaster.py`、`scripts/mcp_server_standalone.py` 三个文件。

---

## 八、施工前仍可讨论的点

**四张表是不是太重。** 事件流表是唯一真相，节点和边表只是派生投影，理论上可以砍掉只留事件流、让前端自己回放聚合。保留投影的理由是"当前图谱长什么样"变成一条 SELECT，而不是每次都回放几百条事件。如果前期想轻量，砍到两张表也能跑。

**`RunComplete` 快照要不要原样入库。** 方案建议快照消费进节点/边表后就丢弃，事件表只留统计字段。代价是将来若对账逻辑有 bug，没有原始快照可回溯。

**复合 session_id 会影响 BioDSA 的目录命名。** BioDSA 把 `session_id` 用作图谱目录名的一部分。若传 `msgid:callid` 这种带冒号的字符串，Linux 无碍，Windows 上做目录名会失败。采用复合键的话，BioDSA 侧需加一次文件名安全化处理。

---

## 附录：关键源码位置速查

### BioDSA

| 内容 | 位置 |
|---|---|
| MCP 服务入口、通知消费任务 | `scripts/mcp_server_standalone.py:70-164` |
| 事件模型定义 | `biodsa/narrative/events.py` |
| 事件提取（从 tool_calls） | `biodsa/narrative/extractor.py` |
| 广播器 | `biodsa/narrative/broadcaster.py` |
| DeepEvidence 流循环与事件发射 | `biodsa/agents/deepevidence/agent.py:782-840` |
| 图谱目录清理 | `biodsa/agents/deepevidence/agent.py:874-883` |
| 图谱存储管理器 | `biodsa/memory/memory_graph/graph.py` |

### WeKnora

| 内容 | 位置 |
|---|---|
| MCP 客户端创建、回调注册 | `internal/mcp/client.go:149-239` |
| MCP 客户端缓存键 | `internal/mcp/manager.go:46-49` |
| MCP 工具执行 | `internal/agent/tools/mcp_tool.go:103-269` |
| 工具执行上下文 | `internal/agent/tools/exec_context.go` |
| 工具超时常量 | `internal/agent/const.go:24-26`（已在服务器改为 3000s） |
| 工具执行入口 | `internal/agent/act.go:457-484` |
| Agent 流式事件处理 | `internal/handler/session/agent_stream_handler.go` |
| 流管理器接口 | `internal/types/interfaces/stream_manager.go:26-31` |
| Redis 流实现 | `internal/stream/redis_manager.go` |
| 断线续传 | `internal/handler/session/stream.go` |
| 现有图谱查询工具 | `internal/agent/tools/graph_query.go` |
| 现有图谱结果渲染 | `frontend/src/views/chat/components/tool-results/GraphQueryResults.vue` |

### mcp-go v0.52.0

| 内容 | 位置 |
|---|---|
| Streamable HTTP 传输 | `client/transport/streamable_http.go` |
| POST 响应流上的通知派发 | `client/transport/streamable_http.go:689-725` |
| 独立 GET 流开关（默认关闭） | `client/transport/streamable_http.go:25-36` |
| SSE 传输的硬编码 60s 响应超时 | `client/transport/sse.go:126-135` |
| 通知回调注册 | `client/client.go:129-135` |
| 自定义 params 字段容器 | `mcp/types.go:223-229` |
