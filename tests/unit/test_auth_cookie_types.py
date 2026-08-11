"""Unit and compatibility tests for the canonical cookie values and codecs.

The value layer owns ordered immutable projections while the legacy free
functions retain their identities as adapters over dependency-bottom codecs.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from notebooklm._auth import cookie_policy as _cookie_policy
from notebooklm._auth import cookies as _auth_cookies
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar


def _storage_state() -> dict:
    """A realistic multi-domain storage state, including rows that must drop."""
    return {
        "cookies": [
            {"name": "SID", "value": "sid-base", "domain": ".google.com", "path": "/"},
            # Same name, regional domain — lower priority tier than .google.com.
            {"name": "SID", "value": "sid-regional", "domain": ".google.com.sg", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
                "expires": 1800000000,
                "httpOnly": True,
                "secure": True,
            },
            {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "sapisid", "domain": ".google.com", "path": "/"},
            {"name": "LSID", "value": "lsid", "domain": "accounts.google.com", "path": "/"},
            {"name": "OSID", "value": "osid", "domain": "notebook.google.com", "path": "/"},
            # Must be dropped: not an allowlisted auth domain.
            {"name": "TRACKER", "value": "nope", "domain": ".evil.example", "path": "/"},
            # Must be dropped: malformed (no domain).
            {"name": "BROKEN", "value": "x", "path": "/"},
        ],
        "origins": [],
    }


class TestConstructorEquivalence:
    """Each constructor must agree with the legacy conversion it wraps."""

    def test_from_storage_state_matches_extract_cookies_with_domains(self) -> None:
        state = _storage_state()
        jar = CookieJar.from_storage_state(state)
        assert jar.to_domain_map() == _auth_cookies.extract_cookies_with_domains(
            state, validate_required=False
        )

    def test_from_storage_state_applies_the_allowlist_filter(self) -> None:
        """Non-auth domains and malformed rows drop, exactly as elsewhere."""
        jar = CookieJar.from_storage_state(_storage_state())
        assert "TRACKER" not in jar.names()
        assert "BROKEN" not in jar.names()

    def test_from_rookiepy_matches_convert_then_extract(self) -> None:
        rows = [
            {
                "name": "SID",
                "value": "sid",
                "domain": ".google.com",
                "path": "/",
                "http_only": True,
                "secure": True,
                "expires": None,
            },
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
                "http_only": True,
                "secure": True,
                "expires": 1800000000,
            },
        ]
        jar = CookieJar.from_rookiepy(rows)
        legacy_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rows)
        assert jar.to_domain_map() == _auth_cookies.extract_cookies_with_domains(
            legacy_state, validate_required=False
        )

    def test_session_cookie_expiry_uses_the_canonical_internal_form(self) -> None:
        """A session cookie is ``expires=None`` *in* the jar, ``-1`` *on the wire*.

        Two representations, deliberately: ``normalize_cookie_expiry`` collapses
        both ``None`` and the exact non-boolean int ``-1`` to ``None`` (see
        ``_auth/cookie_semantics.py``), so ``None`` is the canonical in-memory
        session form the whole auth layer already uses. Playwright's file format
        spells the same thing ``-1``, which is what ``to_storage_state`` emits.
        Pinning both halves keeps a future edit from "simplifying" one into the
        other and silently turning session cookies into dated ones.
        """
        jar = CookieJar.from_rookiepy(
            [{"name": "SID", "value": "s", "domain": ".google.com", "path": "/", "expires": None}]
        )
        assert next(iter(jar)).expires is None
        assert jar.to_storage_state()["cookies"][0]["expires"] == -1

    def test_dated_minus_one_is_not_a_session_cookie(self) -> None:
        """``-1.0`` / ``"-1"`` are dated values, not the session sentinel.

        The distinction is load-bearing enough that ``normalize_cookie_expiry``
        calls it out explicitly; assert the jar inherits it rather than
        flattening every -1-ish input to the sentinel.
        """
        jar = CookieJar.from_storage_state(
            {"cookies": [{"name": "SID", "value": "s", "domain": ".google.com", "expires": -1.0}]}
        )
        assert next(iter(jar)).expires == -1.0
        assert next(iter(jar)).expires is not None

    @pytest.mark.parametrize(
        "shape",
        [
            pytest.param({("SID", ".google.com", "/"): "v"}, id="path-aware-3-tuple"),
            pytest.param({("SID", ".google.com"): "v"}, id="legacy-2-tuple"),
            pytest.param({"SID": "v"}, id="flat"),
        ],
    )
    def test_from_domain_map_widens_every_legacy_shape(self, shape: dict) -> None:
        """All three legacy map shapes widen exactly as normalize_cookie_map does."""
        assert CookieJar.from_domain_map(shape).to_domain_map() == (
            _auth_cookies.normalize_cookie_map(shape)
        )


class TestConverterEquivalence:
    def test_no_flat_map_method_is_exposed(self) -> None:
        """Flattening is legacy-only and must NOT be on the canonical type.

        ``name -> value`` collapses the path component (#369) and picks an
        arbitrary winner among same-tier domains, so the survivor changes when
        storage_state is reordered (#2054) — ``AuthTokens.flat_cookies`` says as
        much in its own docstring. Every remaining caller is back-compat. Pinned
        as a test because "add a convenience to_flat_map()" is the obvious,
        wrong-looking-right change for a future contributor to make.
        """
        assert not hasattr(CookieJar, "to_flat_map")

    def test_to_httpx_matches_build_cookie_jar(self) -> None:
        jar = CookieJar.from_storage_state(_storage_state())
        expected = _auth_cookies.build_cookie_jar(cookies=jar.to_domain_map())
        actual = jar.to_httpx()
        as_identity = lambda j: sorted(  # noqa: E731
            (c.name, c.domain, c.path, c.value) for c in j.jar
        )
        assert as_identity(actual) == as_identity(expected)

    def test_storage_state_round_trip_is_stable(self) -> None:
        """Re-reading a jar's own storage_state yields the same jar.

        Not a claim that the ORIGINAL file round-trips — the allowlist filter
        drops rows on the way in (documented in the module docstring). What
        must hold is idempotence from the filtered view onward, so a jar that
        is written and re-read is unchanged.
        """
        jar = CookieJar.from_storage_state(_storage_state())
        assert CookieJar.from_storage_state(jar.to_storage_state()) == jar

    def test_to_storage_state_preserves_flags_and_expiry(self) -> None:
        jar = CookieJar.from_storage_state(_storage_state())
        row = next(c for c in jar.to_storage_state()["cookies"] if c["name"] == "__Secure-1PSIDTS")
        assert row["expires"] == 1800000000
        assert row["httpOnly"] is True
        assert row["secure"] is True

    def test_to_storage_state_preserves_same_site(self) -> None:
        """A stored ``sameSite`` survives the jar.

        Losing one is the regression ``storage._preserved_same_site`` exists to
        prevent, and ``convert_rookiepy_cookies_to_storage_state`` documents
        preserving it — so a jar that silently dropped it would reintroduce the
        bug through the type meant to become canonical.
        """
        jar = CookieJar.from_storage_state(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "s",
                        "domain": ".google.com",
                        "path": "/",
                        "sameSite": "Lax",
                    }
                ]
            }
        )
        assert next(iter(jar)).same_site == "Lax"
        assert jar.to_storage_state()["cookies"][0]["sameSite"] == "Lax"

    def test_to_storage_state_omits_same_site_when_the_row_had_none(self) -> None:
        """Absent stays absent: the jar must not invent a default.

        ``_preserved_same_site`` treats a missing value as "nothing to keep";
        emitting an invented ``"None"`` here would turn that into a real
        downgrade on the next merge.
        """
        jar = CookieJar.from_storage_state(
            {"cookies": [{"name": "SID", "value": "s", "domain": ".google.com", "path": "/"}]}
        )
        assert next(iter(jar)).same_site is None
        assert "sameSite" not in jar.to_storage_state()["cookies"][0]

    def test_from_rookiepy_matches_the_converters_same_site(self) -> None:
        """The jar route and the legacy converter agree on ``sameSite``."""
        rows = [
            {
                "name": "SID",
                "value": "s",
                "domain": ".google.com",
                "path": "/",
                "sameSite": "Strict",
            }
        ]
        legacy = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rows)
        assert CookieJar.from_rookiepy(rows).to_storage_state() == legacy


class TestQueryEquivalence:
    def test_names_matches_cookie_names_from_storage(self) -> None:
        """The jar's name set equals the legacy reader's — on the FILTERED view.

        ``cookie_names_from_storage`` reads raw rows (no allowlist), so it is
        compared against the jar's own storage_state, which is the filtered
        view both then share.
        """
        jar = CookieJar.from_storage_state(_storage_state())
        assert jar.names() == _cookie_policy.cookie_names_from_storage(jar.to_storage_state())

    def test_has_secondary_binding_matches_policy(self) -> None:
        jar = CookieJar.from_storage_state(_storage_state())
        assert jar.has_secondary_binding() == _cookie_policy._has_valid_secondary_binding(
            jar.names()
        )
        assert jar.has_secondary_binding() is True

    def test_is_rotatable_matches_policy(self) -> None:
        jar = CookieJar.from_storage_state(_storage_state())
        assert jar.is_rotatable() == _cookie_policy._has_rotatable_secondary_binding(jar.names())

    def test_validate_required_passes_on_a_complete_jar(self) -> None:
        CookieJar.from_storage_state(_storage_state()).validate_required()

    def test_validate_required_raises_the_canonical_error(self) -> None:
        """The raise is the policy's own error type, not a lookalike."""
        state = _storage_state()
        state["cookies"] = [c for c in state["cookies"] if c["name"] != "__Secure-1PSIDTS"]
        jar = CookieJar.from_storage_state(state)
        with pytest.raises(_cookie_policy.RequiredCookieValidationError) as excinfo:
            jar.validate_required()
        assert "__Secure-1PSIDTS" in str(excinfo.value)

    def test_missing_hint_matches_policy(self) -> None:
        state = _storage_state()
        state["cookies"] = [c for c in state["cookies"] if c["name"] != "SID"]
        jar = CookieJar.from_storage_state(state)
        assert jar.missing_hint(browser_label="chrome") == _cookie_policy.missing_cookies_hint(
            jar.names(), browser_label="chrome"
        )


class TestContainerAndSafety:
    def test_empty_jar_is_falsy_and_empty(self) -> None:
        jar = CookieJar()
        assert not jar
        assert len(jar) == 0
        assert jar.names() == set()

    def test_iteration_yields_cookie_objects(self) -> None:
        jar = CookieJar.from_storage_state(_storage_state())
        assert all(isinstance(c, Cookie) for c in jar)
        assert len(jar) == len(jar.to_storage_state()["cookies"])

    def test_cookie_identity_is_rfc6265_triple(self) -> None:
        c = Cookie(name="SID", domain=".google.com", path="/x", value="v")
        assert c.identity == ("SID", ".google.com", "/x")

    def test_same_name_different_path_are_independent(self) -> None:
        """#369: a path-distinct twin must not shadow its sibling."""
        jar = CookieJar.from_storage_state(
            {
                "cookies": [
                    {"name": "SID", "value": "root", "domain": ".google.com", "path": "/"},
                    {"name": "SID", "value": "scoped", "domain": ".google.com", "path": "/app"},
                ]
            }
        )
        assert len(jar.to_domain_map()) == 2

    def test_repr_redacts_cookie_values(self) -> None:
        """Values are credential-equivalent and must never reach logs/diffs."""
        jar = CookieJar.from_storage_state(_storage_state())
        rendered = repr(jar)
        assert "sid-base" not in rendered
        assert "psidts" not in rendered
        assert "SID" in rendered  # names are safe and useful

    def test_jar_is_immutable_from_the_outside(self) -> None:
        """Mutating a derived map must not write through to the jar."""
        jar = CookieJar.from_storage_state(_storage_state())
        before = jar.names()
        jar.to_domain_map()[("INJECTED", "x", "/")] = "v"
        assert jar.names() == before


class TestFromHttpx:
    """ADR-0032 Phase A: snapshot a live httpx jar for read-only questions."""

    @staticmethod
    def _httpx_jar(**cookies: str) -> httpx.Cookies:
        jar = httpx.Cookies()
        for name, value in cookies.items():
            jar.set(name, value, domain=".google.com", path="/")
        return jar

    def test_from_httpx_preserves_names_and_values(self) -> None:
        jar = CookieJar.from_httpx(self._httpx_jar(SID="s", HSID="h"))
        assert jar.names() == {"SID", "HSID"}
        assert jar.to_domain_map()[("SID", ".google.com", "/")] == "s"

    def test_from_httpx_preserves_expires_and_secure(self) -> None:
        h = httpx.Cookies()
        h.set("SID", "s", domain=".google.com", path="/")
        for c in h.jar:
            c.expires, c.secure = 1900000000, True
        cookie = next(iter(CookieJar.from_httpx(h)))
        assert cookie.expires == 1900000000
        assert cookie.secure is True

    def test_from_httpx_loses_same_site_by_construction(self) -> None:
        """http.cookiejar can't carry SameSite — every Cookie gets None."""
        cookie = next(iter(CookieJar.from_httpx(self._httpx_jar(SID="s"))))
        assert cookie.same_site is None

    def test_from_httpx_answers_binding_questions(self) -> None:
        jar = CookieJar.from_httpx(self._httpx_jar(SID="s", APISID="a", SAPISID="sa", LSID="l"))
        assert jar.has_secondary_binding() is True

    def test_from_httpx_excludes_non_auth_domain_cookies(self) -> None:
        """A non-auth-domain cookie with an auth name must not leak in.

        ``from_httpx`` applies the same allowed-auth-domain filter as
        ``_cookie_map_from_jar`` (which builds ``AuthTokens.cookies``). Without
        it, an ``OSID`` on an unrelated domain would make ``.jar`` report a
        secondary binding the filtered ``cookies`` view correctly excludes.
        """
        jar = httpx.Cookies()
        jar.set("OSID", "impostor", domain=".evil.example", path="/")
        result = CookieJar.from_httpx(jar)
        assert "OSID" not in result.names()
        assert result.has_secondary_binding() is False
        # A legitimate auth-domain cookie in the same jar still projects through.
        jar.set("SID", "real", domain=".google.com", path="/")
        mixed = CookieJar.from_httpx(jar)
        assert mixed.names() == {"SID"}


class TestCookieIdentityAndEquality:
    def test_typed_identity_is_tuple_shaped_with_default_path(self) -> None:
        key = CookieIdentity("SID", ".google.com")
        name, domain, path = key

        assert type(key) is CookieIdentity
        assert isinstance(key, tuple)
        assert key == ("SID", ".google.com", "/")
        assert (name, domain, path) == (key[0], key[1], key[2])

    def test_legacy_identity_stays_an_exact_plain_tuple(self) -> None:
        cookie = Cookie("SID", ".google.com", "/app", "secret")

        assert type(cookie.identity) is tuple
        assert cookie.identity == ("SID", ".google.com", "/app")
        assert cookie.key == CookieIdentity("SID", ".google.com", "/app")

    @pytest.mark.parametrize(
        ("field_name", "replacement"),
        [
            ("name", "HSID"),
            ("domain", "accounts.google.com"),
            ("path", "/other"),
            ("value", "other-value"),
            ("expires", 1_900_000_000),
            ("http_only", True),
            ("secure", True),
        ],
    )
    def test_equality_and_hash_compare_every_field_except_same_site(
        self, field_name: str, replacement: object
    ) -> None:
        original = Cookie("SID", ".google.com", "/", "value")
        changed = replace(original, **{field_name: replacement})

        assert changed != original
        assert len({original, changed}) == 2

    def test_same_site_is_not_compared_or_hashed_but_serializes_independently(self) -> None:
        lax = Cookie("SID", ".google.com", "/", "value", same_site="Lax")
        strict = replace(lax, same_site="Strict")

        assert lax == strict
        assert hash(lax) == hash(strict)
        assert CookieJar([lax]).to_storage_rows()[0]["sameSite"] == "Lax"
        assert CookieJar([strict]).to_storage_rows()[0]["sameSite"] == "Strict"

    def test_cookie_and_jar_representations_redact_value_and_same_site(self) -> None:
        value_secret = "sentinel-cookie-value"
        same_site_secret = "sentinel-samesite-text"
        cookie = Cookie(
            "SID",
            ".google.com",
            "/",
            value_secret,
            same_site=same_site_secret,
        )
        jar = CookieJar([cookie])

        for rendered in (repr(cookie), str(cookie), repr(jar), str(jar)):
            assert value_secret not in rendered
            assert same_site_secret not in rendered

        with pytest.raises(AssertionError) as caught:
            assert cookie == replace(cookie, value="different")
        failure = str(caught.value)
        assert value_secret not in failure
        assert same_site_secret not in failure

        with pytest.raises(AssertionError) as jar_caught:
            assert jar == CookieJar([replace(cookie, value="different")])
        jar_failure = str(jar_caught.value)
        assert value_secret not in jar_failure
        assert same_site_secret not in jar_failure


class TestOrderedImmutableJar:
    @staticmethod
    def _cookies() -> list[Cookie]:
        return [Cookie("SID", ".google.com", "/", "first")]

    def test_constructor_copies_mutable_input(self) -> None:
        source = self._cookies()
        jar = CookieJar(source)

        source[0] = replace(source[0], value="replaced")
        source.append(Cookie("HSID", ".google.com", "/", "added"))

        assert len(jar) == 1
        assert next(iter(jar)).value == "first"

    def test_internal_tuple_cannot_be_rebound_or_deleted(self) -> None:
        jar = CookieJar(self._cookies())

        with pytest.raises(FrozenInstanceError):
            jar._cookies = ()
        with pytest.raises(FrozenInstanceError):
            del jar._cookies

    def test_ordered_rows_and_duplicate_projections_have_named_winners(self) -> None:
        cookies = [
            Cookie("SID", ".google.com", "/", "first"),
            Cookie("SID", ".google.com", "/", "last"),
            Cookie("SID", ".google.com", "/app", "scoped"),
            Cookie("SID", "google.com", "/", "bare"),
        ]
        jar = CookieJar(cookies)

        assert [row["value"] for row in jar.to_storage_rows()] == [
            "first",
            "last",
            "scoped",
            "bare",
        ]
        expected_map = {
            ("SID", ".google.com", "/"): "first",
            ("SID", ".google.com", "/app"): "scoped",
            ("SID", "google.com", "/"): "bare",
        }
        assert jar.domain_map_first_wins() == expected_map
        assert jar.to_domain_map() == expected_map
        assert all(type(key) is tuple for key in jar.to_domain_map())

        live = jar.to_httpx()
        assert [(c.value, c.domain, c.path) for c in live.jar] == [
            ("last", ".google.com", "/"),
            ("scoped", ".google.com", "/app"),
            ("bare", "google.com", "/"),
        ]

    def test_ambiguous_generic_projections_are_absent(self) -> None:
        assert not hasattr(CookieJar, "to_flat_map")
        assert not hasattr(CookieJar, "by_identity")


class TestFaithfulHttpxCodec:
    def test_legacy_storage_adapter_preserves_duck_secure_value_and_type(self) -> None:
        marker = "sentinel-non-bool-secure"

        class DuckCookie:
            name = "SID"
            value = "value"
            domain = ".google.com"
            path = "/"
            expires = None
            secure = marker

            @staticmethod
            def has_nonstandard_attr(_name: str) -> bool:
                return False

        row = _auth_cookies._cookie_to_storage_state(DuckCookie())

        assert type(row["secure"]) is str
        assert row["secure"] == marker

    def test_full_chain_preserves_session_vs_dated_minus_one_and_attributes(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "SESSION",
                    "value": "session-value",
                    "domain": ".google.com",
                    "path": "/session",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                },
                {
                    "name": "DATED",
                    "value": "dated-value",
                    "domain": "accounts.google.com",
                    "path": "/dated",
                    "expires": -1.0,
                    "httpOnly": True,
                    "secure": True,
                },
            ]
        }
        typed = CookieJar.from_storage_state(state)
        typed_by_name = {cookie.name: cookie for cookie in typed}

        assert typed_by_name["SESSION"].expires is None
        assert type(typed_by_name["DATED"].expires) is float
        assert typed_by_name["DATED"].expires == -1.0

        live_by_name = {cookie.name: cookie for cookie in typed.to_httpx().jar}
        session = live_by_name["SESSION"]
        dated = live_by_name["DATED"]
        assert (session.expires, session.discard) == (None, True)
        assert (type(dated.expires), dated.expires, dated.discard) == (int, -1, False)
        assert (session.path, session.secure, session.domain_initial_dot) == (
            "/session",
            True,
            True,
        )
        assert (dated.path, dated.secure, dated.domain_initial_dot) == (
            "/dated",
            True,
            False,
        )
        assert session.has_nonstandard_attr("HttpOnly")
        assert dated.has_nonstandard_attr("HttpOnly")

        session_row = _auth_cookies._cookie_to_storage_state(session)
        dated_row = _auth_cookies._cookie_to_storage_state(dated)
        assert (type(session_row["expires"]), session_row["expires"]) == (int, -1)
        assert (type(dated_row["expires"]), dated_row["expires"]) == (float, -1.0)

    def test_from_httpx_is_explicitly_same_site_lossy(self) -> None:
        source = CookieJar([Cookie("SID", ".google.com", "/", "value", same_site="Strict")])

        projected = CookieJar.from_httpx(source.to_httpx())

        assert next(iter(projected)).same_site is None
        assert "sameSite" not in projected.to_storage_rows()[0]


