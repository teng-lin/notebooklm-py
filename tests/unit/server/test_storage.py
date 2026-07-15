from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from notebooklm.server.storage import StorageManager


@pytest.fixture
def tmp_media():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class TestStorageManager:
    def test_save_source_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "test.pdf", b"%PDF-content")
        assert rel == "sources/1/10/100_test.pdf"
        assert (tmp_media / rel).read_bytes() == b"%PDF-content"

    def test_save_generated_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_generated_file(1, 10, "ppt", 200, "pptx", b"%PPTX")
        assert rel == "generated/1/10/ppt_200.pptx"
        assert (tmp_media / rel).read_bytes() == b"%PPTX"

    def test_get_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "doc.txt", b"hello")
        assert mgr.get_file(rel) == b"hello"

    def test_get_file_url(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        assert mgr.get_file_url("sources/1/doc.txt") == "/media/sources/1/doc.txt"

    def test_delete_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "tmp.txt", b"data")
        assert mgr.file_exists(rel)
        mgr.delete_file(rel)
        assert not mgr.file_exists(rel)

    def test_path_traversal_raises(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        with pytest.raises(ValueError, match="Path traversal"):
            mgr._resolve("../../etc/passwd")

    def test_default_media_root(self):
        import os
        os.environ.pop("NOTEBOOKLM_MEDIA_ROOT", None)
        from notebooklm.server.storage import MEDIA_ROOT
        assert str(MEDIA_ROOT).endswith(".notebooklm/data")
