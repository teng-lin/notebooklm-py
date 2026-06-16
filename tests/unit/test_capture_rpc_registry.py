"""Unit tests for ``scripts/capture_rpc_registry.py`` (offline; no network/auth).

Covers the pure parse/extract/diff logic, including the edge cases that bit the
original prototype: non-id enum constants (``blog_post``) must be filtered, and an
id that is present in the bundle but not parsed must NOT be reported as a rotation.
"""

from __future__ import annotations

from scripts.capture_rpc_registry import diff, extract_registry, parse_ids_from_text

# Mixed quote styles on purpose — exercises the quote-agnostic parsing of both
# the enum (CREATE is single-quoted) and the bundle (the CCqFvf registration).
_TYPES = """
class RPCMethod(str, Enum):
    LIST = "wXbhsf"
    CREATE = 'CCqFvf'
    GONE = "ZZxxYY"
    UNPARSED = "PuPpY1"
    NOT_AN_ID = "blog_post"

class SomethingElse(str, Enum):
    OTHER = "abcdef"
"""

# Two well-formed registrations, one unmapped registration, and the UNPARSED id
# present only as a bare string (not in registration form).
_BUNDLE = (
    'x=new _.uD("wXbhsf",kF,csb,[_.Ue,!1,_.Se,"/Svc.List"]);'
    "y=new _.uD('CCqFvf',a.b,c,[_.Ue,!0,_.Se,'/Svc.Create']);"
    'z=new _.uD("NewOne",p,q,[_.Ue,!1,_.Se,"/Svc.Brand"]);'
    "log('PuPpY1');"
)


def test_parse_ids_filters_non_ids_and_other_enums() -> None:
    ids = parse_ids_from_text(_TYPES)
    # blog_post (underscore) filtered out; SomethingElse.OTHER excluded (different class)
    assert ids == {
        "wXbhsf": "LIST",
        "CCqFvf": "CREATE",
        "ZZxxYY": "GONE",
        "PuPpY1": "UNPARSED",
    }


def test_extract_registry() -> None:
    assert extract_registry(_BUNDLE) == {
        "wXbhsf": "/Svc.List",
        "CCqFvf": "/Svc.Create",
        "NewOne": "/Svc.Brand",
    }


def test_diff_buckets() -> None:
    ours = parse_ids_from_text(_TYPES)
    live = extract_registry(_BUNDLE)
    buckets = diff(ours, live, _BUNDLE)

    assert set(buckets["confirmed"]) == {"wXbhsf", "CCqFvf"}
    assert buckets["confirmed"]["wXbhsf"] == "/Svc.List"
    # GONE is nowhere in the bundle -> a real rotation/stale alarm
    assert set(buckets["absent"]) == {"ZZxxYY"}
    # UNPARSED appears as a string but not as a parsed registration -> not an alarm
    assert set(buckets["present_unparsed"]) == {"PuPpY1"}
    # NewOne is declared by the bundle but absent from our enum
    assert set(buckets["unmapped"]) == {"NewOne"}
