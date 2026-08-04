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

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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
        default=os.environ.get("BIODSA_LLM_API_KEY", "not-needed"),
        help="API key (usually not needed for local vLLM).",
    )
    p.add_argument(
        "--mcp-port", "-p",
        type=int,
        default=int(os.environ.get("BIODSA_MCP_PORT", "8765")),
        help="Port for the MCP SSE HTTP server.  Default: 8765",
    )
    p.add_argument(
        "--sandbox-image",
        default=os.environ.get("BIODSA_SANDBOX_IMAGE", "biodsa-sandbox-py:latest"),
        help="Docker image for code execution sandbox.",
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
        default=120.0,
        help="Timeout in seconds per LLM call.  Default: 120 (2 min).",
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
        mcp_port=args.mcp_port,
        sandbox_image=args.sandbox_image,
        tool_timeout=args.tool_timeout,
        llm_timeout=args.llm_timeout,
    )
    init_config(config)

    logging.info("BioDSA MCP server starting on http://0.0.0.0:%d (SSE)", args.mcp_port)
    logging.info("Model: %s  |  LLM endpoint: %s", args.model, config.endpoint)
    logging.info("Sandbox image: %s", args.sandbox_image)

    mcp.run(transport="sse", port=args.mcp_port)


if __name__ == "__main__":
    main()
