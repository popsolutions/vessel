"""Port of `com.kvm.AESHandler` — AES-128-CBC NoPadding + PBKDF2.

Java reference (`re/decompiled/sources/com/kvm/AESHandler.java`):
- `encry(src, codekey_int, len)`           — Windows-side encryption
- `encry_bytes(src, kbdKey, kbdIV, len)`   — Linux-side encryption
- `aes_cbc_128_encrypt(data, key, iv)`     — straight AES-CBC NoPadding
- `aes_cbc_128_decrypt(data, key, iv)`
- `generateStoredPasswordHash(plain, passLen, salt, hmac, iter)` — PBKDF2

The two `encry_*` helpers pad the input with zeros to the next 16-byte
boundary (manual padding because the Java cipher is configured with
`AES/CBC/NOPadding`), encrypt, and return the ciphertext. Output length
is always a multiple of 16.
"""
from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Java constants (`AESHandler.java` lines 16-19)
DEFAULT_IV: bytes = bytes(16)
DEFAULT_SALT: bytes = bytes(16)
# `static final byte[] encrypt = {1,2,3,4,5,6,7,8,1,2,3,4,5,6,7,8};`
# Java mutates encrypt[0..3] each call to embed the codekey int — we do
# the same in `encry()` below by building a fresh key per call.
DEFAULT_ENCRYPT_KEY: bytes = bytes([1, 2, 3, 4, 5, 6, 7, 8,
                                    1, 2, 3, 4, 5, 6, 7, 8])
DEFAULT_ITERATIONS: int = 5000


def aes_cbc_128_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Java's `aes_cbc_128_encrypt`. Pure AES/CBC/NoPadding."""
    if len(key) != 16:
        raise ValueError(f"key must be 16 bytes, got {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"iv must be 16 bytes, got {len(iv)}")
    if len(data) % 16 != 0:
        raise ValueError(f"data length {len(data)} not a multiple of 16")
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def aes_cbc_128_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Java's `aes_cbc_128_decrypt`. Pure AES/CBC/NoPadding."""
    if len(key) != 16:
        raise ValueError(f"key must be 16 bytes, got {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"iv must be 16 bytes, got {len(iv)}")
    if len(data) % 16 != 0:
        raise ValueError(f"data length {len(data)} not a multiple of 16")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def encry_bytes(src: bytes, kbd_key: bytes, kbd_iv: bytes, length: int) -> bytes:
    """Java's `AESHandler.encry_bytes` — Linux/encrypted-status path.

        int srcLen = ((len + 15) / 16) * 16;
        byte[] tem_src = new byte[srcLen];
        System.arraycopy(src, 0, tem_src, 0, len);
        return aes_cbc_128_encrypt(tem_src, kbdKey, kbdIV);
    """
    if length <= 0 or src is None:
        raise ValueError(f"length must be > 0, got {length}")
    src_len = ((length + 15) // 16) * 16
    padded = bytes(src[:length]) + b"\x00" * (src_len - length)
    return aes_cbc_128_encrypt(padded, kbd_key, kbd_iv)


def encry(src: bytes, codekey: int, length: int) -> bytes:
    """Java's `AESHandler.encry` — Windows path.

        encrypt[0] = (byte) (codekey >> 24);
        encrypt[1] = (byte) (codekey >> 16);
        encrypt[2] = (byte) (codekey >> 8);
        encrypt[3] = (byte) codekey;
        // encrypt = [codekey_be_4B, 5,6,7,8,1,2,3,4,5,6,7,8]
        return aes_cbc_128_encrypt(tem_src, encrypt, iv=zeros);
    """
    if length <= 0 or src is None:
        raise ValueError(f"length must be > 0, got {length}")
    src_len = ((length + 15) // 16) * 16
    padded = bytes(src[:length]) + b"\x00" * (src_len - length)
    # Bake codekey into the first 4 bytes of the static encrypt key.
    # Java mutates a static field in place; we build a fresh bytearray
    # per call to avoid sharing mutable state across threads.
    key = bytearray(DEFAULT_ENCRYPT_KEY)
    key[0] = (codekey >> 24) & 0xFF
    key[1] = (codekey >> 16) & 0xFF
    key[2] = (codekey >> 8) & 0xFF
    key[3] = codekey & 0xFF
    return aes_cbc_128_encrypt(padded, bytes(key), DEFAULT_IV)


def generate_stored_password_hash(plain: str | bytes, pass_len: int,
                                  rand_salt: bytes | None = None,
                                  hmac: str = "PBKDF2WithHmacSHA1",
                                  iterations: int = DEFAULT_ITERATIONS) -> bytes:
    """Port of `generateStoredPasswordHash(char[], int, byte[], String, int)`.

    Java:
        PBEKeySpec spec = new PBEKeySpec(plainKey, saltValue, iterations, passLen * 8);
        SecretKeyFactory skf = SecretKeyFactory.getInstance(hmac);
        return skf.generateSecret(spec).getEncoded();

    Java's `PBKDF2WithHmacSHA1` / `PBKDF2WithHmacSHA256` encode chars
    as bytes by taking the low 8 bits of each char (PKCS#5 v2 standard).
    For the integer-decimal passwords this code uses
    (`String.valueOf(int)`), every character is ASCII and the low-byte
    encoding is identical to `str.encode("ascii")`. We accept either a
    `str` (encoded as ASCII) or pre-encoded bytes.
    """
    if isinstance(plain, str):
        password = plain.encode("ascii")
    else:
        password = bytes(plain)
    salt = rand_salt if rand_salt is not None else DEFAULT_SALT
    if hmac == "PBKDF2WithHmacSHA1":
        algo = hashes.SHA1()
    elif hmac == "PBKDF2WithHmacSHA256":
        algo = hashes.SHA256()
    else:
        raise ValueError(f"unsupported hmac {hmac!r}")
    kdf = PBKDF2HMAC(algorithm=algo, length=pass_len, salt=salt,
                     iterations=iterations)
    return kdf.derive(password)
