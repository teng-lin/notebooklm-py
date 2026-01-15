"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.
"""

import json
import logging
import os
import re
import uuid
from typing import Any
from urllib.parse import quote, urlencode

from ._core import ClientCore
from .rpc import QUERY_URL, RPCMethod
from .types import AskResult, ChatReference, ConversationTurn

logger = logging.getLogger(__name__)

# UUID pattern for validating source IDs (compiled once at module level)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Minimum answer length to be considered valid (filters out status messages)
_MIN_ANSWER_LENGTH = 20


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
        logger.debug(
            "Asking question in notebook %s (conversation=%s)",
            notebook_id,
            conversation_id or "new",
        )
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        is_new_conversation = conversation_id is None
        if is_new_conversation:
            conversation_id = str(uuid.uuid4())
            conversation_history = None
        else:
            assert conversation_id is not None  # Type narrowing for mypy
            conversation_history = self._build_conversation_history(conversation_id)

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

        answer_text, references = self._parse_ask_response_with_references(response.text)

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
            references=references,
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
        logger.debug("Getting conversation history for notebook %s (limit=%d)", notebook_id, limit)
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
        logger.debug("Configuring chat for notebook %s", notebook_id)
        from .rpc import ChatGoal, ChatResponseLength

        if goal is None:
            goal = ChatGoal.DEFAULT
        if response_length is None:
            response_length = ChatResponseLength.DEFAULT

        if goal == ChatGoal.CUSTOM and not custom_prompt:
            raise ValueError("custom_prompt is required when goal is CUSTOM")

        goal_array = [goal.value, custom_prompt] if goal == ChatGoal.CUSTOM else [goal.value]

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
                        if isinstance(text, str) and len(text) > _MIN_ANSWER_LENGTH:
                            is_answer = False
                            if len(first) > 4 and isinstance(first[4], list):
                                type_info = first[4]
                                if len(type_info) > 0 and type_info[-1] == 1:
                                    is_answer = True
                            return text, is_answer
            except json.JSONDecodeError:
                continue

        return None, False

    def _parse_ask_response_with_references(
        self, response_text: str
    ) -> tuple[str, list[ChatReference]]:
        """Parse the streaming response to extract answer and references.

        Returns:
            Tuple of (answer_text, list of ChatReference objects).
        """

        if response_text.startswith(")]}'"):
            response_text = response_text[4:]

        lines = response_text.strip().split("\n")
        longest_answer = ""
        all_references: list[ChatReference] = []

        def process_chunk(json_str: str) -> None:
            """Process a JSON chunk, updating longest_answer and all_references."""
            nonlocal longest_answer
            text, is_answer, refs = self._extract_answer_and_refs_from_chunk(json_str)
            if text and is_answer and len(text) > len(longest_answer):
                longest_answer = text
            all_references.extend(refs)

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
                    process_chunk(lines[i])
                i += 1
            except ValueError:
                process_chunk(line)
                i += 1

        if not longest_answer:
            logger.debug(
                "No answer extracted from response (%d lines parsed)",
                len(lines),
            )

        # Assign citation numbers based on order of appearance
        for idx, ref in enumerate(all_references, start=1):
            if ref.citation_number is None:
                ref.citation_number = idx

        return longest_answer, all_references

    def _extract_answer_and_refs_from_chunk(
        self, json_str: str
    ) -> tuple[str | None, bool, list[ChatReference]]:
        """Extract answer text and references from a response chunk.

        Response structure (discovered via reverse engineering):
        - first[0]: answer text
        - first[1]: None
        - first[2]: [chunk_id_1, chunk_id_2, ..., session_hash] - chunk IDs (NOT source IDs)
        - first[3]: None
        - first[4]: Citation metadata
          - first[4][0]: Per-source citation positions with text spans
          - first[4][3]: Detailed citation array with structure:
            - cite[0][0]: chunk ID
            - cite[1][2]: relevance score
            - cite[1][4]: array of [text_passage, char_positions] items
            - cite[1][5][0][0][0]: parent SOURCE ID (this is the real source UUID)

        Returns:
            Tuple of (text, is_answer, references).
        """
        refs: list[ChatReference] = []

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None, False, refs

        if not isinstance(data, list):
            return None, False, refs

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
                        is_answer = False
                        if isinstance(text, str) and len(text) > _MIN_ANSWER_LENGTH:
                            if len(first) > 4 and isinstance(first[4], list):
                                type_info = first[4]
                                if len(type_info) > 0 and type_info[-1] == 1:
                                    is_answer = True

                            # Extract references from first[4][3] - the detailed citation array
                            # Each citation contains chunk ID, parent source ID, and cited text
                            refs = self._parse_citations(first)

                            return text, is_answer, refs
            except json.JSONDecodeError:
                continue

        return None, False, refs

    def _parse_citations(self, first: list) -> list[ChatReference]:
        """Parse citation details from response structure.

        The citation data is in first[4][3], which contains an array of citations.
        Each citation has:
          - cite[0][0]: chunk ID (internal reference)
          - cite[1][4]: array of text passages with character positions
          - cite[1][5]: nested structure containing the parent SOURCE ID (UUID)

        Note:
            This parsing relies on reverse-engineered response structures that
            Google can change at any time. Parsing failures are logged and
            result in graceful degradation (empty references list).

        Args:
            first: The first element of the parsed response.

        Returns:
            List of ChatReference objects with source IDs and cited text.
        """
        try:
            # Validate path to citations array: first[4][3]
            if len(first) <= 4 or not isinstance(first[4], list):
                return []
            type_info = first[4]
            if len(type_info) <= 3 or not isinstance(type_info[3], list):
                return []

            refs: list[ChatReference] = []
            for cite in type_info[3]:
                ref = self._parse_single_citation(cite)
                if ref is not None:
                    refs.append(ref)
            return refs
        except (IndexError, TypeError, AttributeError) as e:
            logger.debug(
                "Citation parsing failed (API structure may have changed): %s",
                e,
                exc_info=True,
            )
            return []

    def _parse_single_citation(self, cite: Any) -> ChatReference | None:
        """Parse a single citation entry into a ChatReference.

        Args:
            cite: A citation entry from the citations array.

        Returns:
            ChatReference if valid source ID found, None otherwise.
        """
        if not isinstance(cite, list) or len(cite) < 2:
            return None

        cite_inner = cite[1]
        if not isinstance(cite_inner, list):
            return None

        # Extract source ID from cite[1][5] - required for valid reference
        source_id_data = cite_inner[5] if len(cite_inner) > 5 else None
        source_id = self._extract_uuid_from_nested(source_id_data)
        if source_id is None:
            return None

        # Extract chunk ID from cite[0][0]
        chunk_id = None
        if isinstance(cite[0], list) and cite[0]:
            first_item = cite[0][0]
            if isinstance(first_item, str):
                chunk_id = first_item

        # Extract text passages and char positions from cite[1][4]
        cited_text, start_char, end_char = self._extract_text_passages(cite_inner)

        return ChatReference(
            source_id=source_id,
            cited_text=cited_text,
            start_char=start_char,
            end_char=end_char,
            chunk_id=chunk_id,
        )

    def _extract_text_passages(self, cite_inner: list) -> tuple[str | None, int | None, int | None]:
        """Extract cited text and character positions from citation data.

        Structure (discovered via analysis):
          cite_inner[4] = [[passage_data, ...], ...]
          passage_data = [start_char, end_char, nested_passages]
          nested_passages contains text at varying depths

        Args:
            cite_inner: The inner citation data (cite[1]).

        Returns:
            Tuple of (cited_text, start_char, end_char).
        """
        if len(cite_inner) <= 4 or not isinstance(cite_inner[4], list):
            return None, None, None

        texts: list[str] = []
        start_char: int | None = None
        end_char: int | None = None

        for passage_wrapper in cite_inner[4]:
            if not isinstance(passage_wrapper, list) or not passage_wrapper:
                continue
            passage_data = passage_wrapper[0]
            if not isinstance(passage_data, list) or len(passage_data) < 3:
                continue

            # Extract char positions from first valid passage
            if start_char is None and isinstance(passage_data[0], int):
                start_char = passage_data[0]
            if isinstance(passage_data[1], int):
                end_char = passage_data[1]

            # Extract text from nested structure
            self._collect_texts_from_nested(passage_data[2], texts)

        cited_text = " ".join(texts) if texts else None
        return cited_text, start_char, end_char

    def _collect_texts_from_nested(self, nested: Any, texts: list[str]) -> None:
        """Collect text strings from deeply nested passage structure.

        The text can appear at various levels of nesting. This walks through
        the structure looking for [start, end, text_value] triplets.

        Args:
            nested: Nested list structure to search.
            texts: List to append found text strings to.
        """
        if not isinstance(nested, list):
            return

        for nested_group in nested:
            if not isinstance(nested_group, list):
                continue
            for inner in nested_group:
                if not isinstance(inner, list) or len(inner) < 3:
                    continue
                text_val = inner[2]
                if isinstance(text_val, str) and text_val.strip():
                    texts.append(text_val.strip())
                elif isinstance(text_val, list):
                    for item in text_val:
                        if isinstance(item, str) and item.strip():
                            texts.append(item.strip())

    def _extract_uuid_from_nested(self, data: Any, max_depth: int = 10) -> str | None:
        """Recursively extract a UUID from nested list structures.

        The API returns source IDs in deeply nested list structures that can vary.
        This walks through the nesting to find the first valid UUID string.

        Args:
            data: Nested list data to search.
            max_depth: Maximum recursion depth to prevent stack overflow.

        Returns:
            UUID string if found, None otherwise.
        """
        if max_depth <= 0:
            logger.warning("Max recursion depth reached in UUID extraction")
            return None

        if data is None:
            return None

        if isinstance(data, str):
            return data if _UUID_PATTERN.match(data) else None

        if isinstance(data, list):
            for item in data:
                result = self._extract_uuid_from_nested(item, max_depth - 1)
                if result is not None:
                    return result

        return None
