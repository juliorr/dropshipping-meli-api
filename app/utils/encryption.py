"""Fernet encryption for MeLi OAuth tokens at rest.

Encryption is optional: if TOKEN_ENCRYPTION_KEY is not set, tokens are stored
as plaintext (development default). In production, set the env var to enable.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None and settings.token_encryption_key:
        _fernet = Fernet(settings.token_encryption_key.encode())
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt token if key is configured, otherwise return plaintext."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt token. Falls back to plaintext if decryption fails or no key."""
    f = _get_fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ciphertext  # Pre-encryption plaintext token
