"""Tavily-backed web search for BioDSA.

An alternative backend to the Anthropic ``web_search`` in ``agentic.py``.  Both
produce the same ``(search_results, formatted_response)`` shape so that
``WebSearchTool`` can switch between them without changing its output format.

The ``tavily`` SDK is imported lazily (inside ``tavily_search``) so that merely
importing this module — e.g. for ``_parse_tavily_response`` in unit tests — does
not require the package to be installed.
"""

from typing import List, Optional, Tuple


def _parse_tavily_response(response: dict) -> Tuple[List[str], str]:
    """Turn a Tavily ``search()`` result dict into the BioDSA search shape.

    Args:
        response: The dict returned by ``TavilyClient.search(...)``.

    Returns:
        tuple: (search_results, formatted_response)
            - search_results: List of ``"title, url"`` strings.
            - formatted_response: Tavily's answer (when present) followed by a
              numbered references section.
    """
    search_results = []
    results = response.get("results") or []
    for result in results:
        title = result.get("title", "No title")
        url = result.get("url", "No URL")
        search_results.append(f"{title}, {url}")

    parts = []
    answer = (response.get("answer") or "").strip()
    if answer:
        parts.append(answer)

    if results:
        parts.append("\n\nReferences\n")
        for i, result in enumerate(results, 1):
            title = result.get("title", "No title")
            url = result.get("url", "No URL")
            content = result.get("content", "No excerpt available")
            parts.append(f"[{i}] {url}\n    {title}\n    \"{content}\"\n")

    return search_results, "".join(parts).strip()


def tavily_search(
    query: str,
    api_key: Optional[str] = None,
    search_depth: str = "basic",
    max_results: int = 5,
    include_answer: bool = True,
) -> Tuple[List[str], str]:
    """Perform a web search using Tavily.

    Args:
        query: The search query.
        api_key: Tavily API key.  If None, read from ``TAVILY_API_KEY``.
        search_depth: "basic" or "advanced".
        max_results: Number of results to return (1-20).
        include_answer: Ask Tavily for an LLM-generated answer.

    Returns:
        tuple: (search_results, formatted_response)
    """
    from tavily import TavilyClient

    client = TavilyClient(api_key=api_key)
    response = client.search(
        query=query,
        search_depth=search_depth,
        max_results=max_results,
        include_answer=include_answer,
        include_raw_content=False,
    )
    return _parse_tavily_response(response)


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()

    search_results, formatted_response = tavily_search(
        "What are the latest treatments for diabetes?",
        api_key=os.getenv("TAVILY_API_KEY"),
    )
    print("=" * 80)
    print("SEARCH RESULTS:")
    print("=" * 80)
    for i, result in enumerate(search_results, 1):
        print(f"{i}. {result}")

    print("\n" + "=" * 80)
    print("FORMATTED RESPONSE WITH CITATIONS:")
    print("=" * 80)
    print(formatted_response)
