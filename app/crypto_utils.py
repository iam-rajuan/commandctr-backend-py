import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings


def _key() -> bytes:
    return hashlib.sha256(get_settings().ENCRYPTION_SECRET.encode()).digest()


def encrypt(value: str) -> str:
    iv = os.urandom(12)
    encrypted = AESGCM(_key()).encrypt(iv, value.encode(), None)
    ciphertext, tag = encrypted[:-16], encrypted[-16:]
    return f"{iv.hex()}:{tag.hex()}:{ciphertext.hex()}"


def decrypt(value: str) -> str:
    iv_hex, tag_hex, encrypted_hex = value.split(":")
    payload = bytes.fromhex(encrypted_hex) + bytes.fromhex(tag_hex)
    return AESGCM(_key()).decrypt(bytes.fromhex(iv_hex), payload, None).decode()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
