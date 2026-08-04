from biodsa.mcp.config import MCPServerConfig

__all__ = ["MCPServerConfig", "mcp", "init_config"]


def __getattr__(name: str):
    """Lazy-import server symbols so that ``import biodsa.mcp``
    does not fail when ``fastmcp`` is not yet installed."""
    if name in ("mcp", "init_config"):
        from biodsa.mcp.server import mcp as _mcp, init_config as _init

        globals()["mcp"] = _mcp
        globals()["init_config"] = _init
        return _mcp if name == "mcp" else _init
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
