"""Core infrastructure for NotebookLM API client."""

import logging
import os
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .auth import AuthTokens
from .rpc import (
    RPCMethod,
    RPCError,
    BATCHEXECUTE_URL,
    encode_rpc_request,
    build_request_body,
    decode_response,
)

# Enable RPC debug output via environment variable
DEBUG_RPC = os.environ.get("NOTEBOOKLM_DEBUG_RPC", "").lower() in ("1", "true", "yes")

# Configure logging for RPC debug mode
if DEBUG_RPC:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s: %(message)s",
    )

# Maximum number of conversations to cache (FIFO eviction)
MAX_CONVERSATION_CACHE_SIZE = 100

# Default HTTP timeout in seconds
DEFAULT_TIMEOUT = 30.0


class ClientCore:
    """Core client infrastructure for HTTP and RPC operations.

    Handles:
    - HTTP client lifecycle (open/close)
    - RPC call encoding/decoding
    - Authentication headers
    - Conversation cache

    This class is used internally by the sub-client APIs (NotebooksAPI,
    ArtifactsAPI, etc.) and should not be used directly.
    """

    def __init__(self, auth: AuthTokens, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the core client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
        """
        self.auth = auth
        self._timeout = timeout
        self._http_client: Optional[httpx.AsyncClient] = None
        # Request ID counter for chat API (must be unique per request)
        self._reqid_counter: int = 100000
        # OrderedDict for FIFO eviction when cache exceeds MAX_CONVERSATION_CACHE_SIZE
        self._conversation_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    async def open(self) -> None:
        """Open the HTTP client connection.

        Called automatically by NotebookLMClient.__aenter__.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Cookie": self.auth.cookie_header,
                },
                timeout=self._timeout,
            )

    async def close(self) -> None:
        """Close the HTTP client connection.

        Called automatically by NotebookLMClient.__aexit__.
        """
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    @property
    def is_open(self) -> bool:
        """Check if the HTTP client is open."""
        return self._http_client is not None

    def update_auth_headers(self) -> None:
        """Update HTTP client headers with current auth tokens.

        Call this after modifying auth tokens (e.g., after refresh_auth())
        to ensure the HTTP client uses the updated credentials.

        Raises:
            RuntimeError: If client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        self._http_client.headers["Cookie"] = self.auth.cookie_header

    def _build_url(self, rpc_method: RPCMethod, source_path: str = "/") -> str:
        """Build the batchexecute URL for an RPC call.

        Args:
            rpc_method: The RPC method to call.
            source_path: The source path parameter (usually notebook path).

        Returns:
            Full URL with query parameters.
        """
        params = {
            "rpcids": rpc_method.value,
            "source-path": source_path,
            "f.sid": self.auth.session_id,
            "rt": "c",
        }
        return f"{BATCHEXECUTE_URL}?{urlencode(params)}"

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
    ) -> Any:
        """Make an RPC call to the NotebookLM API.

        Args:
            method: The RPC method to call.
            params: Parameters for the RPC call (nested list structure).
            source_path: The source path parameter (usually /notebook/{id}).
            allow_null: If True, don't raise error when response is null.

        Returns:
            Decoded response data.

        Raises:
            RuntimeError: If client is not initialized (not in context manager).
            httpx.HTTPStatusError: If HTTP request fails.
            RPCError: If RPC call fails or returns unexpected data.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        url = self._build_url(method, source_path)
        rpc_request = encode_rpc_request(method, params)
        body = build_request_body(rpc_request, self.auth.csrf_token)

        try:
            response = await self._http_client.post(url, content=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RPCError(
                f"HTTP {e.response.status_code} calling {method.name}: {e.response.reason_phrase}",
                rpc_id=method.value,
            ) from e
        except httpx.RequestError as e:
            raise RPCError(
                f"Request failed calling {method.name}: {e}",
                rpc_id=method.value,
            ) from e

        try:
            return decode_response(
                response.text, method.value, allow_null=allow_null, debug=DEBUG_RPC
            )
        except RPCError:
            # Re-raise RPCError as-is (already has context from decoder)
            raise
        except Exception as e:
            raise RPCError(
                f"Failed to decode response for {method.name}: {e}",
                rpc_id=method.value,
            ) from e

    def get_http_client(self) -> httpx.AsyncClient:
        """Get the underlying HTTP client for direct requests.

        Used by download operations that need direct HTTP access.

        Returns:
            The httpx.AsyncClient instance.

        Raises:
            RuntimeError: If client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._http_client

    def cache_conversation_turn(
        self, conversation_id: str, query: str, answer: str, turn_number: int
    ) -> None:
        """Cache a conversation turn locally.

        Uses FIFO eviction when cache exceeds MAX_CONVERSATION_CACHE_SIZE.

        Args:
            conversation_id: The conversation ID.
            query: The user's question.
            answer: The AI's response.
            turn_number: The turn number in the conversation.
        """
        is_new_conversation = conversation_id not in self._conversation_cache

        # Only evict when adding a NEW conversation at capacity
        if is_new_conversation:
            while len(self._conversation_cache) >= MAX_CONVERSATION_CACHE_SIZE:
                # popitem(last=False) removes oldest entry (FIFO)
                self._conversation_cache.popitem(last=False)
            self._conversation_cache[conversation_id] = []

        self._conversation_cache[conversation_id].append({
            "query": query,
            "answer": answer,
            "turn_number": turn_number,
        })

    def get_cached_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        """Get cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of cached turns, or empty list if not found.
        """
        return self._conversation_cache.get(conversation_id, [])

    def clear_conversation_cache(self, conversation_id: Optional[str] = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        if conversation_id:
            if conversation_id in self._conversation_cache:
                del self._conversation_cache[conversation_id]
                return True
            return False
        else:
            self._conversation_cache.clear()
            return True