class TestIntentionalProjectionLoss:
    def test_storage_projection_drops_document_and_unknown_row_fields(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "SID",
                    "value": "value",
                    "domain": ".google.com",
                    "path": "/",
                    "futureCookieField": {"preserve": "only-in-document"},
                },
                {"malformed": "row"},
            ],
            "origins": [{"origin": "https://example.test"}],
            "futureTopLevel": {"preserve": "only-in-document"},
        }

        projected = CookieJar.from_storage_state(state).to_storage_state()

        assert projected["origins"] == []
        assert "futureTopLevel" not in projected
        assert len(projected["cookies"]) == 1
        assert "futureCookieField" not in projected["cookies"][0]

    def test_storage_and_rookiepy_inputs_are_not_mutated_or_retained(self) -> None:
        state = {"cookies": [{"name": "SID", "value": "state-original", "domain": ".google.com"}]}
        rookie_rows = [{"name": "SID", "value": "rookie-original", "domain": ".google.com"}]
        state_jar = CookieJar.from_storage_state(state)
        rookie_jar = CookieJar.from_rookiepy(rookie_rows)

        state["cookies"][0]["value"] = "state-mutated"
        rookie_rows[0]["value"] = "rookie-mutated"

        assert next(iter(state_jar)).value == "state-original"
        assert next(iter(rookie_jar)).value == "rookie-original"
