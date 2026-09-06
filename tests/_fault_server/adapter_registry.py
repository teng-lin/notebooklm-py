"""Dependency-light names shared by adapter integration and stress selection."""

SCENARIOS = tuple(
    sorted(
        (
            "adapter_cli_ambiguous_create",
            "adapter_cli_transient_read",
            "adapter_mcp_ambiguous_create",
            "adapter_mcp_transient_read",
            "adapter_rest_ambiguous_create",
            "adapter_rest_transient_read",
            "adapter_rest_download_disconnect",
            "adapter_mcp_download_disconnect",
            "adapter_mcp_chat_start_disconnect",
        )
    )
)
