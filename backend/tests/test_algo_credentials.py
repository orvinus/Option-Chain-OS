"""Changing the Algo Config / Backtesting sign-in ID and password.

The point of this feature is that the DATABASE is the source of truth, not the
environment file. Before 2026-09-13 both the login route and the route guard
refused everything unless ALGO_ADMIN_USER / ALGO_ADMIN_PASSWORD were still set,
so the secret could never leave disk even though a hashed user row already
existed and already survived restarts.
"""
from __future__ import annotations

import pytest

from app.algo import auth as auth_mod


# ----------------------------------------------------------- the env gate


async def test_auth_is_configured_when_a_user_row_exists_even_with_no_env(monkeypatch):
    """The exact case the old gate got wrong: credentials changed in the
    browser, .env then emptied, backend restarted."""
    monkeypatch.setattr(auth_mod.settings, "algo_admin_user", "")
    monkeypatch.setattr(auth_mod.settings, "algo_admin_password", "")

    async def one_user():
        return 1

    monkeypatch.setattr(auth_mod, "user_count", one_user)
    assert await auth_mod.auth_configured() is True


async def test_auth_is_configured_from_env_when_the_table_is_empty(monkeypatch):
    """First run: nothing in the database yet, the env still bootstraps."""
    monkeypatch.setattr(auth_mod.settings, "algo_admin_user", "u")
    monkeypatch.setattr(auth_mod.settings, "algo_admin_password", "p")

    async def no_users():
        return 0

    monkeypatch.setattr(auth_mod, "user_count", no_users)
    assert await auth_mod.auth_configured() is True


async def test_auth_is_not_configured_with_neither(monkeypatch):
    """No row and no env is a real misconfiguration and must stay loud."""
    monkeypatch.setattr(auth_mod.settings, "algo_admin_user", "")
    monkeypatch.setattr(auth_mod.settings, "algo_admin_password", "")

    async def no_users():
        return 0

    monkeypatch.setattr(auth_mod, "user_count", no_users)
    assert await auth_mod.auth_configured() is False


def test_the_env_is_only_ever_a_bootstrap():
    """``ensure_admin_seeded`` must never overwrite an existing row, or every
    restart would silently revert a changed password."""
    import inspect

    src = inspect.getsource(auth_mod.ensure_admin_seeded)
    assert "if n and int(n) > 0" in src and "return" in src


def test_no_access_gate_still_tests_the_env_vars_directly():
    """Regression guard: gating ACCESS on the env is what pinned the secret to
    disk. ``ensure_admin_seeded`` may still read the env — that is the
    bootstrap, and it is the one legitimate reader."""
    import inspect

    from app.api.algo_auth import algo_login

    for fn in (auth_mod.require_admin, algo_login):
        src = inspect.getsource(fn)
        assert "settings.algo_admin_user" not in src, (
            f"{fn.__name__} still gates access on the env vars"
        )
        assert "auth_configured" in src, f"{fn.__name__} must ask the database"


# ------------------------------------------------------- validation rules


def _no_auth(monkeypatch, ident=None):
    async def fake(username, password):
        return ident

    monkeypatch.setattr(auth_mod, "authenticate", fake)


async def test_blank_new_id_is_refused(monkeypatch):
    _no_auth(monkeypatch)
    with pytest.raises(auth_mod.CredentialError, match="cannot be blank"):
        await auth_mod.change_credentials(
            user_id=1, current_username="a", current_password="b",
            new_username="   ", new_password="longenough1",
        )


async def test_short_new_password_is_refused(monkeypatch):
    _no_auth(monkeypatch)
    with pytest.raises(auth_mod.CredentialError, match="at least 8"):
        await auth_mod.change_credentials(
            user_id=1, current_username="a", current_password="b",
            new_username="newid", new_password="short",
        )


async def test_wrong_current_password_is_refused(monkeypatch):
    _no_auth(monkeypatch, ident=None)
    with pytest.raises(auth_mod.CredentialError, match="not correct"):
        await auth_mod.change_credentials(
            user_id=1, current_username="a", current_password="wrong",
            new_username="newid", new_password="longenough1",
        )


async def test_a_session_cannot_rewrite_a_different_account(monkeypatch):
    """Right password, wrong account. The row is chosen by the SESSION's
    user_id, so this must refuse rather than change someone else."""
    _no_auth(monkeypatch, ident=auth_mod.AdminIdentity(user_id=99, username="other", role="admin"))
    with pytest.raises(auth_mod.CredentialError, match="not correct"):
        await auth_mod.change_credentials(
            user_id=1, current_username="other", current_password="right",
            new_username="newid", new_password="longenough1",
        )


async def test_refusals_are_worded_identically(monkeypatch):
    """A wrong password and a valid-password-wrong-account must be
    indistinguishable, or the response probes which IDs exist."""
    _no_auth(monkeypatch, ident=None)
    with pytest.raises(auth_mod.CredentialError) as a:
        await auth_mod.change_credentials(
            user_id=1, current_username="a", current_password="x",
            new_username="n", new_password="longenough1",
        )
    _no_auth(monkeypatch, ident=auth_mod.AdminIdentity(user_id=7, username="b", role="admin"))
    with pytest.raises(auth_mod.CredentialError) as b:
        await auth_mod.change_credentials(
            user_id=1, current_username="b", current_password="x",
            new_username="n", new_password="longenough1",
        )
    assert str(a.value) == str(b.value)


# --------------------------------------------------------------- hashing


def test_a_changed_password_verifies_and_the_old_one_does_not():
    new_hash = auth_mod.hash_password("brand-new-secret")
    assert auth_mod.verify_password("brand-new-secret", new_hash)
    assert not auth_mod.verify_password("the-old-one", new_hash)


def test_hashes_are_salted_so_two_equal_passwords_differ():
    assert auth_mod.hash_password("same") != auth_mod.hash_password("same")


# ----------------------------------------------------------- the endpoint


def test_endpoint_is_registered_and_requires_a_session():
    import inspect

    from app.api import algo_auth as api_mod

    src = inspect.getsource(api_mod.algo_change_credentials)
    assert "require_admin" in inspect.getsource(api_mod)
    # The session is cleared so the new credentials are proven immediately.
    assert "delete_cookie" in src
    assert "credentials_changed" in src
    paths = {r.path for r in api_mod.router.routes}
    assert "/algo/auth/change-credentials" in paths


def test_the_request_model_never_carries_a_confirm_field():
    """Confirmations are a browser concern; sending them twice would put the
    password on the wire more than once for no benefit."""
    from app.api.algo_auth import ChangeCredentialsRequest

    assert set(ChangeCredentialsRequest.model_fields) == {
        "current_username", "current_password", "new_username", "new_password",
    }


def test_repeated_failures_are_throttled():
    from app.api import algo_auth as api_mod

    api_mod._change_fails.clear()
    uid = 4242
    for _ in range(api_mod._FAIL_LIMIT):
        api_mod._throttle_check(uid)
        api_mod._change_fails.setdefault(uid, []).append(__import__("time").time())
    with pytest.raises(Exception) as e:
        api_mod._throttle_check(uid)
    assert "429" in str(e.value) or "Too many" in str(e.value)
    api_mod._change_fails.clear()
