"""The OAuth callback must prove it came from the request this client started.

charon_auth previously set `state = verifier` and never compared the returned
state to the issued one, so any callback was accepted. Two defects in one line:
no CSRF protection on the callback, and the PKCE verifier — the value PKCE
depends on keeping private — was published in the redirect URL.
"""

import secrets

import pytest

from charon.providers.charon_auth import _pkce_pair, _verify_state


def test_matching_state_is_accepted():
    issued = secrets.token_urlsafe(32)
    _verify_state(issued, issued)  # must not raise


def test_mismatched_state_is_refused():
    with pytest.raises(RuntimeError, match='state mismatch'):
        _verify_state(secrets.token_urlsafe(32), secrets.token_urlsafe(32))


def test_absent_state_is_refused_not_assumed_valid():
    for returned in (None, ''):
        with pytest.raises(RuntimeError, match='no state parameter'):
            _verify_state(secrets.token_urlsafe(32), returned)


def test_state_is_not_the_pkce_verifier():
    """Regression: state used to be the verifier itself."""
    verifier, _challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    assert state != verifier
    with pytest.raises(RuntimeError):
        # Knowing the state must not imply knowing the verifier, and an
        # attacker replaying one in place of the other must be refused.
        _verify_state(state, verifier)
