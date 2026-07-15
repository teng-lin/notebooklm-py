from __future__ import annotations

import os
from pathlib import Path


def get_media_root() -> Path:
    raw = os.environ.get("NOTEBOOKLM_MEDIA_ROOT", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".notebooklm" / "data"


MEDIA_ROOT = get_media_root()


class StorageManager:
    def __init__(self, media_root: str | Path | None = None) -> None:
        self._root = Path(media_root).expanduser().resolve() if media_root else MEDIA_ROOT

    @property
    def root(self) -> Path:
        return self._root

    def save_source_file(
        self,
        user_id: int | str,
        notebook_id: int | str,
        source_id: int | str,
        filename: str,
        content: bytes,
    ) -> str:
        dest_dir = self._root / "sources" / str(user_id) / str(notebook_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{source_id}_{filename}"
        dest.write_bytes(content)
        return str(dest.relative_to(self._root))

    def save_generated_file(
        self,
        user_id: int | str,
        notebook_id: int | str,
        content_type: str,
        content_id: int | str,
        ext: str,
        content: bytes,
    ) -> str:
        dest_dir = self._root / "generated" / str(user_id) / str(notebook_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{content_type}_{content_id}.{ext}"
        dest.write_bytes(content)
        return str(dest.relative_to(self._root))

    def get_file(self, relative_path: str) -> bytes:
        full = self._resolve(relative_path)
        return full.read_bytes()

    def get_file_path(self, relative_path: str) -> Path:
        return self._resolve(relative_path)

    def get_file_url(self, relative_path: str) -> str:
        return f"/media/{relative_path}"

    def delete_file(self, relative_path: str) -> None:
        full = self._resolve(relative_path)
        if full.exists():
            full.unlink()

    def file_exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).exists()

    def _resolve(self, relative_path: str) -> Path:
        sanitized = Path(relative_path).as_posix()
        full = (self._root / sanitized).resolve()
        if not str(full).startswith(str(self._root.resolve())):
            raise ValueError(f"Path traversal detected: {relative_path}")
        return full
