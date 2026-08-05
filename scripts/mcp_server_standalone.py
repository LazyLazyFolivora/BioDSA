#!/usr/bin/env python
"""
Standalone MCP server for BioDSA using the official ``mcp`` SDK (SSE transport).

This bypasses FastMCP's Host-header validation issues and is protocol-compatible
with the ``mcp-go`` library used by WeKnora.

Usage:
    python scripts/mcp_server_standalone.py \\
        --model Qwen3.6-FP8 --llm-host 172.20.72.25 --llm-port 60000
"""

import argparse
import logging
import os
import sys
import traceback
from typing import Optional, List

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from biodsa.mcp.config import MCPServerConfig

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_config: Optional[MCPServerConfig] = None


def _agent_kwargs() -> dict:
    return dict(
        model_name=_config.model_name,
        api_type="local",
        api_key=_config.api_key,
        endpoint=_config.endpoint,
        llm_timeout=_config.llm_timeout,
    )


def _fmt_results(results, max_code_len: int = 800) -> str:
    parts = [results.final_response or "(no final response)"]
    if results.code_execution_results:
        parts.append(f"\n\n---\n### Code Executions ({len(results.code_execution_results)})")
        for i, cr in enumerate(results.code_execution_results, 1):
            code = cr.get("code", "")
            output = cr.get("console_output", "") or cr.get("output", "")
            if code:
                snippet = code if len(code) <= max_code_len else code[:max_code_len] + "\n# ... (truncated)"
                parts.append(f"\n**Execution #{i}:**\n```python\n{snippet}\n```")
            if output:
                out_snippet = output if len(output) <= 1000 else output[:1000] + "\n... (truncated)"
                parts.append(f"\n**Output:**\n```\n{out_snippet}\n```")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def tool_deepevidence_research(research_question: str, knowledge_bases: Optional[List[str]] = None) -> str:
    """Deep biomedical research across 17+ knowledge bases (PubMed, ChEMBL, etc.).

    Uses a hierarchical orchestrator + BFS/DFS sub-agents to gather and
    synthesise evidence from multiple biomedical databases.

    Args:
        research_question: The research question. E.g.
            "What are the mechanisms of EGFR inhibitor resistance in NSCLC?"
        knowledge_bases: Optional list of knowledge bases to search.
            Options: pubmed, chembl, uniprot, opentargets, ensembl, etc.
    """
    from biodsa.agents.deepevidence.agent import DeepEvidenceAgent

    agent = None
    try:
        kwargs = _agent_kwargs()
        kwargs.setdefault("small_model_name", _config.model_name)
        kwargs.setdefault("small_model_api_type", "local")
        kwargs.setdefault("small_model_api_key", _config.api_key)
        kwargs.setdefault("small_model_endpoint", _config.endpoint)
        agent = DeepEvidenceAgent(**kwargs)
        go_kwargs = {"input_query": research_question}
        if knowledge_bases:
            go_kwargs["knowledge_bases"] = knowledge_bases
        results = agent.go(**go_kwargs)
        return _fmt_results(results)
    except Exception:
        logging.error("DeepEvidenceAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_systematic_review(research_question: str, target_outcomes: Optional[List[str]] = None) -> str:
    """Systematic literature review via TrialMind-SLR (4-stage workflow).

    Stages: literature search → screening → data extraction → evidence synthesis.

    Args:
        research_question: The systematic review question (PICO format preferred).
        target_outcomes: Optional list of outcomes to focus extraction on.
    """
    from biodsa.agents.trialmind_slr.agent import TrialMindSLRAgent

    agent = None
    try:
        agent = TrialMindSLRAgent(**_agent_kwargs())
        go_kwargs = {"research_question": research_question}
        if target_outcomes:
            go_kwargs["target_outcomes"] = target_outcomes
        results = agent.go(**go_kwargs)
        return _fmt_results(results)
    except Exception:
        logging.error("TrialMindSLRAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_gene_analysis(gene_set: str) -> str:
    """Gene set analysis with self-verification (GeneAgent).

    Performs cascade verification: functional enrichment, literature-backed
    claim generation, and database-backed claim verification.

    Args:
        gene_set: Comma-separated gene symbols. E.g. "ERBB2,EGFR,KRAS,TP53".
    """
    from biodsa.agents.geneagent.agent import GeneAgent

    agent = None
    try:
        agent = GeneAgent(**_agent_kwargs())
        results = agent.go(gene_set=gene_set)
        return _fmt_results(results)
    except Exception:
        logging.error("GeneAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_trialgpt_match(patient_note: str) -> str:
    """Match a patient to clinical trials using TrialGPT.

    Two-stage workflow:
    1. Extract key medical info from the patient note; search ClinicalTrials.gov.
    2. Rank candidate trials by eligibility with detailed rationale.

    Args:
        patient_note: Free-text clinical note describing the patient's condition,
            demographics, biomarkers, treatment history, etc.
    """
    from biodsa.agents.trialgpt.agent import TrialGPTAgent

    agent = None
    try:
        agent = TrialGPTAgent(**_agent_kwargs())
        results = agent.go(patient_note=patient_note)
        return _fmt_results(results)
    except Exception:
        logging.error("TrialGPTAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_clinical_risk(patient_note: str, query: str = "") -> str:
    """Clinical risk prediction using AgentMD's 2,164+ clinical calculators.

    Two-step workflow: select appropriate calculators, then compute risk scores.

    Args:
        patient_note: Clinical note with patient demographics, vitals, labs, history.
        query: Optional specific clinical question (e.g. "Calculate TIMI risk score").
    """
    from biodsa.agents.agentmd.agent import AgentMD

    agent = None
    try:
        kwargs = _agent_kwargs()
        agent = AgentMD(**kwargs)
        go_kwargs = {"patient_note": patient_note}
        if query:
            go_kwargs["query"] = query
        results = agent.go(**go_kwargs)
        return _fmt_results(results)
    except Exception:
        logging.error("AgentMD failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_dswizard_analyze(task: str, workspace_dir: str = "") -> str:
    """Biomedical data analysis with DSWizard (planning → implementation).

    Use for: statistical analysis, survival analysis, differential expression,
    data visualization on biomedical datasets (CSV/TSV).

    Args:
        task: Analysis description. E.g. "Perform survival analysis for TP53
              mutant vs wild-type patients in the BRCA dataset."
        workspace_dir: Local directory with CSV/TSV files to analyse.
    """
    from biodsa.agents.dswizard.agent import DSWizardAgent

    agent = None
    try:
        kwargs = _agent_kwargs()
        agent = DSWizardAgent(**kwargs)
        if workspace_dir:
            agent.register_workspace(workspace_dir)
        results = agent.go(task)
        return _fmt_results(results)
    except Exception:
        logging.error("DSWizardAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


async def tool_meta_analysis(research_question: str, target_outcomes: Optional[List[str]] = None) -> str:
    """Systematic review and meta-analysis via SLR-Meta agent.

    Searches PubMed and ClinicalTrials.gov, screens studies, extracts data,
    and performs meta-analysis where appropriate.

    Args:
        research_question: The research question for the meta-analysis.
        target_outcomes: Optional list of outcomes for data extraction.
    """
    from biodsa.agents.slr_meta.agent import SLRMetaAgent

    agent = None
    try:
        kwargs = _agent_kwargs()
        agent = SLRMetaAgent(**kwargs)
        go_kwargs = {"research_question": research_question}
        if target_outcomes:
            go_kwargs["target_outcomes"] = target_outcomes
        results = agent.go(**go_kwargs)
        return _fmt_results(results)
    except Exception:
        logging.error("SLRMetaAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BioDSA MCP Server (official SDK, SSE)")
    p.add_argument("--model", "-m", required=True, help="Model name in vLLM.")
    p.add_argument("--llm-host", default=os.environ.get("BIODSA_LLM_HOST", "localhost"))
    p.add_argument("--llm-port", type=int, default=int(os.environ.get("BIODSA_LLM_PORT", "8000")))
    p.add_argument("--api-key", default=os.environ.get("BIODSA_LLM_API_KEY", "not-needed"))
    p.add_argument("--mcp-port", "-p", type=int, default=int(os.environ.get("BIODSA_MCP_PORT", "8765")))
    p.add_argument("--mcp-host", default="0.0.0.0")
    p.add_argument("--llm-timeout", type=float, default=120.0)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    global _config
    _config = MCPServerConfig(
        llm_host=args.llm_host,
        llm_port=args.llm_port,
        model_name=args.model,
        api_key=args.api_key,
        mcp_port=args.mcp_port,
        llm_timeout=args.llm_timeout,
    )

    from mcp.server.lowlevel import Server
    from mcp.server.sse import SseServerTransport
    from mcp.types import Tool, TextContent
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import Response

    # Build MCP server with tools
    app = Server("BioDSA")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="biodsa_deepevidence_research",
                description="Deep biomedical research across 17+ knowledge bases (PubMed, ChEMBL, KEGG, etc.). "
                "Uses a hierarchical orchestrator + BFS/DFS sub-agents to gather and synthesise evidence. "
                "Use for: drug repurposing, target identification, mechanism reasoning, evidence synthesis.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "research_question": {
                            "type": "string",
                            "description": "The research question. E.g. 'What are the mechanisms of EGFR inhibitor resistance in NSCLC?'",
                        },
                        "knowledge_bases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of knowledge bases to search.",
                        },
                    },
                    "required": ["research_question"],
                },
            ),
            Tool(
                name="biodsa_systematic_review",
                description="Systematic literature review (4-stage: search → screen → extract → synthesise).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "research_question": {"type": "string", "description": "Research question in PICO format."},
                        "target_outcomes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional outcomes to focus on.",
                        },
                    },
                    "required": ["research_question"],
                },
            ),
            Tool(
                name="biodsa_gene_analysis",
                description="Gene set analysis with self-verification (enrichment + literature + database verification).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "gene_set": {
                            "type": "string",
                            "description": "Comma-separated gene symbols. E.g. 'ERBB2,EGFR,KRAS,TP53'.",
                        },
                    },
                    "required": ["gene_set"],
                },
            ),
            Tool(
                name="biodsa_trialgpt_match",
                description="Match a patient to clinical trials using TrialGPT. "
                "Two-stage: extract medical info from note → rank candidate trials by eligibility with rationale.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "patient_note": {
                            "type": "string",
                            "description": "Free-text clinical note with condition, demographics, biomarkers, treatment history.",
                        },
                    },
                    "required": ["patient_note"],
                },
            ),
            Tool(
                name="biodsa_clinical_risk",
                description="Clinical risk prediction using AgentMD's 2,164+ clinical calculators. "
                "Selects appropriate calculators then computes risk scores.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "patient_note": {
                            "type": "string",
                            "description": "Clinical note with patient vitals, labs, history, medications.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional specific question. E.g. 'Calculate TIMI risk score'.",
                        },
                    },
                    "required": ["patient_note"],
                },
            ),
            Tool(
                name="biodsa_dswizard_analyze",
                description="Biomedical data analysis with DSWizard (planning → implementation). "
                "Use for: statistical analysis, survival analysis, differential expression, data visualization.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Analysis description. E.g. 'Perform survival analysis for TP53 mutant vs wild-type in BRCA dataset.'",
                        },
                        "workspace_dir": {
                            "type": "string",
                            "description": "Optional local directory path with CSV/TSV files to analyse.",
                        },
                    },
                    "required": ["task"],
                },
            ),
            Tool(
                name="biodsa_meta_analysis",
                description="Systematic review and meta-analysis via SLR-Meta agent. "
                "Searches PubMed/ClinicalTrials.gov, screens studies, extracts data, performs meta-analysis.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "research_question": {
                            "type": "string",
                            "description": "Research question for meta-analysis.",
                        },
                        "target_outcomes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional outcomes for data extraction.",
                        },
                    },
                    "required": ["research_question"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        handlers = {
            "biodsa_deepevidence_research": tool_deepevidence_research,
            "biodsa_systematic_review": tool_systematic_review,
            "biodsa_gene_analysis": tool_gene_analysis,
            "biodsa_trialgpt_match": tool_trialgpt_match,
            "biodsa_clinical_risk": tool_clinical_risk,
            "biodsa_dswizard_analyze": tool_dswizard_analyze,
            "biodsa_meta_analysis": tool_meta_analysis,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")

        result = await handler(**arguments)
        return [TextContent(type="text", text=result)]

    # SSE transport + Starlette
    sse = SseServerTransport("/messages")

    async def handle_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    async def health(request):
        return Response("OK")

    starlette = Starlette(
        routes=[
            Route("/health", endpoint=health),
            Mount("/messages", app=sse.handle_post_message),
        ]
    )

    from starlette.types import ASGIApp, Scope, Receive, Send

    class SSEMiddleware:
        def __init__(self, app: ASGIApp, sse_path: str = "/sse"):
            self.app = app
            self.sse_path = sse_path

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["path"] == self.sse_path:
                await handle_sse(scope, receive, send)
            else:
                await self.app(scope, receive, send)

    starlette.add_middleware(SSEMiddleware, sse_path="/sse")

    logging.info("BioDSA MCP server starting on http://%s:%d (SSE)", args.mcp_host, args.mcp_port)
    logging.info("Model: %s  |  LLM endpoint: %s", args.model, _config.endpoint)
    logging.info("Tools: deepevidence_research, systematic_review, gene_analysis, trialgpt_match, clinical_risk, dswizard_analyze, meta_analysis")

    uvicorn.run(starlette, host=args.mcp_host, port=args.mcp_port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
