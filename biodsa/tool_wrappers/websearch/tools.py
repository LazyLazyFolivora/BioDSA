"""
Web Search Tool
"""
from typing import Optional, Type
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
import logging
import os
import time

from .agentic import web_search
from .tavily import tavily_search
from biodsa.sandbox.sandbox_interface import ExecutionSandboxWrapper

logger = logging.getLogger(__name__)

__all__ = [
    "WebSearchTool",
    "WebSearchToolInput",
]

# Backend switch.  The web_search knowledge base keeps a single tool; this
# decides which search provider it talks to, so the rest of the pipeline
# (schema, agents, MCP server) is unchanged.
_BACKEND_ENV = "BIODSA_WEBSEARCH_BACKEND"
_BACKEND_ANTHROPIC = "anthropic"
_BACKEND_TAVILY = "tavily"

# =====================================================
# Tool: Web Search (Anthropic or Tavily backend)
# =====================================================
class WebSearchToolInput(BaseModel):
    """Input schema for WebSearchTool."""
    query: str = Field(
        ...,
        description=(
"""
A clear and concise query to inform the target of the web search.

Example:
- "What are the latest treatments for diabetes?"
- "CRISPR gene editing applications 2024"
- "How does mRNA vaccine work?"
"""
        )
    )


class WebSearchTool(BaseTool):
    """
    Tool to perform web search.

    The backend is selected via the ``BIODSA_WEBSEARCH_BACKEND`` environment
    variable (``anthropic`` or ``tavily``); it defaults to ``anthropic``.

    - ``anthropic`` uses Claude's built-in web search and requires
      ``ANTHROPIC_API_KEY``.
    - ``tavily`` uses the Tavily search API and requires ``TAVILY_API_KEY``.

    The tool returns current information with proper citations (URLs and text
    excerpts) in both cases.
    """
    name: str = "web_search"
    description: str = (
        "Pass a query to a web search sub-agent that will perform a web search to find current information from the internet. "
        "The sub-agent will return an answer to your question with proper citations including URLs and text excerpts. "
    )
    args_schema: Type[BaseModel] = WebSearchToolInput
    api_key: Optional[str] = None
    model_name: str = "claude-haiku-4-5-20251001"
    max_search_uses: int = 3
    backend: str = _BACKEND_ANTHROPIC
    tavily_api_key: Optional[str] = None
    search_depth: str = "basic"
    max_results: int = 5
    include_answer: bool = True
    sandbox: ExecutionSandboxWrapper = None

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "claude-haiku-4-5-20251001",
        max_search_uses: int = 3,
        backend: Optional[str] = None,
        tavily_api_key: Optional[str] = None,
        search_depth: str = "basic",
        max_results: int = 5,
        include_answer: bool = True,
        sandbox: ExecutionSandboxWrapper = None,
    ):
        """
        Initialize the web search tool.

        Args:
            api_key: Anthropic API key (used when backend is "anthropic").
            model_name: Claude model to use for the Anthropic backend.
            max_search_uses: Maximum number of web searches per query (Anthropic).
            backend: "anthropic" or "tavily".  Defaults to BIODSA_WEBSEARCH_BACKEND
                env var, then "anthropic".
            tavily_api_key: Tavily API key (used when backend is "tavily").
            search_depth: "basic" or "advanced" (Tavily).
            max_results: Number of results to return (Tavily, 1-20).
            include_answer: Ask Tavily for an LLM-generated answer (Tavily).
            sandbox: Optional sandbox for code execution.
        """
        super().__init__()
        self.backend = (backend or os.getenv(_BACKEND_ENV, _BACKEND_ANTHROPIC)).lower()
        logger.info("WebSearchTool backend=%s", self.backend)
        self.model_name = model_name
        self.max_search_uses = max_search_uses
        self.search_depth = search_depth
        self.max_results = max_results
        self.include_answer = include_answer
        self.sandbox = sandbox

        if self.backend == _BACKEND_TAVILY:
            self.tavily_api_key = tavily_api_key or os.getenv("TAVILY_API_KEY")
            if not self.tavily_api_key:
                raise ValueError(
                    "Tavily API key is required. Either pass 'tavily_api_key', set "
                    "'TAVILY_API_KEY', or switch BIODSA_WEBSEARCH_BACKEND to 'anthropic'."
                )
        else:
            self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
            if not self.api_key:
                raise ValueError(
                    "Anthropic API key is required. Either pass 'api_key', set "
                    "'ANTHROPIC_API_KEY', or switch BIODSA_WEBSEARCH_BACKEND to 'tavily'."
                )

    def _run(self, query: str) -> str:
        """
        Execute the web search tool.

        Args:
            query: The search query or question

        Returns:
            Formatted string containing:
            - Raw search results (title, url)
            - A synthesized response with inline citations
            - References section with full citation details
        """
        start = time.time()
        try:
            if self.backend == _BACKEND_TAVILY:
                search_results, formatted_response = tavily_search(
                    query=query,
                    api_key=self.tavily_api_key,
                    search_depth=self.search_depth,
                    max_results=self.max_results,
                    include_answer=self.include_answer,
                )
            else:
                search_results, formatted_response = web_search(
                    query=query,
                    model_name=self.model_name,
                    api_key=self.api_key,
                    max_search_uses=self.max_search_uses,
                )

            # Format output
            output_parts = []
            output_parts.append("=" * 80)
            output_parts.append(f"Web Search Results for: '{query}'")
            output_parts.append("=" * 80)

            # Add raw search results if available
            if search_results:
                output_parts.append("\n📊 Raw Search Results:")
                output_parts.append("-" * 80)
                for idx, result in enumerate(search_results, 1):
                    output_parts.append(f"{idx}. {result}")
                output_parts.append("")

            # Add formatted response with citations
            output_parts.append("📝 Answer with Citations:")
            output_parts.append("-" * 80)
            output_parts.append(formatted_response)
            output_parts.append("=" * 80)

            logger.info(
                "WebSearchTool backend=%s query=%r elapsed=%.2fs",
                self.backend, query, time.time() - start,
            )
            return "\n".join(output_parts)

        except Exception as e:
            logger.warning(
                "WebSearchTool backend=%s query=%r failed elapsed=%.2fs: %s",
                self.backend, query, time.time() - start, e,
            )
            return f"Error executing web search: {str(e)}"


if __name__ == "__main__":
    # Example usage
    import os
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()

    # Initialize the tool
    web_search_tool = WebSearchTool(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model_name="claude-haiku-4-5-20251001",
        max_search_uses=3
    )

    # Example query
    query = "What are the latest developments in CRISPR gene editing for cancer treatment in 2024?"

    print("Testing WebSearchTool...")
    print(f"Query: {query}\n")

    # Run the tool
    result = web_search_tool._run(query)
    print(result)
