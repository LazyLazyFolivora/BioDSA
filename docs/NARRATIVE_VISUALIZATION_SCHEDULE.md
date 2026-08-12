# 推理过程可视化 — 进度安排（2 周）

## 架构（最终确认）

```
BioDSA                          WeKnora 后端                       WeKnora 前端
─────                           ────────────                       ────────────
MCP POST /mcp  ←────session_id──── 生成 session_id
agent 执行                          │
  extract_events                    │ goroutine 并行读
  broadcaster.emit_sync             │
       ↓                            ↓
GET /progress?session_id=xxx ──→ SSE 事件流
                                  EventBus.Emit(EventAgentGraph)
                                  AgentStreamHandler
                                  StreamManager
                                       │
                                       ↓ SSE ──→ Cytoscape 渲染
                                                        ↓
agent 结束                          RunComplete 收到
                                       │
                                       ↓
                                  POST /api/graph/snapshot → DB

下次打开对话                       GET /api/graph/{id} → DB → 恢复图谱
```

- 浏览器不直连 BioDSA，不涉及 CORS/SSRF/网络突破
- 图谱事件不走 MCP 协议，不涉及 mcp-go fork
- WeKnora 后端生成 session_id，不存在时序问题

---

## 范围

| 侧 | 内容 | 工作项 |
|----|------|--------|
| BioDSA | 事件模型 + 提取器 + 广播器 + Stream Hook + MCP Server | ~350 行 |
| WeKnora 后端 | 新事件类型 + Handler + MCP 并行读取 + 持久化表 + API | ~200 行 |
| WeKnora 前端 | Cytoscape 组件 + SSE 消费 + 动画 | 不在本期 |

---

## Week 1

### Day 1 — BioDSA: 事件模型

`biodsa/narrative/events.py`

7 种 frozen dataclass，`entity_type` 归一化映射表，单元测试。

### Day 2 — BioDSA: 事件提取器 + 广播器

`biodsa/narrative/extractor.py` + `biodsa/narrative/broadcaster.py`

两个模块可以写完之后分别写单测，互不依赖。

### Day 3 — BioDSA: Stream Hook + MCP Server

- `agent.py:generate()` 内 hook extract + emit
- `mcp_server_standalone.py`: session_id 参数、/progress 路由、线程池
- WebKnora 后端开发者同步开始调研：EventBus 扩展点、MCP 工具执行链路

### Day 4 — BioDSA: 端到端验证 + WeKnora: 事件流

**BioDSA:** curl 跑真实 DeepEvidence 请求，验证 `/progress` SSE 事件流完整。

**WeKnora 后端:**
- `event.go` 新增 `EventAgentGraph`
- `types` 新增 `ResponseTypeGraph`
- `agent_stream_handler.go` 订阅转发
- `mcp_tool.go` 改 `Execute()`，启动 goroutine 并行读 `/progress`

### Day 5 — 合流验证

WeKnora 调 BioDSA → 图谱事件流过 EventBus → StreamManager → 前端收到 `ResponseTypeGraph` 事件。

---

## Week 2

### Day 6 — WeKnora: 持久化表 + API

- Migration: `biodsa_graph_snapshots` 表
- `POST /api/biodsa/graph/snapshot` — RunComplete 时落库
- `GET /api/biodsa/graph/{session_id}` — 回显

### Day 7 — 异常场景

**BioDSA:**
- agent 异常退出时 broadcaster 清理（finally）
- 并发 3 run 资源竞争

**WeKnora:**
- session_id 无效时 `/progress` 返回错误，前端提示
- goroutine 断连重试
- graph snapshot 幂等（重复 POST 不报错）

### Day 8 — 回顾与打磨

- extract_events 覆盖所有 10 个 KB 的 tool name
- entity_type 归一化两套体系对齐
- 全链路日志（session_id 贯穿 BioDSA + WeKnora）
- 事件字段序列化格式确认（前端开发者评审）

### Day 9-10 — 联调缓冲 + 文档

- BioDSA + WeKnora + 前端三方联调
- 性能测试（并发 run 的资源占用）
- 部署文档（session_id 配置、环境变量）
- 问题修复缓冲

---

## 依赖关系

```
BioDSA:
  events ──→ extractor ──→ stream hook ──→ MCP server ──→ 端到端
  broadcaster ────────────→ stream hook

WeKnora:
  事件类型 ──→ Handler ──→ MCP 并行读取 ──→ 合流验证
                                          ──→ 持久化

汇合:
  BioDSA 端到端 + WeKnora 事件流 ──→ W1D5 合流
  合流 ──→ 异常场景 ──→ 打磨 ──→ W2D4-5 联调
```

Day 1-3 BioDSA 和 WeKnora 后端可并行开发。

---

## 关键决策点

| 决策 | 结论 | 理由 |
|------|------|------|
| 图谱事件走什么通道 | BioDSA `/progress` SSE → WeKnora goroutine 读取 | 不碰 mcp-go，不用浏览器直连 |
| session_id 谁生成 | WeKnora 后端 | 不存在时序问题 |
| 持久化谁负责 | WeKnora（表 + API） | 图谱属于对话产物 |
| 前端回显谁负责 | WeKnora（从表读） | 同上 |
