from dataclasses import dataclass


@dataclass
class MCPServerConfig:
    """Configuration for the BioDSA MCP server."""

    llm_host: str = "localhost"
    """vLLM server hostname or IP."""

    llm_port: int = 8000
    """vLLM server port."""

    model_name: str = ""
    """Model name as registered in vLLM (e.g. 'meta-llama/Llama-3.1-8B-Instruct')."""

    api_key: str = "not-needed"
    """API key (not needed for local vLLM, but required by LangChain's ChatOpenAI)."""

    mcp_port: int = 8765
    """Port for the MCP SSE HTTP server."""

    tool_timeout: float = 600.0
    """Default timeout in seconds for each MCP tool call (10 min)."""

    llm_timeout: float = 1200.0
    """Per-LLM-call timeout in seconds (20 min).

    This is a ceiling, not an expectation: it bounds how long a single call may
    stall, and a healthy call returns in seconds. It has to clear the slowest
    legitimate call, because a local model prefilling a long tool-heavy history
    can take minutes and every timeout here costs a retry. Late rounds are the
    slow ones, so size this against those rather than the average.

    Retries are bounded separately by wall clock (see ``RETRY_BUDGET_FACTOR``), so
    raising this does not multiply into a worst case that outlives the caller.
    """

    @property
    def endpoint(self) -> str:
        """Full vLLM OpenAI-compatible endpoint URL."""
        return f"http://{self.llm_host}:{self.llm_port}/v1"
