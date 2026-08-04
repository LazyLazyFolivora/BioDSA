#!/usr/bin/env python
"""
Test connectivity to a vLLM (OpenAI-compatible) endpoint.

Usage:
    python scripts/test_llm_connection.py --llm-host localhost --llm-port 8000
    python scripts/test_llm_connection.py --llm-host gpu-server --llm-port 8000 --model deepseek-ai/DeepSeek-R1
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Test vLLM endpoint connectivity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--llm-host", default=os.environ.get("BIODSA_LLM_HOST", "localhost"))
    p.add_argument("--llm-port", type=int, default=int(os.environ.get("BIODSA_LLM_PORT", "8000")))
    p.add_argument("--model", "-m", default=None, help="Model name to test (optional, lists available models if omitted)")
    p.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds")
    return p.parse_args()


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def test_models_list(base_url: str, timeout: float) -> dict:
    """GET /v1/models — list available models."""
    req = urllib.request.Request(_url(base_url, "/v1/models"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def test_chat_completion(base_url: str, model: str, timeout: float) -> dict:
    """POST /v1/chat/completions — simple inference test."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "user", "content": "Say 'BioDSA connection OK' and nothing else."}
        ],
        "max_tokens": 32,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        _url(base_url, "/v1/chat/completions"),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    args = parse_args()
    base_url = f"http://{args.llm_host}:{args.llm_port}"

    print(f"Testing vLLM endpoint: {base_url}")
    print(f"Timeout: {args.timeout}s\n")

    # 1. Health check — list models
    print("=" * 60)
    print("1. GET /v1/models")
    print("=" * 60)
    try:
        t0 = time.time()
        models_data = test_models_list(base_url, args.timeout)
        elapsed = time.time() - t0
        models = [m["id"] for m in models_data.get("data", [])]
        print(f"  OK  ({elapsed:.2f}s) — {len(models)} model(s) available:")
        for name in models[:20]:
            print(f"    - {name}")
        if len(models) > 20:
            print(f"    ... and {len(models) - 20} more")
        if not args.model and models:
            args.model = models[0]
            print(f"\n  Using first available model: {args.model}")
    except urllib.error.URLError as e:
        print(f"  FAILED — cannot reach {base_url}: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"  FAILED — {e}")
        sys.exit(1)

    if not args.model:
        print("\n  No model specified and no models returned. Skipping chat test.")
        print("\nAll checks passed (models endpoint only).")
        return

    # 2. Chat completion test
    print(f"\n{'=' * 60}")
    print(f"2. POST /v1/chat/completions  (model={args.model})")
    print("=" * 60)
    try:
        t0 = time.time()
        completion = test_chat_completion(base_url, args.model, args.timeout)
        elapsed = time.time() - t0
        content = completion["choices"][0]["message"]["content"].strip()
        usage = completion.get("usage", {})
        print(f"  OK  ({elapsed:.2f}s)")
        print(f"  Response: {content}")
        print(f"  Tokens:   prompt={usage.get('prompt_tokens', '?')}, "
              f"completion={usage.get('completion_tokens', '?')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  FAILED  HTTP {e.code}")
        print(f"  {body[:500]}")
        sys.exit(1)
    except Exception as e:
        print(f"  FAILED — {e}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print("All checks passed. vLLM endpoint is ready.")
    print(f"Start the MCP server with:")
    print(f"  python scripts/start_mcp_server.py --model {args.model} "
          f"--llm-host {args.llm_host} --llm-port {args.llm_port}")


if __name__ == "__main__":
    main()
