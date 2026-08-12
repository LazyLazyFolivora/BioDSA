# BioDSA 推理过程可视化 — 设计文档

## 目标

将 DeepEvidence agent 的推理过程实时可视化，在 WeKnora 前端内展示为一张**生长的知识图谱**，而非传统的机械步骤日志。

---

## 整体架构

```
┌────────── WeKnora ──────────┐          ┌─── BioDSA MCP Server ───┐
│                              │          │                          │
│  用户发消息                   │──POST /mcp──→  MCP tool 入口        │
│                              │  (streamable HTTP)   │             │
│  收到 run_id                 │←────────────────── 返回 run_id      │
│                              │                     │             │
│                              │                     ▼             │
│  [知识图谱组件]               │                Agent 开始运行       │
│                              │                     │             │
│  new EventSource() ──────────│──GET /progress────→  SSE 端点       │
│      (SSE)                   │    ?run_id=xxx       │             │
│                              │                     ▼             │
│  收到 entity_found ──────────│←────────────────── 叙事事件流       │
│  收到 relation_found ────────│←──────────────────                 │
│  收到 phase_change ──────────│←──────────────────                 │
│                              │                     │             │
│  Cytoscape.js 渲染           │                MCP 返回最终结果      │
│  知识图谱实时生长             │                     │             │
│                              │                          │          │
└──────────────────────────────┘          └──────────────────────────┘
```

**两条独立连接：**

| 连接 | 发起方 | 协议 | 路由 | 用途 |
|------|--------|------|------|------|
| MCP 工具调用 | WeKnora 后端 → BioDSA | streamable HTTP | `POST /mcp` | 触发 agent，拿到 run_id，等待最终结果 |
| 进度事件流 | 浏览器 → BioDSA | SSE | `GET /progress?run_id=xxx` | 实时推送知识图谱事件 |

**关键点：SSE 是浏览器直接连 BioDSA，不经过 WeKnora 后端。** BioDSA 需要加 CORS 头允许浏览器跨域连接。

---

## 一、BioDSA 后端要做的事

### 1.1 叙事事件模型

新增 `biodsa/narrative/events.py`：

```python
from dataclasses import dataclass, asdict
from typing import Optional
import json
import uuid
import time


@dataclass(frozen=True)
class NarrativeEvent:
    """基类"""
    event_id: str            # UUID
    timestamp: float         # 从 run 开始的秒数
    event_type: str          # 事件类型字符串
    message: str             # 人类可读的一句话


@dataclass(frozen=True)
class EntityFound(NarrativeEvent):
    """新实体被发现"""
    entity_name: str         # "EGFR"
    entity_type: str         # gene | drug | disease | mutation | pathway | compound | target | literature
    source_kb: str           # 来源知识库
    summary: str = ""        # 一句话描述

    def __post_init__(self):
        object.__setattr__(self, "event_type", "entity_found")


@dataclass(frozen=True)
class RelationFound(NarrativeEvent):
    """两个实体间的关系被建立"""
    source_entity: str       # "EGFR"
    target_entity: str       # "T790M"
    relation_type: str       # mutation | targets | treats | associated_with | regulates
    evidence_source: str     # 来源知识库
    evidence_strength: float # 0.0 ~ 1.0
    contradictory: bool = False

    def __post_init__(self):
        object.__setattr__(self, "event_type", "relation_found")


@dataclass(frozen=True)
class PhaseChange(NarrativeEvent):
    """研究阶段切换"""
    phase: str               # orchestrating | broad_search | deep_dive | synthesizing
    detail: str = ""         # 如 "广度检索: 基因、文献、变异三个知识库"

    def __post_init__(self):
        object.__setattr__(self, "event_type", "phase_change")


@dataclass(frozen=True)
class ContradictionFound(NarrativeEvent):
    """矛盾证据被发现"""
    entity_a: str
    entity_b: str
    description: str

    def __post_init__(self):
        object.__setattr__(self, "event_type", "contradiction_found")


@dataclass(frozen=True)
class ProgressUpdate(NarrativeEvent):
    """进度统计"""
    total_steps: int
    tool_calls_made: int
    entities_by_type: dict   # {"gene": 5, "drug": 2, "disease": 1, ...}

    def __post_init__(self):
        object.__setattr__(self, "event_type", "progress")
```

