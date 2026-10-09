import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from collector.config import Settings
from collector.security import decrypt_token, encrypt_token, hash_author


def test_token_round_trip() -> None:
    enc = encrypt_token("IGQVJ-secret-token")
    assert enc != "IGQVJ-secret-token"
    assert decrypt_token(enc) == "IGQVJ-secret-token"


def test_wrong_key_fails() -> None:
    enc = encrypt_token("tok")
    with pytest.raises(InvalidToken):
        decrypt_token(enc, key=Fernet.generate_key().decode())


def test_hash_is_deterministic_and_keyed() -> None:
    a = hash_author("some_user")
    assert a == hash_author("some_user")
    assert a != hash_author("other_user")
    assert a != hash_author("some_user", key="different-key")
    assert len(a) == 64 and "some_user" not in a


def test_no_secret_has_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL",
        "IG_APP_ID",
        "IG_APP_SECRET",
        "IG_REDIRECT_URI",
        "IG_WEBHOOK_VERIFY_TOKEN",
        "TOKEN_ENC_KEY",
        "AUTHOR_HASH_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
