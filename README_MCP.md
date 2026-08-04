# BioDSA MCP Server — 本地模型启动指南

## 架构

```
┌──────────────┐   SSE/HTTP    ┌──────────────────┐   OpenAI API   ┌─────────────┐
│  Claude Code │ ◄───────────► │  BioDSA MCP      │ ◄───────────► │  vLLM        │
│  (or Cursor) │   :8765       │  Server           │   :8000       │  (本地GPU)   │
└──────────────┘               └──────┬───────────┘               └─────────────┘
                                      │
                               ┌──────┴──────────┐
                               │  Docker Sandbox  │
                               │  (代码执行隔离)   │
                               └─────────────────┘
```

## 前提

- Python 3.12
- Docker（代码执行沙箱）
- vLLM 已启动并暴露 OpenAI 兼容端口

### 启动 vLLM（如未启动）

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 测试 vLLM 连通性

```bash
python scripts/test_llm_connection.py \
    --llm-host localhost --llm-port 8000
```

通过后会自动提示下一步命令。

### 3. 启动 MCP Server

```bash
python scripts/start_mcp_server.py \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --llm-host localhost --llm-port 8000
```

输出示例：
```
BioDSA MCP server starting on http://0.0.0.0:8765 (SSE)
Model: meta-llama/Llama-3.1-8B-Instruct  |  LLM endpoint: http://localhost:8000/v1
Sandbox image: biodsa-sandbox-py:latest
```

### 4. 配置 MCP 客户端

**Claude Code** — 编辑 `~/.claude/mcp.json`：

```json
{
  "mcpServers": {
    "biodsa": {
      "type": "sse",
      "url": "http://localhost:8765/sse"
    }
  }
}
```

**Cursor** — 在 MCP 设置中添加相同配置。

配置后重启 Claude Code / Cursor，即可在对话中调用 BioDSA 工具。

## CLI 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--model`, `-m` | (必填) | vLLM 中的模型名 |
| `--llm-host` | `localhost` | vLLM 服务器 IP |
| `--llm-port` | `8000` | vLLM 服务器端口 |
| `--mcp-port`, `-p` | `8765` | MCP SSE 服务端口 |
| `--api-key` | `not-needed` | API Key（本地 vLLM 无需） |
| `--sandbox-image` | `biodsa-sandbox-py:latest` | Docker 沙箱镜像 |
| `--tool-timeout` | `600` | 单次工具调用超时（秒） |
| `--llm-timeout` | `120` | 单次 LLM 调用超时（秒） |

## 环境变量

可在 `.env` 文件中配置，避免每次输入参数：

```bash
BIODSA_LLM_HOST=localhost
BIODSA_LLM_PORT=8000
BIODSA_LLM_MODEL=meta-llama/Llama-3.1-8B-Instruct
BIODSA_LLM_API_KEY=not-needed
BIODSA_MCP_PORT=8765
BIODSA_SANDBOX_IMAGE=biodsa-sandbox-py:latest
```

## 远程 GPU 部署

vLLM 跑在远端机器时：

```bash
# MCP Server 所在机器
python scripts/start_mcp_server.py \
    --model deepseek-ai/DeepSeek-R1 \
    --llm-host 192.168.1.100 --llm-port 8000 \
    --mcp-port 8765
```

客户端连接 `http://<mcp-server-host>:8765/sse`。

## 可用工具

| MCP Tool | Agent | 用途 |
|---|---|---|
| `biodsa_dswizard_analyze` | DSWizard | 生物医学数据分析 |
| `biodsa_deepevidence_research` | DeepEvidence | 17+ 知识库深度科研 |
| `biodsa_trialgpt_match` | TrialGPT | 患者-临床试验匹配 |
| `biodsa_gene_analysis` | GeneAgent | 基因集功能分析 |
| `biodsa_clinical_risk` | AgentMD | 临床风险计算 |
| `biodsa_systematic_review` | TrialMind-SLR | 系统文献综述 |
| `biodsa_meta_analysis` | SLR-Meta | Meta 分析 |

## 使用示例

在 Claude Code 中：

> 用 biodsa_deepevidence_research 研究 KRAS G12C 突变在结直肠癌中的耐药机制

> 用 biodsa_dswizard_analyze 分析 ./biomedical_data/ 目录下的 BRCA 数据集，做 TP53 突变 vs 野生型的生存分析

> 把这个患者匹配到临床试验：58岁女性，EGFR 突变阳性非小细胞肺癌，厄洛替尼治疗后进展
