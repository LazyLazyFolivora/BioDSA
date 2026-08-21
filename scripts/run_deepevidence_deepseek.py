#!/usr/bin/env python
"""Run DeepEvidenceAgent with a DeepSeek model.

DeepSeek's API is OpenAI-compatible, so it plugs into the agent through the
"local" API type, which is really "any OpenAI-compatible endpoint". No source
changes are needed: the endpoint just points at DeepSeek instead of a vLLM box.

Usage:
    export DEEPSEEK_API_KEY=sk-...
    python scripts/run_deepevidence_deepseek.py "Summarize approved NSCLC immunotherapies" \
        --knowledge-bases pubmed_papers clinical_trials drug disease

    # reasoning model (note: no tool-calling support — the agent needs tools)
    python scripts/run_deepevidence_deepseek.py "..." --model deepseek-reasoner
"""

import argparse
import os
import sys

REPO_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_BASE_DIR not in sys.path:
    sys.path.insert(0, REPO_BASE_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(REPO_BASE_DIR, ".env"))

DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run DeepEvidenceAgent with a DeepSeek model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("question", help="Research question for the agent.")
    p.add_argument(
        "--model", "-m",
        default=DEFAULT_MODEL,
        help="DeepSeek model id. 'deepseek-chat' (default) supports the tool "
             "calling the agent needs; 'deepseek-reasoner' is reasoning-only.",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("DEEPSEEK_API_KEY"),
        help="DeepSeek API key (defaults to $DEEPSEEK_API_KEY).",
    )
    p.add_argument(
        "--endpoint",
        default=DEEPSEEK_ENDPOINT,
        help="OpenAI-compatible base URL (default: %s)." % DEEPSEEK_ENDPOINT,
    )
    p.add_argument(
        "--knowledge-bases",
        nargs="*",
        default=None,
        help="Knowledge bases to search (e.g. pubmed_papers clinical_trials drug "
             "disease). Omit to use all available.",
    )
    p.add_argument(
        "--output-dir",
        default="test_artifacts",
        help="Directory for the PDF report (default: test_artifacts).",
    )
    p.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip writing the PDF report.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.api_key:
        sys.exit("Missing DeepSeek API key: set DEEPSEEK_API_KEY or pass --api-key")

    from biodsa.agents import DeepEvidenceAgent

    agent = DeepEvidenceAgent(
        model_name=args.model,
        api_type="local",            # OpenAI-compatible endpoint
        api_key=args.api_key,
        endpoint=args.endpoint,
        subagent_action_rounds_budget=5,
        main_search_rounds_budget=2,
        main_action_rounds_budget=15,
        light_mode=False,
        llm_timeout=1200,
    )

    go_kwargs = {"input_query": args.question}
    if args.knowledge_bases:
        go_kwargs["knowledge_bases"] = args.knowledge_bases

    results = agent.go(**go_kwargs)
    print(results.to_json())
    if not args.no_pdf:
        results.to_pdf(output_dir=args.output_dir)
    agent.clear_workspace()
    print("Done!")


if __name__ == "__main__":
    main()
