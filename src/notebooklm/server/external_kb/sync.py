from __future__ import annotations

import json
import uuid
from typing import Any

from ..storage import StorageManager


async def import_document_to_notebook(
    connector: Any,
    doc_id: str,
    notebook_id: str,
    doc_title: str = "imported",
    content_data: dict | None = None,
) -> dict[str, Any]:
    user_id = getattr(connector, "_current_user_id", None)
    if user_id is None:
        user_id = 0

    local_filename = f"{uuid.uuid4().hex}_{doc_title}"
    text_content = json.dumps(content_data or {}, ensure_ascii=False)
    storage = StorageManager()

    relative_path = storage.save_source_file(
        user_id=str(user_id),
        notebook_id=str(notebook_id),
        source_id=uuid.uuid4().hex[:8],
        filename=local_filename,
        content=text_content.encode("utf-8"),
    )

    return {"source_id": 0, "local_path": relative_path}