### 1.2 事件提取器

新增 `biodsa/narrative/extractor.py`。

**核心逻辑：从 `generate()` stream 的每一步中提取结构化事件，不需要额外 LLM 调用。**

提取策略分两步：

**Step 1 — 处理 AI 的 tool_call（识别"agent 即将查什么"）：**

```python
TOOL_TO_ENTITY_TYPE = {
    # gene
    "unified_gene_search":      ("gene",       lambda a: a["search_term"]),
    "fetch_gene_details":        ("gene",       lambda a: a["gene_id"]),
    # disease
    "unified_disease_search":    ("disease",    lambda a: a["search_term"]),
    "fetch_disease_details":     ("disease",    lambda a: a["disease_id"]),
    # drug
    "unified_drug_search":       ("drug",       lambda a: a["search_term"]),
    "fetch_drug_details":        ("drug",       lambda a: a["drug_id"]),
    # variant / mutation
    "search_variants":           ("mutation",   lambda a: a["search_term"]),
    "fetch_variant_details":     ("mutation",   lambda a: a["variant_id"]),
    # target
    "unified_target_search":     ("target",     lambda a: a["search_term"]),
    # compound
    "unified_compound_search":   ("compound",   lambda a: a["search_term"]),
    # pathway
    "unified_pathway_search":    ("pathway",    lambda a: a["search_term"]),
    # literature
    "search_papers":             ("literature", lambda a: a["query"]),
    "find_entities":             ("literature", lambda a: a["entity_name"]),
}

PHASE_TOOLS = {
    "go_breadth_first_search":   "broad_search",
    "go_depth_first_search":     "deep_dive",
}
```

**Step 2 — 处理 ToolMessage（从返回文本中解析"agent 实际发现了什么"）：**

```python
# 正则从 tool 输出文本中抓取实体
PATTERNS = {
    "gene":      r"\b([A-Z][A-Z0-9]{1,10})\b(?=.*(?:gene|mutation|kinase|receptor|oncogene))",
    "mutation":  r"\b([A-Z]\d{2,4}[A-Z])\b",   # e.g. T790M, L858R
    "drug":      r"\b(\w+(?:tinib|mab|zomib|cycline|parib|lisib))\b",
    "disease":   r"\b(?:NSCLC|adenocarcinoma|carcinoma|lymphoma|leukemia|melanoma)\b",
}
```

**提取函数入口：**

```python
def extract_events(message, step_num: int, tool_results_cache: dict) -> list[NarrativeEvent]:
    """
    message: AIMessage 或 ToolMessage（来自 stream chunk）
    返回该 step 对应的叙事事件列表
    """
    events = []

    if isinstance(message, AIMessage) and message.tool_calls:
        for tc in message.tool_calls:
            name = tc["name"]
            args = tc.get("args", {})

            # 阶段切换
            if name in PHASE_TOOLS:
                events.append(PhaseChange(...))

            # 实体发现
            if name in TOOL_TO_ENTITY_TYPE:
                entity_type, extractor = TOOL_TO_ENTITY_TYPE[name]
                entity_name = extractor(args)
                events.append(EntityFound(
                    entity_name=entity_name,
                    entity_type=entity_type,
                    source_kb=args.get("knowledge_base", "unknown"),
                ))

    elif isinstance(message, ToolMessage):
        # 从返回文本中抓关系线索
        text = message.content or ""
        for entity_type, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                events.append(EntityFound(...))

        # 检查矛盾信号词
        if any(kw in text for kw in ["contradictory", "inconsistent", "conflicting"]):
            events.append(ContradictionFound(...))

    # 始终追加进度
    events.append(ProgressUpdate(...))
    return events
```

### 1.3 SSE 事件广播器

新增 `biodsa/narrative/broadcaster.py`：

