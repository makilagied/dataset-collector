import hashlib
import hmac

from cryptography.fernet import Fernet

from collector.config import get_settings


def encrypt_token(token: str, key: str | None = None) -> str:
    key = key or get_settings().token_enc_key
    return Fernet(key.encode()).encrypt(token.encode()).decode()


def decrypt_token(token_enc: str, key: str | None = None) -> str:
    key = key or get_settings().token_enc_key
    return Fernet(key.encode()).decrypt(token_enc.encode()).decode()


def hash_author(username_or_id: str, key: str | None = None) -> str:
    """Deterministic HMAC-SHA256 (hex) of a commenter identifier."""
    key = key or get_settings().author_hash_key
    return hmac.new(key.encode(), username_or_id.encode(), hashlib.sha256).hexdigest()
