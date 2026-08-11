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

    llm_timeout: float = 120.0
    """Per-LLM-call timeout in seconds (2 min)."""

    @property
    def endpoint(self) -> str:
        """Full vLLM OpenAI-compatible endpoint URL."""
        return f"http://{self.llm_host}:{self.llm_port}/v1"