```python
import asyncio
import json
from dataclasses import asdict

class EventBroadcaster:
    """管理多个 run 的 SSE 事件队列"""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}

    def create_run(self, run_id: str) -> None:
        self._queues[run_id] = asyncio.Queue()

    def close_run(self, run_id: str) -> None:
        queue = self._queues.pop(run_id, None)
        if queue:
            queue.put_nowait(None)  # 哨兵，通知 SSE 连接结束

    async def emit(self, run_id: str, event) -> None:
        queue = self._queues.get(run_id)
        if queue:
            await queue.put(event)

    async def subscribe(self, run_id: str):
        """生成 SSE 文本流"""
        queue = self._queues.get(run_id)
        if not queue:
            yield f"event: error\ndata: unknown run_id\n\n"
            return
        while True:
            event = await queue.get()
            if event is None:  # 哨兵，run 结束
                yield f"event: done\ndata: complete\n\n"
                return
            payload = json.dumps(asdict(event), ensure_ascii=False)
            yield f"event: {event.event_type}\ndata: {payload}\n\n"
```

### 1.4 修改 DeepEvidenceAgent.generate() 接入事件流

在 `biodsa/agents/deepevidence/agent.py` 的 `generate()` 方法中：

```python
# 现有 stream 循环体内（line ~800），新增：
if self._broadcaster and self._run_id:
    events = extract_events(last_message, step_num, self._tool_cache)
    for event in events:
        asyncio.create_task(self._broadcaster.emit(self._run_id, event))
```

改动：每步执行后 ~6 行代码。

### 1.5 修改 MCP 工具函数，返回 run_id

在 `scripts/mcp_server_standalone.py` 的 `tool_deepevidence_research()` 中：

```python
# 修改返回：在结果末尾附加 viewer 连接信息
run_id = uuid.uuid4().hex
broadcaster.create_run(run_id)
agent._broadcaster = broadcaster
agent._run_id = run_id

results = agent.go(**go_kwargs)

# 返回最终结果 + viewer 连接提示
return _fmt_results(results) + (
    f"\n\n---\n"
    f"### 推理过程可视化\n"
    f"将 run_id 填入前端知识图谱组件查看推理过程：\n"
    f"run_id: `{run_id}`"
)
```

### 1.6 MCP Server 新增 SSE 路由 + CORS

在 `scripts/mcp_server_standalone.py` 中新增：

```python
from starlette.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware

# CORS：允许浏览器跨域访问 SSE 端点
starlette.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

async def progress_endpoint(request):
    run_id = request.query_params.get("run_id", "")
    return StreamingResponse(
        broadcaster.subscribe(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

starlette.routes.append(Route("/progress", endpoint=progress_endpoint))
```

### 1.7 后端文件清单

| 文件 | 作用 | 行数 |
|------|------|------|
| `biodsa/narrative/__init__.py` | 包初始化，导出 public API | 5 |
| `biodsa/narrative/events.py` | 5 种事件 dataclass | 70 |
| `biodsa/narrative/extractor.py` | tool_call → 叙事事件提取 | 100 |
| `biodsa/narrative/broadcaster.py` | SSE 事件队列 + subscribe 生成器 | 45 |
| `biodsa/agents/deepevidence/agent.py` | generate() 中接入事件流 (+6行) | +6 |
| `scripts/mcp_server_standalone.py` | CORS + /progress 路由 + run_id 生成 | +25 |

**后端总新增：~250 行。**

---

## 二、WeKnora 前端要做的事

### 2.1 组件位置

在 WeKnora 消息流中，当 MCP 工具 `biodsa_deepevidence_research` 被调用时，消息卡片内嵌入知识图谱面板。

```
┌────────────────────────────────────────────┐
│  用户消息：EGFR 抑制剂在 NSCLC 中的耐药...    │
├────────────────────────────────────────────┤
│  ┌── BioDSA 正在研究 ────────────────────┐  │
│  │                                        │  │
│  │  ┌─────────────────┬────────────────┐  │  │
│  │  │  知识图谱画布    │   进度面板      │  │  │
│  │  │  (Cytoscape.js) │   阶段: 广度检索│  │  │
│  │  │                 │   基因: 5      │  │  │
│  │  │  ● EGFR ——— ◇  │   药物: 3      │  │  │
│  │  │   │       osim  │   变异: 7      │  │  │
│  │  │   │             │               │  │  │
│  │  │  ◆ T790M        │   最近发现:     │  │  │
│  │  │                 │   ✓ T790M...   │  │  │
│  │  │  新节点浮现中... │               │  │  │
│  │  └─────────────────┴────────────────┘  │  │
│  │                                        │  │
│  │  ── 最终结果 ────────────────────────  │  │
│  │  EGFR 的耐药机制主要包括...             │  │
│  └────────────────────────────────────────┘  │
└────────────────────────────────────────────┘
```

