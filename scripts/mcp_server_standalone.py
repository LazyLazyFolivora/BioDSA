#!/usr/bin/env python
"""
Standalone MCP server for BioDSA using the official ``mcp`` SDK.

Supports both SSE and Streamable HTTP transports. Streamable HTTP is recommended
for long-running agent tasks to avoid the 60s SSE timeout in mcp-go clients.

Usage:
    python scripts/mcp_server_standalone.py \\
        --model Qwen3.6-FP8 --llm-host 172.20.72.25 --llm-port 60000

    python scripts/mcp_server_standalone.py \\
        --model Qwen3.6-FP8 --llm-host 172.20.72.25 --llm-port 60000 \\
        --transport streamable-http
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback
from typing import Optional, List

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from biodsa.mcp.config import MCPServerConfig
from biodsa.narrative.broadcaster import EventBroadcaster

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_config: Optional[MCPServerConfig] = None
_broadcaster: EventBroadcaster = EventBroadcaster()

# How long to wait for the notification consumer to flush its queue once a run
# has finished before giving up on it.
CONSUMER_DRAIN_TIMEOUT = 30.0


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

async def tool_deepevidence_research(research_question: str, knowledge_bases: Optional[List[str]] = None, session_id: Optional[str] = None) -> str:
    """Deep biomedical research across 10 knowledge bases.

    Uses a hierarchical orchestrator + BFS/DFS sub-agents to gather and
    synthesise evidence from multiple biomedical databases.

    Args:
        research_question: The research question. E.g.
            "What are the mechanisms of EGFR inhibitor resistance in NSCLC?"
        knowledge_bases: Optional list of knowledge bases to search.
            Valid values: pubmed_papers, gene, disease, drug, variant,
            clinical_trials, web_search, target, pathway, compound.
        session_id: Optional client-generated session ID for narrative
            graph event streaming.
    """
    from biodsa.agents.deepevidence.agent import DeepEvidenceAgent
    from biodsa.narrative.events import RunComplete
    from biodsa.narrative.graph_log import log_run_summary
    from biodsa.memory.memory_graph import load_graph_data
    from mcp.server.lowlevel.server import request_ctx
    from mcp.types import Notification

    agent = None
    consumer_task = None
    streaming = False
    t_start = time.time()
    try:
        kwargs = _agent_kwargs()
        kwargs.setdefault("small_model_name", _config.model_name)
        kwargs.setdefault("small_model_api_type", "local")
        kwargs.setdefault("small_model_api_key", _config.api_key)
        kwargs.setdefault("small_model_endpoint", _config.endpoint)
        if session_id:
            # Resolve the request context *before* creating the run: without a
            # consumer there is nobody draining the queue, and the agent would
            # pile up a whole run's events (including the RunComplete snapshot)
            # in memory for nothing.
            try:
                ctx = request_ctx.get()
            except LookupError:
                ctx = None
                logging.warning("DeepEvidence: no MCP request context, graph events will not be streamed")
            if ctx is not None and _broadcaster.create_run(session_id):
                kwargs["broadcaster"] = _broadcaster
                kwargs["session_id"] = session_id
                streaming = True
                mcp_session = ctx.session
                _sid = session_id
                # Without related_request_id the SDK routes notifications to the
                # standalone GET SSE stream, which streamable-http clients such
                # as WeKnora never open -- the events would be silently dropped.
                _req_id = ctx.request_id

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
                logging.info("DeepEvidence: MCP notification consumer started for session=%s", session_id)
        logging.info("DeepEvidence: creating agent for query=%s session=%s", research_question[:80], session_id)
        agent = DeepEvidenceAgent(**kwargs)
        go_kwargs = {"input_query": research_question}
        if knowledge_bases:
            go_kwargs["knowledge_bases"] = knowledge_bases
        logging.info("DeepEvidence: agent.go() starting (kbs=%s, session=%s)",
                     go_kwargs.get("knowledge_bases", "all"), session_id)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: agent.go(**go_kwargs))
        elapsed = time.time() - t_start
        logging.info("DeepEvidence: agent.go() done in %.0fs", elapsed)
        try:
            graph_data = results.evidence_graph_data if results else {}
            entities = graph_data.get("entities", []) if isinstance(graph_data, dict) else []
            relations = graph_data.get("relations", []) if isinstance(graph_data, dict) else []
        except Exception:
            entities, relations = [], []
            logging.warning("Could not read evidence graph data", exc_info=True)
        # Recorded regardless of streaming: this is the only place the finished
        # graph is persisted anywhere the operator can read it.
        log_run_summary(session_id, entities, relations, elapsed, research_question)
        # Push final graph snapshot
        if streaming:
            try:
                total_steps = len(results.message_history) if results and results.message_history else 0
                _broadcaster.emit_sync(session_id, RunComplete(
                    entities=entities,
                    relations=relations,
                    total_steps=total_steps,
                    duration_seconds=elapsed,
                    final_response_preview=(results.final_response or "")[:200] if results else "",
                ))
            except Exception:
                logging.warning("Failed to push RunComplete event", exc_info=True)
        return _fmt_results(results)
    except Exception:
        elapsed = time.time() - t_start
        logging.error("DeepEvidenceAgent failed after %.0fs: %s", elapsed, traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None
        if streaming:
            try:
                _broadcaster.close_run(session_id)
            except Exception:
                logging.warning("Failed to close broadcaster run", exc_info=True)
            if consumer_task is not None:
                try:
                    # Bounded on purpose: send_notification can stall on a slow
                    # or half-open client, and an unbounded await here would
                    # wedge the tool call forever.
                    await asyncio.wait_for(consumer_task, timeout=CONSUMER_DRAIN_TIMEOUT)
                except asyncio.TimeoutError:
                    consumer_task.cancel()
                    try:
                        await consumer_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    logging.warning(
                        "Graph event consumer did not drain within %.0fs, cancelled: session=%s",
                        CONSUMER_DRAIN_TIMEOUT, session_id,
                    )
                except Exception:
                    logging.warning("Graph event consumer task failed", exc_info=True)
        # The per-session graph is kept: it is the only durable copy of the nodes
        # and relations, the client cannot ask for it again, and deleting it makes
        # an empty graph indistinguishable from a failed write. go() clears the
        # directory at the start of each run, so a session does not accumulate.
        try:
            if agent is not None and agent.owns_evidence_graph_cache_dir:
                logging.info("DeepEvidence: session graph at %s",
                             agent.evidence_graph_cache_dir)
        except Exception:
            logging.warning("Failed to report session graph dir", exc_info=True)


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
    p = argparse.ArgumentParser(description="BioDSA MCP Server (official SDK)")
    p.add_argument("--model", "-m", required=True, help="Model name in vLLM.")
    p.add_argument("--llm-host", default=os.environ.get("BIODSA_LLM_HOST", "localhost"))
    p.add_argument("--llm-port", type=int, default=int(os.environ.get("BIODSA_LLM_PORT", "8000")))
    p.add_argument("--api-key", default=os.environ.get("BIODSA_LLM_API_KEY", "not-needed"))
    p.add_argument("--mcp-port", "-p", type=int, default=int(os.environ.get("BIODSA_MCP_PORT", "8765")))
    p.add_argument("--mcp-host", default="0.0.0.0")
    p.add_argument("--llm-timeout", type=float, default=120.0)
    p.add_argument("--transport", default="sse", choices=["sse", "streamable-http"],
                   help="MCP transport protocol (default: sse). "
                        "streamable-http avoids the 60s SSE timeout in mcp-go clients.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--graph-log-dir", default=os.environ.get("BIODSA_GRAPH_LOG_DIR"),
                   help="Directory for the daily-rotating knowledge-graph event log "
                        "(default: <REPO_BASE_DIR or ~>/.biodsa_memory/graph_events).")
    return p.parse_args()


def _build_mcp_app():
    """Build the MCP Server with all tool registrations."""
    from mcp.server.lowlevel import Server
    from mcp.types import Tool, TextContent

    app = Server("BioDSA")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="biodsa_deepevidence_research",
                description="Deep biomedical research across 10 knowledge bases. "
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
                            "description": "Optional list of knowledge bases. "
                            "Valid values: pubmed_papers, gene, disease, drug, variant, "
                            "clinical_trials, web_search, target, pathway, compound.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Optional client-generated session ID for real-time graph event streaming.",
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
        t_start = time.time()
        logging.info("MCP tool call: %s args=%s", name,
                     {k: str(v)[:80] for k, v in arguments.items()})
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
        elapsed = time.time() - t_start
        result_preview = result[:150].replace('\n', ' ') if result else "(empty)"
        logging.info("MCP tool done: %s in %.0fs result=%s", name, elapsed, result_preview)
        return [TextContent(type="text", text=result)]

    return app


def _serve_sse(app, args):
    """Serve MCP via SSE transport."""
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import Response
    from starlette.types import ASGIApp, Scope, Receive, Send

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


def _serve_streamable_http(app, args):
    """Serve MCP via Streamable HTTP transport (avoids 60s SSE timeout)."""
    import contextlib
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import Response
    from starlette.types import ASGIApp, Scope, Receive, Send

    session_manager = StreamableHTTPSessionManager(app)

    async def health(request):
        return Response("OK")

    @contextlib.asynccontextmanager
    async def lifespan(starlette_app):
        async with session_manager.run():
            yield

    starlette = Starlette(
        routes=[Route("/health", endpoint=health)],
        lifespan=lifespan,
    )

    class StreamableHTTPMiddleware:
        def __init__(self, app: ASGIApp, mgr: StreamableHTTPSessionManager, mcp_path: str = "/mcp"):
            self.app = app
            self.mgr = mgr
            self.mcp_path = mcp_path

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["path"] == self.mcp_path:
                await self.mgr.handle_request(scope, receive, send)
            else:
                await self.app(scope, receive, send)

    starlette.add_middleware(StreamableHTTPMiddleware, mgr=session_manager, mcp_path="/mcp")

    logging.info("BioDSA MCP server starting on http://%s:%d (Streamable HTTP)", args.mcp_host, args.mcp_port)
    logging.info("Model: %s  |  LLM endpoint: %s", args.model, _config.endpoint)
    logging.info("Endpoint: POST http://%s:%d/mcp", args.mcp_host, args.mcp_port)
    logging.info("Tools: deepevidence_research, systematic_review, gene_analysis, trialgpt_match, clinical_risk, dswizard_analyze, meta_analysis")

    uvicorn.run(starlette, host=args.mcp_host, port=args.mcp_port, log_level=args.log_level.lower())


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from biodsa.narrative.graph_log import configure_graph_log
    graph_log_path = configure_graph_log(args.graph_log_dir)
    if graph_log_path is not None:
        logging.info("Knowledge-graph event log: %s", graph_log_path)

    global _config
    _config = MCPServerConfig(
        llm_host=args.llm_host,
        llm_port=args.llm_port,
        model_name=args.model,
        api_key=args.api_key,
        mcp_port=args.mcp_port,
        llm_timeout=args.llm_timeout,
    )

    app = _build_mcp_app()

    if args.transport == "streamable-http":
        _serve_streamable_http(app, args)
    else:
        _serve_sse(app, args)


if __name__ == "__main__":
    main()
