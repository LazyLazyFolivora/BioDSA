#!/usr/bin/env python
"""
Start the BioDSA MCP server with SSE transport.

Requires a running vLLM (or other OpenAI-compatible) server.
The server exposes BioDSA agents as MCP tools callable by
Claude Code, Cursor, or any MCP-compatible client.

Usage:
    python scripts/start_mcp_server.py \\
        --model meta-llama/Llama-3.1-8B-Instruct \\
        --llm-host localhost --llm-port 8000

    python scripts/start_mcp_server.py \\
        --model deepseek-ai/DeepSeek-R1 \\
        --llm-host gpu-server --llm-port 8000 \\
        --mcp-port 9876
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Read .env before parse_args() evaluates its os.environ defaults, so the API
# key and endpoint override can live in a file instead of the shell environment.
load_dotenv(os.path.join(_project_root, ".env"))

from biodsa.mcp import MCPServerConfig, mcp, init_config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BioDSA MCP Server (SSE transport)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--model", "-m",
        required=True,
        help="Model name as registered in vLLM (e.g. 'meta-llama/Llama-3.1-8B-Instruct').",
    )
    p.add_argument(
        "--llm-host",
        default=os.environ.get("BIODSA_LLM_HOST", "localhost"),
        help="vLLM server hostname or IP.  Default: localhost",
    )
    p.add_argument(
        "--llm-port",
        type=int,
        default=int(os.environ.get("BIODSA_LLM_PORT", "8000")),
        help="vLLM server port.  Default: 8000",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("BIODSA_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or "not-needed",
        help="API key (not needed for local vLLM). Falls back to DEEPSEEK_API_KEY.",
    )
    p.add_argument(
        "--api-base",
        default=os.environ.get("BIODSA_LLM_API_BASE"),
        help="OpenAI-compatible base URL override (e.g. 'https://api.deepseek.com' "
             "for DeepSeek). Omit to use http://<llm-host>:<llm-port>/v1.",
    )
    p.add_argument(
        "--mcp-port", "-p",
        type=int,
        default=int(os.environ.get("BIODSA_MCP_PORT", "8765")),
        help="Port for the MCP SSE HTTP server.  Default: 8765",
    )
    p.add_argument(
        "--tool-timeout",
        type=float,
        default=600.0,
        help="Timeout in seconds per tool call.  Default: 600 (10 min).",
    )
    p.add_argument(
        "--llm-timeout",
        type=float,
        default=1200.0,
        help="Ceiling in seconds for a single LLM call, enforced by the HTTP client. "
             "Must clear the slowest legitimate call: a local model prefilling a long "
             "history can take minutes, and each timeout costs a retry. Retries are "
             "bounded by wall clock, so raising this does not multiply the worst "
             "case.  Default: 1200.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = MCPServerConfig(
        llm_host=args.llm_host,
        llm_port=args.llm_port,
        model_name=args.model,
        api_key=args.api_key,
        api_base=args.api_base,
        mcp_port=args.mcp_port,
        tool_timeout=args.tool_timeout,
        llm_timeout=args.llm_timeout,
    )
    init_config(config)

    logging.info("BioDSA MCP server starting on http://0.0.0.0:%d (SSE)", args.mcp_port)
    logging.info("Model: %s  |  LLM endpoint: %s", args.model, config.endpoint)

    os.environ["MCP_PORT"] = str(args.mcp_port)
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