### 2.2 交互流程

```
1. 用户在 WeKnora 提问
2. WeKnora 调用 MCP 工具 biodsa_deepevidence_research
3. BioDSA 返回首包，包含 run_id
4. WeKnora 前端拿到 run_id，创建 EventSource 连接：
   const es = new EventSource(`http://172.17.0.1:8765/progress?run_id=${run_id}`)
5. 每个 SSE 事件 → 调用 Cytoscape API 增删节点/边
6. agent 跑完 → MCP 返回最终结果文本 → 展示在面板下方
```

### 2.3 知识图谱画布

**节点类型与配色：**

| 实体类型 | 颜色 | CSS 色值 | 形状 |
|---------|------|---------|------|
| gene | 蓝 | `#4A90D9` | 圆形 (ellipse) |
| drug | 红 | `#E74C3C` | 菱形 (diamond) |
| disease | 橙 | `#F39C12` | 方形 (rectangle) |
| mutation | 紫 | `#9B59B6` | 六边形 (hexagon) |
| pathway | 绿 | `#2ECC71` | 三角形 (triangle) |
| compound | 青 | `#1ABC9C` | 圆形 |
| target | 深橙 | `#E67E22` | 菱形 |
| literature | 灰 | `#95A5A6` | 小圆点 (10px) |

**动画效果：**

- **节点浮现**：`fadeIn` + `scale(0→1)`，持续 400ms
- **边生长**：source→target 描线动画，持续 500ms
- **BFS 阶段**：同一批事件分组渲染，多个节点几乎同时浮现
- **DFS 阶段**：从中心节点向外辐射长出子节点
- **矛盾关系**：红色虚线 `line-style: dashed`，闪烁 2 秒后稳定

**交互：**
- 点击节点 → tooltip 显示实体名、类型、来源
- 悬停节点 → 高亮关联边，其他变暗
- 滚轮缩放、拖拽平移

### 2.4 进度面板

紧凑地放在画布右侧或底部：

```
当前阶段：广度检索 — BFS 探索中       已用时 2:34

基因  ████████ 5    药物 ███ 2    疾病 ██ 1
变异  ██████████ 7  文献 ████████████████ 23

最近：
✓ 发现 EGFR 耐药突变 T790M (证据强度 0.87)
✓ PubMed 找到 23 篇相关文献
⚠ 矛盾：C797S 对 osimertinib 的影响方向不一致
→ 正在深入检索 C797S...
```

### 2.5 SSE 事件处理逻辑

```javascript
const es = new EventSource(`http://${biodsaHost}:${port}/progress?run_id=${runId}`);

es.addEventListener("entity_found", (e) => {
  const evt = JSON.parse(e.data);
  // 如果节点不存在，添加到 cytoscape
  if (!cy.getElementById(evt.entity_name).length) {
    cy.add({
      group: "nodes",
      data: { id: evt.entity_name, type: evt.entity_type, label: evt.entity_name },
    });
    cy.getElementById(evt.entity_name).animate({
      style: { opacity: 1, scale: 1 },
      duration: 400,
    });
  }
  updateCounters(evt.entity_type);
});

es.addEventListener("relation_found", (e) => {
  const evt = JSON.parse(e.data);
  const edgeId = `${evt.source_entity}→${evt.target_entity}`;
  if (!cy.getElementById(edgeId).length) {
    cy.add({
      group: "edges",
      data: {
        id: edgeId,
        source: evt.source_entity,
        target: evt.target_entity,
        relation: evt.relation_type,
        strength: evt.evidence_strength,
        contradictory: evt.contradictory,
      },
    });
    // 矛盾关系用红色虚线
    if (evt.contradictory) {
      cy.getElementById(edgeId).style({
        "line-color": "#E74C3C",
        "line-style": "dashed",
      });
    }
  }
  // 更新线条粗细 = f(evidence_strength)
  cy.getElementById(edgeId).style({
    width: Math.max(1, evt.evidence_strength * 6),
  });
});

