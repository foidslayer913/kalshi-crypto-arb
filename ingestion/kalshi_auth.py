from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def load_private_key(pem_bytes: bytes) -> RSAPrivateKey:
    key = load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("Kalshi requires an RSA private key")
    return key


def sign_request(private_key: RSAPrivateKey, method: str, path: str, timestamp_ms: str | None = None) -> tuple[str, str]:
    """Sign a Kalshi REST/WS request per Kalshi's RSASSA-PSS auth scheme.

    Returns (timestamp_ms, base64_signature) for use in the KALSHI-ACCESS-* headers.
    """
    timestamp_ms = timestamp_ms or str(int(time.time() * 1000))
    message = f"{timestamp_ms}{method}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return timestamp_ms, base64.b64encode(signature).decode("utf-8")


def auth_headers(private_key: RSAPrivateKey, api_key_id: str, method: str, path: str) -> dict[str, str]:
    timestamp_ms, signature = sign_request(private_key, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }
