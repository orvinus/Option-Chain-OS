"""Algo admin auth tests — password hashing and session tokens.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_auth.py

Pure crypto-path tests with injected clocks — no database, no network.
"""
from __future__ import annotations

from app.algo.auth import (
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from app.core.config import settings


def test_password_hash_round_trip():
    stored = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)


def test_password_hash_is_salted():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b, "two hashes of the same password must differ (random salt)"
    assert verify_password("same-password", a) and verify_password("same-password", b)


def test_tampered_hash_rejected_not_crashed():
    stored = hash_password("pw")
    assert not verify_password("pw", stored.replace("pbkdf2_sha256", "md5"))
    assert not verify_password("pw", "complete$garbage")
    assert not verify_password("pw", "")


def test_token_round_trip():
    tok = issue_token(7, now=1_000_000.0)
    assert verify_token(tok, now=1_000_000.0 + 60) == 7


def test_token_expires():
    tok = issue_token(7, now=1_000_000.0)
    ttl_s = settings.algo_session_ttl_h * 3600
    assert verify_token(tok, now=1_000_000.0 + ttl_s + 1) is None, (
        "a token past its TTL must be rejected"
    )


def test_tampered_token_rejected():
    tok = issue_token(7, now=1_000_000.0)
    user_part, expires_part, sig = tok.split(".")
    forged = f"8.{expires_part}.{sig}"
    assert verify_token(forged, now=1_000_000.0 + 60) is None, (
        "changing the user id must invalidate the signature"
    )
    extended = f"{user_part}.{int(expires_part) + 999999}.{sig}"
    assert verify_token(extended, now=1_000_000.0 + 60) is None, (
        "extending the expiry must invalidate the signature"
    )
    assert verify_token("junk", now=1_000_000.0) is None
    assert verify_token("", now=1_000_000.0) is None


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all algo auth tests passed")


if __name__ == "__main__":
    _run_all()
