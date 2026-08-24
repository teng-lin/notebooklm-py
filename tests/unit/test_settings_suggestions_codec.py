"""Golden wire and tolerant decode evidence for the P6.6 web codecs."""

from __future__ import annotations

import pytest

from notebooklm._binding import CodecPayload
from notebooklm._records import (
    AccountLimitsRecord,
    ArtifactSuggestReportsInput,
    SettingsGetInput,
    SettingsGetLimitsInput,
    SettingsSetLanguageInput,
)
from notebooklm._web.codec.settings import (
    decode_account_limits,
    decode_get_account_limits,
    decode_get_user_settings,
    decode_set_output_language,
    decode_settings_get,
    decode_settings_get_limits,
    decode_settings_set_language,
    encode_get_user_settings,
    encode_set_output_language,
    encode_settings_get,
    encode_settings_get_limits,
    encode_settings_set_language,
)
from notebooklm._web.codec.suggestions import (
    decode_artifact_suggest_reports,
    decode_prompt_source_ids,
    decode_prompt_suggestions,
    decode_report_suggestions,
    encode_artifact_suggest_reports,
    encode_prompt_suggestions,
    encode_report_suggestions,
)
from notebooklm.exceptions import UnknownRPCMethodError


def test_settings_request_codecs_match_recorded_golden_arrays_and_are_fresh() -> None:
    first = encode_get_user_settings()
    second = encode_get_user_settings()

    assert first == [None, [1, None, None, None, None, None, None, None, None, None, [1]]]
    assert first is not second
    assert first[1] is not second[1]
    assert encode_set_output_language("ja") == [[[None, [[None, None, None, None, ["ja"]]]]]]


def test_row_payloads_match_the_handler_kwargs_golden() -> None:
    """P9.3 rows: params plus the exact route/option flags each handler passed."""
    get = encode_settings_get(SettingsGetInput())
    limits = encode_settings_get_limits(SettingsGetLimitsInput())
    language = encode_settings_set_language(SettingsSetLanguageInput("ja"))
    reports = encode_artifact_suggest_reports(ArtifactSuggestReportsInput("nb"))

    assert get == CodecPayload(params=encode_get_user_settings(), source_path="/")
    assert limits == CodecPayload(params=encode_get_user_settings(), source_path="/")
    assert get.params is not limits.params
    assert language == CodecPayload(
        params=[[[None, [[None, None, None, None, ["ja"]]]]]], source_path="/"
    )
    assert reports == CodecPayload(params=[[2], "nb"], source_path="/notebook/nb", allow_null=True)
    for payload in (get, limits, language):
        assert payload.allow_null is False
        assert payload.raise_on_null_status is False
        assert payload.attempt_timeout is None
    assert reports.raise_on_null_status is False


def test_row_decoders_delegate_to_the_recorded_projections() -> None:
    response = [[None, [True, 200, "unknown-source-limit", None, 99], [True]]]
    assert decode_settings_get(SettingsGetInput(), response) == decode_get_user_settings(response)
    assert decode_settings_get_limits(SettingsGetLimitsInput(), response) == (
        decode_get_account_limits(response)
    )
    assert decode_settings_set_language(SettingsSetLanguageInput("ja"), [None, None, [True]]) == (
        decode_set_output_language([None, None, [True]])
    )
    rows = [[["Title", "Description", "Prompt", None, None, "Advanced"]]]
    assert decode_artifact_suggest_reports(ArtifactSuggestReportsInput("nb"), rows) == (
        decode_report_suggestions(rows)
    )
    assert (
        decode_artifact_suggest_reports(ArtifactSuggestReportsInput("nb"), None).suggestions == ()
    )


def test_settings_decode_preserves_unknown_limits_and_optional_language_policy() -> None:
    response = [[None, [True, 200, "unknown-source-limit", None, 99], [True]]]

    limits = decode_account_limits(response)
    assert limits == AccountLimitsRecord(
        notebook_limit=200,
        source_limit=None,
        raw_limits=(True, 200, "unknown-source-limit", None, 99),
        tier=99,
    )
    assert decode_get_account_limits(response).limits == limits
    assert decode_get_user_settings(response).settings.output_language is None
    assert decode_set_output_language([None, None, [True]]).output_language is None


def test_settings_decode_preserves_present_unknown_language_leaves() -> None:
    get_response = [[None, [], [None, None, None, None, [17]]]]
    set_response = [None, None, [None, None, None, None, [{"future": "language"}]]]

    assert decode_get_user_settings(get_response).settings.output_language == 17
    assert decode_set_output_language(set_response).output_language == {"future": "language"}


def test_settings_language_mandatory_prefix_still_raises_drift() -> None:
    with pytest.raises(UnknownRPCMethodError) as caught:
        decode_get_user_settings([["truncated"]])

    assert caught.value.method_id == "ZwVcOc"
    assert caught.value.source == "_settings._extract_output_language"


def test_suggestion_request_codecs_match_recorded_golden_arrays() -> None:
    first = encode_prompt_suggestions("nb", ["src-a", "src-b"], mode=7, query=" steer ")
    second = encode_prompt_suggestions("nb", [], mode=4, query="  ")

    assert first == [
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
        "nb",
        [["src-a"], ["src-b"]],
        7,
        None,
        " steer ",
    ]
    assert second[0] == first[0]
    assert second[0] is not first[0]
    assert second[2:] == [[], 4, None, None]
    assert encode_report_suggestions("nb") == [[2], "nb"]


@pytest.mark.parametrize("mode", [0, 11])
def test_prompt_encoder_rejects_unrecognized_mode_before_wire(mode: int) -> None:
    with pytest.raises(ValueError, match="1..10"):
        encode_prompt_suggestions("nb", [], mode=mode)


def test_suggestion_decoders_preserve_best_effort_unknown_values() -> None:
    prompts = decode_prompt_suggestions([[["\n- Title", "\n- Prompt"], ["short"], ["Also", 42]]])
    reports = decode_report_suggestions(
        [[["Report", "Description", None, None, "Prompt", "unknown-level"]]]
    )

    assert [(row.title, row.prompt) for row in prompts.suggestions] == [
        ("Title", "Prompt"),
        ("Also", ""),
    ]
    assert reports.suggestions[0].audience_level == "unknown-level"
    assert decode_prompt_suggestions(None).suggestions == ()
    assert decode_report_suggestions([[]]).suggestions == ()


def test_prompt_source_lookup_decodes_drive_and_ordinary_ids_tolerantly() -> None:
    response = [
        [
            "Notebook",
            [
                [["src-ordinary"]],
                [[None, True, ["src-drive"]]],
                "malformed",
            ],
            "nb",
        ]
    ]

    assert decode_prompt_source_ids(response, notebook_id="nb") == (
        "src-ordinary",
        "src-drive",
    )
    assert decode_prompt_source_ids([["truncated"]], notebook_id="nb") == ()
