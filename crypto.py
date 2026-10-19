"""微信小店自研消息加解密。

参考官方实现（AESEncryptWithRandom/AESDecryptWithAppId）：
    1. AESKey = base64decode(EncodingAESKey + "=")，32 字节
    2. IV = AESKey[:16]
    3. AES-256-CBC + PKCS#7
    4. 密文 Base64
    5. 明文结构 = random(16B) + msg_len(4B 网络字节序) + msg + appid
    6. 验签 sha1("".join(sorted([token, timestamp, nonce [, encrypt]])))
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import time
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.backends import default_backend


def _sha1_hex(items: list[str]) -> str:
    return hashlib.sha1("".join(sorted(items)).encode("utf-8")).hexdigest()


def verify_get(token: str, timestamp: str, nonce: str, echostr: str,
               signature: str) -> bool:
    """GET 验签：首次提交 URL 时由微信发起。"""
    expected = _sha1_hex([token, timestamp, nonce])
    return expected == signature


def verify_post(token: str, timestamp: str, nonce: str, encrypt: str,
                msg_signature: str) -> bool:
    """POST 验签：注意用 msg_signature（不是 signature）。"""
    expected = _sha1_hex([token, timestamp, nonce, encrypt])
    return expected == msg_signature


def _derive_aes_key(encoding_aes_key: str) -> bytes:
    """43 字符 EncodingAESKey + "=" 后 base64 解码得 32 字节 AESKey。"""
    raw = encoding_aes_key + "="
    return base64.b64decode(raw)


def decrypt(encoding_aes_key: str, encrypt_b64: str) -> tuple[str, str]:
    """解密，返回 (msg_json_str, appid)。"""
    aes_key = _derive_aes_key(encoding_aes_key)
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    plain = decryptor.update(base64.b64decode(encrypt_b64)) + decryptor.finalize()
    # 微信自定义填充：取末尾字节为填充长度（允许 1-32，非标准 PKCS7 的 1-16）
    pad = plain[-1]
    if 1 <= pad <= 32:
        plain = plain[:-pad]
    # random(16) + msg_len(4) + msg + appid
    if len(plain) < 20:
        raise ValueError("解密后明文长度异常")
    msg_len = struct.unpack("!I", plain[16:20])[0]
    if 20 + msg_len > len(plain):
        raise ValueError("msg_len 与实际明文长度不匹配")
    msg = plain[20:20 + msg_len].decode("utf-8")
    appid = plain[20 + msg_len:].decode("utf-8")
    return msg, appid


def encrypt(encoding_aes_key: str, app_id: str, msg: str) -> str:
    """加密回包（客服场景大多用不到，保留以备需要加密回包时使用）。"""
    aes_key = _derive_aes_key(encoding_aes_key)
    iv = aes_key[:16]
    random_bytes = os.urandom(16)
    msg_bytes = msg.encode("utf-8")
    appid_bytes = app_id.encode("utf-8")
    body = random_bytes + struct.pack("!I", len(msg_bytes)) + msg_bytes + appid_bytes
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(body) + padder.finalize()
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    enc = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(enc).decode("ascii")


def make_post_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """加密回包用的签名。"""
    return _sha1_hex([token, timestamp, nonce, encrypt])


def build_encrypted_response(token: str, encoding_aes_key: str,
                             app_id: str, plaintext_msg: str) -> str:
    """构造加密回包 JSON 字符串（极少用到，绝大多数客服回调直接返 success）。"""
    encrypt = encrypt(encoding_aes_key, app_id, plaintext_msg)
    timestamp = str(int(time.time()))
    nonce = socket.gethostname()[:8] + str(int(time.time() * 1000))[-4:]
    signature = make_post_signature(token, timestamp, nonce, encrypt)
    import json
    return json.dumps({
        "Encrypt": encrypt,
        "MsgSignature": signature,
        "TimeStamp": int(timestamp),
        "Nonce": nonce,
    }, ensure_ascii=False)
