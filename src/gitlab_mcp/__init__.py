def main() -> None:
    """Entry point for the gitlab-mcp MCP server.

    Strict startup ordering — every step fail-fast:

    1. Parse CLI flags with argparse.
    2. Load settings, require GITLAB_URL and GITLAB_TOKEN.
    3. Construct the HTTP client (no requests yet).
    4. Populate `client.instance` via eager backend detection
       (or from the explicit GITLAB_BACKEND override, which keeps the
       backend fixed but still probes /metadata best-effort for version).
    5. Import the server module and register tools
       (this is where the `_heptapod_only` filter runs).
    6. Run MCPServer over stdio, or streamable HTTP with --http.
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

    from .config import get_settings

    settings = get_settings()
    if not settings.gitlab_url or not settings.gitlab_token:
        raise ValueError(
            "GITLAB_URL and GITLAB_TOKEN must be set. See README."
        )

    from .client import get_client

    client = get_client()

    if settings.gitlab_backend == "auto":
        from .backend import detect_instance

        client.instance = detect_instance(client)
    else:
        from .backend import InstanceInfo, probe_metadata

        vcs_types = (
            {"git", "hg", "hg_git"}
            if settings.gitlab_backend == "heptapod"
            else {"git"}
        )
        version, revision, enterprise = probe_metadata(client)
        client.instance = InstanceInfo(
            backend=settings.gitlab_backend,
            version=version,
            revision=revision,
            enterprise=enterprise,
            vcs_types_supported=vcs_types,
            url=client._base,
        )

    from .server import _register_tools, mcp

    _register_tools()

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
