"""
BioDSA MCP Server.

Exposes BioDSA biomedical AI agents as MCP tools via SSE transport,
backed by a local vLLM model.  Code execution runs locally.
"""

import logging
import traceback
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from biodsa.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level shared state
# ---------------------------------------------------------------------------
_config: Optional[MCPServerConfig] = None

mcp = FastMCP("BioDSA")


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
def init_config(config: MCPServerConfig) -> None:
    """Store the server configuration (called before ``mcp.run()``)."""
    global _config
    _config = config


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _agent_kwargs() -> dict:
    """Common keyword arguments for instantiating any BioDSA agent."""
    return dict(
        model_name=_config.model_name,
        api_type="local",
        api_key=_config.api_key,
        endpoint=_config.endpoint,
        llm_timeout=_config.llm_timeout,
    )


def _fmt_results(results, max_code_len: int = 800) -> str:
    """Render agent results as Markdown text."""
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


def _run_agent(agent_factory, agent_args: dict, go_kwargs: dict) -> str:
    """Create an agent, run ``go(**go_kwargs)``, return formatted text."""
    agent = None
    try:
        agent = agent_factory(**_agent_kwargs(), **agent_args)
        results = agent.go(**go_kwargs)
        return _fmt_results(results)
    except Exception:
        logger.error("Agent run failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


# ---------------------------------------------------------------------------
# MCP Tools  (one per BioDSA agent)
# ---------------------------------------------------------------------------

@mcp.tool()
def biodsa_dswizard_analyze(task: str, workspace_dir: str = "") -> str:
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
        logger.error("DSWizardAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_deepevidence_research(
    research_question: str,
    knowledge_bases: Optional[List[str]] = None,
) -> str:
    """Deep biomedical research across 17+ knowledge bases (PubMed, ChEMBL, etc.).

    Uses a hierarchical orchestrator + BFS/DFS sub-agents to gather and
    synthesise evidence from multiple biomedical databases.

    Args:
        research_question: The research question. E.g.
            "What are the mechanisms of EGFR inhibitor resistance in NSCLC?"
        knowledge_bases: Optional list of knowledge bases to search.
            Options: pubmed, chembl, uniprot, opentargets, ensembl,
            cbioportal, reactome, etc. If omitted, all available are used.
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
        logger.error("DeepEvidenceAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_trialgpt_match(patient_note: str) -> str:
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
        logger.error("TrialGPTAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_gene_analysis(gene_set: str) -> str:
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
        logger.error("GeneAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_clinical_risk(patient_note: str, query: str = "") -> str:
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
        logger.error("AgentMD failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_systematic_review(
    research_question: str,
    target_outcomes: Optional[List[str]] = None,
) -> str:
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
        logger.error("TrialMindSLRAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None


@mcp.tool()
def biodsa_meta_analysis(
    research_question: str,
    target_outcomes: Optional[List[str]] = None,
) -> str:
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
        logger.error("SLRMetaAgent failed: %s", traceback.format_exc())
        return f"Error: {traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.sandbox = None
