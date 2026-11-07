"""Backward clock steps, and refresh responses that do not parse cleanly.

Both classes of failure are silent and expensive: a clock that steps back
makes a stale reading look fresh forever, and a rotated refresh token lost to
a parse error ends the account's login for good (the server has already
invalidated the token we sent).
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import patch

from claude_swap import oauth
from claude_swap.usage_store import STALE_OK_S, FetchRecord, UsageStore


def _credentials(refresh: str = "rt-old", access: str = "at-old") -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": 1_700_000_000_000,
            "scopes": ["user:inference"],
        }
    })


def _response(payload: dict):
    """A urlopen context manager answering with ``payload`` as JSON."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp(json.dumps(payload).encode())


class TestRefreshKeepsTheRotatedToken:
    def test_response_without_expires_in_still_adopts_new_refresh_token(self):
        # The server rotated the token (200 OK) but left `expires_in` out.
        # Reading that field first used to raise, discarding the successor and
        # leaving the dead token stored -> invalid_grant on the next refresh.
        payload = {"access_token": "at-new", "refresh_token": "rt-new"}
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response(payload),
        ):
            outcome = oauth.try_refresh_oauth_credentials(_credentials())

        assert outcome.error is None
        assert outcome.credentials is not None
        stored = json.loads(outcome.credentials)["claudeAiOauth"]
        assert stored["refreshToken"] == "rt-new"
        assert stored["accessToken"] == "at-new"
        # Falls back to an hour rather than inventing an expiry in the past,
        # which would make every caller treat the fresh token as expired.
        assert stored["expiresAt"] > time.time() * 1000

    def test_non_numeric_expires_in_does_not_lose_the_successor(self):
        payload = {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": "3600",  # a string, as some gateways send
        }
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response(payload),
        ):
            outcome = oauth.try_refresh_oauth_credentials(_credentials())

        assert outcome.error is None
        stored = json.loads(outcome.credentials)["claudeAiOauth"]
        assert stored["refreshToken"] == "rt-new"

    def test_normal_response_still_uses_the_server_expiry(self):
        payload = {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 60,
        }
        # Floored: the code builds the stamp with int(), so an unfloored
        # float lower bound loses to sub-millisecond truncation.
        before_ms = int(time.time() * 1000)
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response(payload),
        ):
            outcome = oauth.try_refresh_oauth_credentials(_credentials())

        stored = json.loads(outcome.credentials)["claudeAiOauth"]
        assert before_ms + 60_000 <= stored["expiresAt"] <= before_ms + 70_000


class TestBackwardClockStep:
    def test_future_fetched_at_is_not_trusted_for_switch_decisions(
        self, tmp_path
    ):
        # Written before the clock stepped back, so the row is stamped in the
        # future. A negative age used to read as fresh, so auto-switch kept
        # acting on an old reading and nothing ever refetched.
        identities = {"1": ("a@example.com", "")}
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}

        now = time.time()
        store = UsageStore(tmp_path / "cache", clock=lambda: now)
        store.record({"1": FetchRecord(usage=usage)}, identities)
        assert store.entries(identities)["1"].decision_value() == usage

        stepped_back = now - 6 * 3600
        after = UsageStore(tmp_path / "cache", clock=lambda: stepped_back)
        entry = after.entries(identities)["1"]
        assert entry.age_s is None
        assert entry.decision_value() is None

    def test_a_normal_recent_reading_is_still_trusted(self, tmp_path):
        identities = {"1": ("a@example.com", "")}
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}

        now = time.time()
        store = UsageStore(tmp_path / "cache", clock=lambda: now)
        store.record({"1": FetchRecord(usage=usage)}, identities)

        later = UsageStore(
            tmp_path / "cache", clock=lambda: now + STALE_OK_S / 2
        )
        assert later.entries(identities)["1"].decision_value() == usage
