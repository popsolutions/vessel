"""AES-128-CBC NoPadding wrappers, mirroring com/kvm/AESHandler.java.

The VirtualMedia stream uses a single AES-128-CBC key per session, derived
from the HMM Web embed parameters:

    secretkey       40 hex chars (20 bytes):
                      [0:8]  -> verifyvalue_int (big-endian, 4 bytes)
                      [8:40] -> AES-128 key for KVM stream (16 bytes)
    secretiv        32 hex chars (16 bytes) -> AES IV (also reused as salt)
    codekey_ext     32 hex chars (16 bytes) -> AES-128 key for VirtualMedia

NoPadding means callers must pre-pad to a multiple of 16 bytes themselves
(matching the Java side's manual round-up in AESHandler.encry).
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 16


def _check(name: str, value: bytes, expected: int) -> None:
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes, got {len(value)}")


def aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC encrypt with NoPadding. plaintext length must be % 16 == 0."""
    _check("key", key, 16)
    _check("iv", iv, 16)
    if len(plaintext) % BLOCK_SIZE != 0:
        raise ValueError(
            f"plaintext length {len(plaintext)} not a multiple of {BLOCK_SIZE} "
            "(NoPadding); pad with zeros before calling"
        )
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    _check("key", key, 16)
    _check("iv", iv, 16)
    if len(ciphertext) % BLOCK_SIZE != 0:
        raise ValueError(
            f"ciphertext length {len(ciphertext)} not a multiple of {BLOCK_SIZE}"
        )
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def parse_secretkey(hex_str: str) -> tuple[int, bytes]:
    """Split the embed's `secretkey` (40 hex chars) into (verifyvalue_int, aes_key_16).

    >>> v, k = parse_secretkey("00112233" + "0011223344556677" + "8899aabbccddeeff")
    >>> v
    1122867
    >>> len(k)
    16
    """
    s = hex_str.strip()
    if len(s) < 40:
        raise ValueError(f"secretkey must be >= 40 hex chars, got {len(s)}")
    raw = bytes.fromhex(s[:40])
    verifyvalue = int.from_bytes(raw[:4], "big")
    aes_key = raw[4:20]
    return verifyvalue, aes_key


def parse_secretiv(hex_str: str) -> bytes:
    """Parse the embed's `secretiv` (32 hex chars) into a 16-byte IV.

    >>> parse_secretiv("00112233445566778899aabbccddeeff").hex()
    '00112233445566778899aabbccddeeff'
    """
    s = hex_str.strip()
    if len(s) < 32:
        raise ValueError(f"secretiv must be >= 32 hex chars, got {len(s)}")
    return bytes.fromhex(s[:32])


def parse_codekey_ext(hex_str: str) -> bytes:
    """Parse the embed's `codekey_ext` (32 hex chars) into a 16-byte AES key."""
    return parse_secretiv(hex_str)  # same shape: 16 bytes from 32 hex
