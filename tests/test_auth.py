"""Tests for `hmm_client.auth` — GUI Basic Auth + bcrypt."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from hmm_client import auth


@pytest.fixture
def fresh_env(monkeypatch):
    """Strip auth-related env vars so each test starts clean."""
    monkeypatch.delenv("VESSEL_GUI_USER", raising=False)
    monkeypatch.delenv("VESSEL_GUI_PASSWORD_HASH", raising=False)


@pytest.mark.unit
class TestHashAndVerify:
    def test_hash_returns_bcrypt_format(self):
        h = auth.hash_password("correct-horse-battery")
        assert h.startswith("$2b$") or h.startswith("$2a$")
        # bcrypt hashes are 60 chars
        assert len(h) == 60

    def test_verify_round_trip(self):
        h = auth.hash_password("correct-horse-battery").encode()
        assert auth.verify_password("correct-horse-battery", h) is True

    def test_verify_wrong_password(self):
        h = auth.hash_password("correct-horse-battery").encode()
        assert auth.verify_password("incorrect-horse-battery", h) is False

    def test_verify_against_garbage_hash_returns_false(self):
        # Should NOT raise — operators may misconfigure
        assert auth.verify_password("anything", b"not-a-bcrypt-hash") is False
        assert auth.verify_password("anything", b"") is False


@pytest.mark.unit
class TestIsEnabled:
    def test_disabled_when_env_unset(self, fresh_env):
        assert auth.is_enabled() is False

    def test_disabled_when_env_empty(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", "")
        assert auth.is_enabled() is False

    def test_disabled_when_hash_not_bcrypt(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", "plaintext-not-a-hash")
        assert auth.is_enabled() is False

    def test_enabled_with_valid_hash(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("x"))
        assert auth.is_enabled() is True


@pytest.mark.unit
class TestRequireAuth:
    def test_returns_anonymous_when_disabled(self, fresh_env):
        assert auth.require_auth(creds=None) == "anonymous"

    def test_401_when_enabled_and_no_creds(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("p"))
        with pytest.raises(HTTPException) as excinfo:
            auth.require_auth(creds=None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.headers["WWW-Authenticate"] == "Basic"

    def test_401_when_wrong_password(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("rightpw"))
        creds = HTTPBasicCredentials(username="admin", password="wrongpw")
        with pytest.raises(HTTPException) as excinfo:
            auth.require_auth(creds=creds)
        assert excinfo.value.status_code == 401

    def test_401_when_wrong_username(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("pw"))
        # Default user is "admin"
        creds = HTTPBasicCredentials(username="not-admin", password="pw")
        with pytest.raises(HTTPException):
            auth.require_auth(creds=creds)

    def test_returns_username_on_success(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("pw"))
        creds = HTTPBasicCredentials(username="admin", password="pw")
        assert auth.require_auth(creds=creds) == "admin"

    def test_custom_username_via_env(self, fresh_env, monkeypatch):
        monkeypatch.setenv("VESSEL_GUI_USER", "operator")
        monkeypatch.setenv("VESSEL_GUI_PASSWORD_HASH", auth.hash_password("pw"))
        creds = HTTPBasicCredentials(username="operator", password="pw")
        assert auth.require_auth(creds=creds) == "operator"
        # Default "admin" should be rejected when custom user set
        with pytest.raises(HTTPException):
            auth.require_auth(creds=HTTPBasicCredentials(username="admin", password="pw"))
