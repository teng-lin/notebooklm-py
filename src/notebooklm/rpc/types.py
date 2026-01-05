"""RPC types and constants for NotebookLM API."""

from enum import Enum


# NotebookLM API endpoints
BATCHEXECUTE_URL = "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
QUERY_URL = "https://notebooklm.google.com/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed"


class RPCMethod(str, Enum):
    """RPC method IDs for NotebookLM operations.

    These are obfuscated method identifiers used by the batchexecute API.
    Reverse-engineered from network traffic analysis.
    """

    # Notebook operations
    LIST_NOTEBOOKS = "wXbhsf"
    CREATE_NOTEBOOK = "CCqFvf"
    GET_NOTEBOOK = "rLM1Ne"
    RENAME_NOTEBOOK = "s0tc2d"
    DELETE_NOTEBOOK = "WWINqb"

    # Source operations
    ADD_SOURCE = "izAoDd"
    DELETE_SOURCE = "tGMBJ"
    GET_SOURCE = "hizoJc"
    REFRESH_SOURCE = "FLmJqe"
    CHECK_SOURCE_FRESHNESS = "yR9Yof"
    MUTATE_SOURCE = "b7Wfje"
    DISCOVER_SOURCES = "qXyaNe"

    # Summary and query
    SUMMARIZE = "VfAZjd"
    GET_SOURCE_GUIDE = "tr032e"

    # Query endpoint (not a batchexecute RPC ID)
    QUERY_ENDPOINT = "/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed"

    # Studio content generation
    CREATE_AUDIO = "AHyHrd"
    GET_AUDIO = "VUsiyb"
    DELETE_AUDIO = "sJDbic"
    CREATE_VIDEO = "R7cb6c"
    POLL_STUDIO = "gArtLc"
    DELETE_STUDIO = "V5N4be"
    CREATE_ARTIFACT = "xpWGLf"
    GET_ARTIFACT = "BnLyuf"
    LIST_ARTIFACTS = "gArtLc"

    # Research
    START_FAST_RESEARCH = "Ljjv0c"
    START_DEEP_RESEARCH = "QA9ei"
    POLL_RESEARCH = "e3bVqc"
    IMPORT_RESEARCH = "LBwxtb"

    ACT_ON_SOURCES = "yyryJe"
    GENERATE_MIND_MAP = "yyryJe"
    CREATE_NOTE = "CYK0Xb"
    GET_NOTES = "cFji9"

    # Note operations
    MUTATE_NOTE = "cYAfTb"
    DELETE_NOTE = "AH0mwd"

    # Artifact management
    RENAME_ARTIFACT = "rc3d8d"
    UPDATE_ARTIFACT = "DJezBc"
    DELETE_ARTIFACT = "WxBZtb"
    EXPORT_ARTIFACT = "Krh3pd"
    LIST_ARTIFACTS_ALT = "LfTXoe"

    # Conversation
    GET_CONVERSATION_HISTORY = "hPTbtc"

    # Sharing operations
    SHARE_AUDIO = "RGP97b"
    SHARE_PROJECT = "QDyure"

    # Additional notebook operations
    LIST_FEATURED_PROJECTS = "nS9Qlc"
    REMOVE_RECENTLY_VIEWED = "fejl7e"
    PROJECT_ANALYTICS = "AUrzMb"

    # Guidebooks
    GET_GUIDEBOOKS = "YJBpHc"
    UPDATE_GUIDEBOOK = "R6smae"
    DELETE_GUIDEBOOK = "LJyzeb"


class StudioContentType(int, Enum):
    """Types of studio content that can be generated.

    These are integer codes used in the R7cb6c RPC call.
    """

    AUDIO = 1
    REPORT = 2  # Includes: Briefing Doc, Study Guide, Blog Post, White Paper, Research Proposal, etc.
    VIDEO = 3
    QUIZ = 4  # Also used for flashcards
    MIND_MAP = 5
    # Note: Type 6 appears unused in current API
    INFOGRAPHIC = 7
    SLIDE_DECK = 8
    DATA_TABLE = 9


class AudioFormat(int, Enum):
    """Audio overview format options."""

    DEEP_DIVE = 1
    BRIEF = 2
    CRITIQUE = 3
    DEBATE = 4


class AudioLength(int, Enum):
    """Audio overview length options."""

    SHORT = 1
    DEFAULT = 2
    LONG = 3


class VideoFormat(int, Enum):
    """Video overview format options."""

    EXPLAINER = 1
    BRIEF = 2


class VideoStyle(int, Enum):
    """Video visual style options."""

    AUTO_SELECT = 1
    CUSTOM = 2
    CLASSIC = 3
    WHITEBOARD = 4
    KAWAII = 5
    ANIME = 6
    WATERCOLOR = 7
    RETRO_PRINT = 8
    HERITAGE = 9
    PAPER_CRAFT = 10


class QuizQuantity(int, Enum):
    """Quiz/Flashcards quantity options.

    Note: API uses 1 for fewer questions, 2 for standard/more.
    """

    FEWER = 1
    STANDARD = 2
    MORE = 2


class QuizDifficulty(int, Enum):
    """Quiz/Flashcards difficulty options."""

    EASY = 1
    MEDIUM = 2
    HARD = 3


class InfographicOrientation(int, Enum):
    """Infographic orientation options."""

    LANDSCAPE = 1
    PORTRAIT = 2
    SQUARE = 3


class InfographicDetail(int, Enum):
    """Infographic detail level options."""

    CONCISE = 1
    STANDARD = 2
    DETAILED = 3


class SlideDeckFormat(int, Enum):
    """Slide deck format options."""

    DETAILED_DECK = 1
    PRESENTER_SLIDES = 2


class SlideDeckLength(int, Enum):
    """Slide deck length options."""

    DEFAULT = 1
    SHORT = 2
