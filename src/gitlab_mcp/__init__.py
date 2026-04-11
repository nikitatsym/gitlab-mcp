def main():
    """Entry point for the gitlab-mcp MCP server.

    Strict startup ordering — every step fail-fast:

    1. Strip security CLI flags from sys.argv (FastMCP doesn't tolerate them).
    2. Load settings, require GITLAB_URL and GITLAB_TOKEN.
    3. Construct the HTTP client (no requests yet).
    4. Populate `client.instance` via eager backend detection
       (or from the explicit GITLAB_BACKEND override).
    5. Import the server module and register tools
       (this is where the `_heptapod_only` filter runs).
    6. Run FastMCP over stdio.
    """
    import sys

    from .config import set_allow_public

    if "--allow-public" in sys.argv:
        sys.argv.remove("--allow-public")
        set_allow_public(True)

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
        from .backend import InstanceInfo

        vcs_types = (
            {"git", "hg", "hg_git"}
            if settings.gitlab_backend == "heptapod"
            else {"git"}
        )
        client.instance = InstanceInfo(
            backend=settings.gitlab_backend,
            version="unknown",
            enterprise=False,
            vcs_types_supported=vcs_types,
            url=client._base,
        )

    from .server import _register_tools, mcp

    _register_tools()
    mcp.run(transport="stdio")
