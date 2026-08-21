"""Tests for the Tavily web-search backend (offline — no API calls)."""

import pytest

from biodsa.tool_wrappers.websearch.tavily import _parse_tavily_response
from biodsa.tool_wrappers.websearch.tools import WebSearchTool


def test_parse_tavily_response_builds_results_and_references():
    response = {
        "query": "diabetes treatments",
        "answer": "Metformin is a first-line treatment for type 2 diabetes.",
        "results": [
            {
                "title": "Diabetes Treatment Overview",
                "url": "https://example.com/diabetes",
                "content": "Metformin and lifestyle changes are recommended.",
                "score": 0.95,
            },
            {
                "title": "Novel Therapies",
                "url": "https://example.com/novel",
                "content": "GLP-1 receptor agonists show promise.",
                "score": 0.88,
            },
        ],
    }

    search_results, formatted = _parse_tavily_response(response)

    assert search_results == [
        "Diabetes Treatment Overview, https://example.com/diabetes",
        "Novel Therapies, https://example.com/novel",
    ]
    assert "Metformin" in formatted
    assert "[1] https://example.com/diabetes" in formatted
    assert "[2] https://example.com/novel" in formatted


def test_parse_tavily_response_handles_empty_results_and_answer():
    response = {"query": "q", "results": []}

    search_results, formatted = _parse_tavily_response(response)

    assert search_results == []
    assert formatted == ""


def test_parse_tavily_response_handles_missing_fields():
    response = {"results": [{}]}

    search_results, formatted = _parse_tavily_response(response)

    assert search_results == ["No title, No URL"]
    assert "[1] No URL" in formatted


def test_websearch_tool_defaults_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("BIODSA_WEBSEARCH_BACKEND", raising=False)

    tool = WebSearchTool()

    assert tool.backend == "anthropic"


def test_websearch_tool_reads_backend_from_env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("BIODSA_WEBSEARCH_BACKEND", "tavily")

    tool = WebSearchTool()

    assert tool.backend == "tavily"


def test_websearch_tool_raises_when_tavily_key_missing(monkeypatch):
    monkeypatch.setenv("BIODSA_WEBSEARCH_BACKEND", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(ValueError):
        WebSearchTool()
