# BioDSA MCP Server — Local Model Guide

## Overview

Run BioDSA biomedical AI agents backed by a **local vLLM** model and expose them as **MCP tools** for Claude Code, Cursor, or any MCP-compatible client.

## Prerequisites

- **vLLM** serving an OpenAI-compatible endpoint, e.g.:
  ```bash
  vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
  ```
- Python 3.12 + `pipenv install`

## Quick Start

### 1. Install dependencies

```bash
pipenv install
```

### 2. Start the MCP server

```bash
python scripts/start_mcp_server.py \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --llm-host localhost --llm-port 8000
```

The server starts on `http://0.0.0.0:8765` with SSE transport.

### 3. Configure your MCP client

**Claude Code** — add to `~/.claude/mcp.json`:
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

**Cursor** — add to your Cursor MCP settings (same JSON format).

### 4. Use it

After connecting, your AI assistant will have these tools available:

| Tool | Agent | Example |
|------|-------|---------|
| `biodsa_dswizard_analyze` | DSWizard | "Analyze survival by TP53 status in my dataset" |
| `biodsa_deepevidence_research` | DeepEvidence | "Research EGFR resistance mechanisms" |
| `biodsa_trialgpt_match` | TrialGPT | "Match this patient to clinical trials" |
| `biodsa_gene_analysis` | GeneAgent | "Analyze ERBB2,EGFR,KRAS,TP53" |
| `biodsa_clinical_risk` | AgentMD | "Calculate TIMI risk score for this patient" |
| `biodsa_systematic_review` | TrialMind-SLR | "Systematic review on immunotherapy biomarkers" |
| `biodsa_meta_analysis` | SLR-Meta | "Meta-analysis of statin efficacy in elderly" |

### Example prompt in Claude Code

> Use biodsa_deepevidence_research to investigate the role of KRAS G12C
> mutations in colorectal cancer drug resistance.

## Remote GPU Server

If vLLM runs on a different machine:

```bash
python scripts/start_mcp_server.py \
    --model deepseek-ai/DeepSeek-R1 \
    --llm-host gpu-server --llm-port 8000 \
    --mcp-port 8765
```

Then configure your client to connect to `http://<mcp-server-host>:8765/sse`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BIODSA_LLM_HOST` | `localhost` | vLLM server hostname or IP |
| `BIODSA_LLM_PORT` | `8000` | vLLM server port |
| `BIODSA_LLM_MODEL` | — | Model name |
| `BIODSA_LLM_API_KEY` | `not-needed` | API key (optional for local) |
| `BIODSA_MCP_PORT` | `8765` | MCP server port |

## Architecture

```
┌──────────────┐   SSE/HTTP    ┌──────────────────┐   OpenAI API   ┌─────────┐
│  Claude Code │ ◄───────────► │  BioDSA MCP      │ ◄───────────► │  vLLM   │
│  (or Cursor) │   localhost   │  Server :8765    │   localhost   │  :8000  │
└──────────────┘               └──────────────────┘               └─────────┘
```

- **Agents**: Instantiated per-tool-call, code execution runs locally.
- **Transport**: SSE (HTTP) — allows remote vLLM and multi-client connections.
