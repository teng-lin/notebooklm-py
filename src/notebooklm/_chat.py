"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.
"""

import json
import logging
import os
import uuid
from typing import Any
from urllib.parse import quote, urlencode

from ._core import ClientCore
from .rpc import QUERY_URL, RPCMethod
from .types import AskResult, ConversationTurn

logger = logging.getLogger(__name__)


class ChatAPI:
    """Operations for notebook chat/conversations.

    Provides methods for asking questions to notebooks and managing
    conversation history with follow-up support.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Ask a question
            result = await client.chat.ask(notebook_id, "What is X?")
            print(result.answer)

            # Follow-up question
            result = await client.chat.ask(
                notebook_id,
                "Can you elaborate?",
                conversation_id=result.conversation_id
            )
    """

    def __init__(self, core: ClientCore):
        """Initialize the chat API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

    async def ask(
        self,
        notebook_id: str,
        question: str,
        source_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> AskResult:
        """Ask the notebook a question.

        Args:
            notebook_id: The notebook ID.
            question: The question to ask.
            source_ids: Specific source IDs to query. If None, uses all sources.
            conversation_id: Existing conversation ID for follow-up questions.

        Returns:
            AskResult with answer, conversation_id, and turn info.

        Example:
            # New conversation
            result = await client.chat.ask(notebook_id, "What is machine learning?")

            # Follow-up
            result = await client.chat.ask(
                notebook_id,
                "How does it differ from deep learning?",
                conversation_id=result.conversation_id
            )
        """
        if source_ids is None:
            source_ids = await self._get_source_ids(notebook_id)

        is_new_conversation = conversation_id is None
        if is_new_conversation:
            conversation_id = str(uuid.uuid4())
            conversation_history = None
        else:
            assert conversation_id is not None  # Type narrowing for mypy
            conversation_history = self._build_conversation_history(conversation_id)

        # At this point, conversation_id is guaranteed to be str
        assert conversation_id is not None

        sources_array = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            sources_array,
            question,
            conversation_history,
            [2, None, [1]],
            conversation_id,
        ]

        params_json = json.dumps(params, separators=(",", ":"))
        f_req = [None, params_json]
        f_req_json = json.dumps(f_req, separators=(",", ":"))

        encoded_req = quote(f_req_json, safe="")

        body_parts = [f"f.req={encoded_req}"]
        if self._core.auth.csrf_token:
            encoded_at = quote(self._core.auth.csrf_token, safe="")
            body_parts.append(f"at={encoded_at}")

        body = "&".join(body_parts) + "&"

        self._core._reqid_counter += 100000
        url_params = {
            "bl": os.environ.get("NOTEBOOKLM_BL", "boq_labs-tailwind-frontend_20251221.14_p0"),
            "hl": "en",
            "_reqid": str(self._core._reqid_counter),
            "rt": "c",
        }
        if self._core.auth.session_id:
            url_params["f.sid"] = self._core.auth.session_id

        query_string = urlencode(url_params)
        url = f"{QUERY_URL}?{query_string}"

        http_client = self._core.get_http_client()
        response = await http_client.post(url, content=body)
        response.raise_for_status()

        answer_text = self._parse_ask_response(response.text)

        if answer_text:
            turns = self._core.get_cached_conversation(conversation_id)
            turn_number = len(turns) + 1
            self._core.cache_conversation_turn(conversation_id, question, answer_text, turn_number)
        else:
            turns = self._core.get_cached_conversation(conversation_id)
            turn_number = len(turns)

        return AskResult(
            answer=answer_text,
            conversation_id=conversation_id,
            turn_number=turn_number,
            is_follow_up=not is_new_conversation,
            raw_response=response.text[:1000],
        )

    async def get_history(self, notebook_id: str, limit: int = 20) -> Any:
        """Get conversation history from the API.

        Args:
            notebook_id: The notebook ID.
            limit: Maximum number of conversations to retrieve.

        Returns:
            Raw conversation history data from API.
        """
        params: list[Any] = [[], None, notebook_id, limit]
        return await self._core.rpc_call(
            RPCMethod.GET_CONVERSATION_HISTORY,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    def get_cached_turns(self, conversation_id: str) -> list[ConversationTurn]:
        """Get locally cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of ConversationTurn objects.
        """
        cached = self._core.get_cached_conversation(conversation_id)
        return [
            ConversationTurn(
                query=turn["query"],
                answer=turn["answer"],
                turn_number=turn["turn_number"],
            )
            for turn in cached
        ]

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        return self._core.clear_conversation_cache(conversation_id)

    async def configure(
        self,
        notebook_id: str,
        goal: Any | None = None,
        response_length: Any | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        """Configure chat persona and response settings for a notebook.

        Args:
            notebook_id: The notebook ID.
            goal: Chat persona/goal (ChatGoal enum: DEFAULT, CUSTOM, LEARNING_GUIDE).
            response_length: Response verbosity (ChatResponseLength enum).
            custom_prompt: Custom instructions (required if goal is CUSTOM).

        Raises:
            ValueError: If goal is CUSTOM but custom_prompt is not provided.
        """
        from .rpc import ChatGoal, ChatResponseLength

        if goal is None:
            goal = ChatGoal.DEFAULT
        if response_length is None:
            response_length = ChatResponseLength.DEFAULT

        if goal == ChatGoal.CUSTOM and not custom_prompt:
            raise ValueError("custom_prompt is required when goal is CUSTOM")

        if goal == ChatGoal.CUSTOM:
            goal_array = [goal.value, custom_prompt]
        else:
            goal_array = [goal.value]

        chat_settings = [goal_array, [response_length.value]]
        params = [
            notebook_id,
            [[None, None, None, None, None, None, None, chat_settings]],
        ]

        await self._core.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def set_mode(self, notebook_id: str, mode: Any) -> None:
        """Set chat mode using predefined configurations.

        Args:
            notebook_id: The notebook ID.
            mode: Predefined ChatMode (DEFAULT, LEARNING_GUIDE, CONCISE, DETAILED).
        """
        from .rpc import ChatGoal, ChatResponseLength
        from .types import ChatMode

        mode_configs = {
            ChatMode.DEFAULT: (ChatGoal.DEFAULT, ChatResponseLength.DEFAULT, None),
            ChatMode.LEARNING_GUIDE: (ChatGoal.LEARNING_GUIDE, ChatResponseLength.LONGER, None),
            ChatMode.CONCISE: (ChatGoal.DEFAULT, ChatResponseLength.SHORTER, None),
            ChatMode.DETAILED: (ChatGoal.DEFAULT, ChatResponseLength.LONGER, None),
        }

        goal, length, prompt = mode_configs[mode]
        await self.configure(notebook_id, goal, length, prompt)

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _get_source_ids(self, notebook_id: str) -> list[str]:
        """Extract source IDs from notebook data."""
        params = [notebook_id, None, [2], None, 0]
        notebook_data = await self._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        source_ids: list[str] = []
        if not notebook_data or not isinstance(notebook_data, list):
            return source_ids

        try:
            if len(notebook_data) > 0 and isinstance(notebook_data[0], list):
                notebook_info = notebook_data[0]
                if len(notebook_info) > 1 and isinstance(notebook_info[1], list):
                    sources = notebook_info[1]
                    for source in sources:
                        if isinstance(source, list) and len(source) > 0:
                            first = source[0]
                            if isinstance(first, list) and len(first) > 0:
                                sid = first[0]
                                if isinstance(sid, str):
                                    source_ids.append(sid)
        except (IndexError, TypeError):
            pass

        return source_ids

    def _build_conversation_history(self, conversation_id: str) -> list | None:
        """Build conversation history for follow-up requests."""
        turns = self._core.get_cached_conversation(conversation_id)
        if not turns:
            return None

        history = []
        for turn in turns:
            history.append([turn["answer"], None, 2])
            history.append([turn["query"], None, 1])
        return history

    def _parse_ask_response(self, response_text: str) -> str:
        """Parse the streaming response to extract the answer."""
        if response_text.startswith(")]}'"):
            response_text = response_text[4:]

        lines = response_text.strip().split("\n")
        longest_answer = ""

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            try:
                int(line)
                i += 1
                if i < len(lines):
                    json_str = lines[i]
                    text, is_answer = self._extract_answer_from_chunk(json_str)
                    if text and is_answer and len(text) > len(longest_answer):
                        longest_answer = text
                i += 1
            except ValueError:
                text, is_answer = self._extract_answer_from_chunk(line)
                if text and is_answer and len(text) > len(longest_answer):
                    longest_answer = text
                i += 1

        if not longest_answer:
            logger.debug(
                "No answer extracted from response (%d lines parsed)",
                len(lines),
            )
        return longest_answer

    def _extract_answer_from_chunk(self, json_str: str) -> tuple[str | None, bool]:
        """Extract answer text from a response chunk."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None, False

        if not isinstance(data, list):
            return None, False

        for item in data:
            if not isinstance(item, list) or len(item) < 3:
                continue
            if item[0] != "wrb.fr":
                continue

            inner_json = item[2]
            if not isinstance(inner_json, str):
                continue

            try:
                inner_data = json.loads(inner_json)
                if isinstance(inner_data, list) and len(inner_data) > 0:
                    first = inner_data[0]
                    if isinstance(first, list) and len(first) > 0:
                        text = first[0]
                        if isinstance(text, str) and len(text) > 20:
                            is_answer = False
                            if len(first) > 4 and isinstance(first[4], list):
                                type_info = first[4]
                                if len(type_info) > 0 and type_info[-1] == 1:
                                    is_answer = True
                            return text, is_answer
            except json.JSONDecodeError:
                continue

        return None, False