es.addEventListener("phase_change", (e) => {
  const evt = JSON.parse(e.data);
  updatePhaseIndicator(evt.phase, evt.detail);
});

es.addEventListener("done", () => {
  es.close();
  showCompletionState();
});
```

### 2.6 技术选型

| 层 | 选择 | 理由 |
|----|------|------|
| 图可视化 | **Cytoscape.js** | 生物信息学标准，支持动态增删节点，cose-bilkent 力导向布局 |
| 布局算法 | **cose-bilkent** | 为生物网络优化的复合力导向布局 |
| 数据通信 | **EventSource** | 浏览器原生 SSE API，自动重连，零依赖 |
| 框架 | 跟随 WeKnora 现有技术栈 | 不需要引入新框架，Cytoscape 纯 JS |

---

## 三、完整数据流示例

用户问："EGFR 抑制剂在 NSCLC 中的耐药机制是什么？"

```
时间轴               SSE 事件                                      知识图谱渲染
─────────────────────────────────────────────────────────────────────────────
0s               phase_change: orchestrating              面板显示"正在规划检索策略..."
                 MCP 返回 run_id，前端建立 SSE 连接

3s               phase_change: broad_search               画布中心出现搜索图标
                 detail: "BFS: gene, pubmed, variant"

5s               entity_found: EGFR (gene)                蓝色圆节点 "EGFR" 从中心浮现
                 source_kb: gene

7s               entity_found: T790M (mutation)           紫色六边形 "T790M" 浮现
                                                        EGFR→T790M 边开始生长

9s               entity_found: PMID:34567890 (literature)  灰色小圆点浮现在 T790M 附近

12s              relation_found:                          连线 EGFR→T790M 加粗
                 source=EGFR, target=T790M                标签显示 "耐药突变"
                 relation=mutation, strength=0.87

15s              entity_found: osimertinib (drug)         红色菱形 "osimertinib" 浮现

18s              relation_found:                          连线 osimertinib→T790M 生长
                 source=osimertinib, target=T790M          标签显示 "靶向"
                 relation=targets, strength=0.92

21s              contradiction_found:                     连线 C797S→osimertinib 变红色虚线
                 entity_a=C797S, entity_b=osimertinib      闪烁 2 秒稳定
                 desc="C797S 突变对 osimertinib
                       的影响方向不一致"

25s              phase_change: deep_dive                  面板切换 "深入挖掘 C797S..."
                 detail: "DFS: gene, variant"

28s              entity_found: (更多节点和关系...)         图谱继续向外扩展

...3-5 分钟后...
─────────────────────────────────────────────────────────────────────────────
done             最终结果已生成                             图谱静止，所有连线渲染完成
```

---

## 四、实施顺序

| 阶段 | 内容 | 预计 |
|------|------|------|
| **Phase 1** | `events.py` + `extractor.py` + `broadcaster.py` | 30 min |
| **Phase 2** | 改 `agent.py` generate() + MCP server CORS 和 `/progress` 路由 | 20 min |
| **Phase 3** | WeKnora 前端：Cytoscape 容器组件 + SSE EventSource 消费 + 动画 | 1.5 hr |
| **Phase 4** | 端到端联调 + 事件提取质量调优 | 30 min |

---

## 五、注意事项

### CORS

浏览器直接连 BioDSA 的 SSE 端点，必须加 CORS 头。如果 WeKnora 前端和 BioDSA 不在同一 host/port，需要：

```python
from starlette.middleware.cors import CORSMiddleware
starlette.add_middleware(CORSMiddleware, allow_origins=["*"], ...)
```

如果 WeKnora 有反向代理（如 Nginx），也可以代理 `/progress` 路径避免跨域。

### run_id 提取

WeKnora 前端需要从 MCP 工具返回结果中解析 `run_id`。如果 WeKnora 支持结构化 MCP 返回，可以把 run_id 放在 JSON 字段里；否则用正则从文本中提取：

```javascript
const match = toolResult.match(/run_id:\s*`([a-f0-9]+)`/);
const runId = match ? match[1] : null;
```

### 事件队列内存

长时间运行的 run 会产生大量事件。broadcaster 需要设置最大队列长度，超出后丢弃旧的 progress 事件（entity_found 和 relation_found 不丢）。
