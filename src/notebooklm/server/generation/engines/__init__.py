from __future__ import annotations

from ..registry import GeneratorRegistry
from .document_engine import DocumentGenerator
from .infographic_engine import InfographicGenerator
from .mindmap_engine import MindmapGenerator
from .podcast_engine import PodcastGenerator
from .ppt_engine import PptGenerator
from .video_engine import VideoGenerator

GeneratorRegistry.register("document", DocumentGenerator)
GeneratorRegistry.register("podcast", PodcastGenerator)
GeneratorRegistry.register("ppt", PptGenerator)
GeneratorRegistry.register("mindmap", MindmapGenerator)
GeneratorRegistry.register("infographic", InfographicGenerator)
GeneratorRegistry.register("video", VideoGenerator)

__all__ = [
    "DocumentGenerator",
    "PodcastGenerator",
    "PptGenerator",
    "MindmapGenerator",
    "InfographicGenerator",
    "VideoGenerator",
]
