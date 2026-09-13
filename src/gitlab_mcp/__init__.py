from .client import GitLabClient, client_var
from .config import Settings
from .server import mcp

__all__ = ["GitLabClient", "Settings", "client_var", "main", "mcp"]


def main() -> None:
    """Entry point for the gitlab-mcp MCP server.

    Strict startup ordering — every step fail-fast:

    1. Parse CLI flags with argparse.
    2. Construct the HTTP client and call `check()`: it requires GITLAB_URL
       and GITLAB_TOKEN and probes the backend once through `client.instance`
       (lazy detection, or the explicit GITLAB_BACKEND override, which keeps
       the backend fixed but still probes /metadata best-effort for version).
    3. Run MCPServer over stdio, or streamable HTTP with --http.

    Tools are registered at import of `.server`, not here: a host importing
    this package must see them without calling `main()`.
    """
    import argparse

    from mcp.server.transport_security import TransportSecuritySettings

    from .config import set_allow_public

    parser = argparse.ArgumentParser(
        prog="gitlab-mcp",
        description="MCP server for GitLab and Heptapod. Serves MCP over stdio "
        "unless --http is given.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP at /mcp instead of stdio, for a gateway in front",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address for --http")
    parser.add_argument("--port", type=int, default=8000, help="port for --http")
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="allow creating public repositories and organizations; blocked by default",
    )
    args = parser.parse_args()

    set_allow_public(args.allow_public)

    from .client import get_client

    # Startup gate: a missing, unreachable or rejected credential must fail
    # here, not on the first tool call. `check()` caches the probed instance,
    # so tools reuse its result.
    get_client().check()

    if args.http:
        # Stateless: the gateway in front opens a session per call; nothing outlives a request.
        # It also forwards the public Host header, which the SDK's loopback rebinding guard 421s.
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return

    mcp.run(transport="stdio")
