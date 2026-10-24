"""xwallet8.com（xvip）接口加解密。

响应体是 AES-256-ECB(Pkcs7) 密文，AES 密钥随机生成、经 RSA 公钥加密后
放在响应头 ``encrypt-key`` 里。RSA 私钥硬编码在站点前端脚本里，这里复刻解密。
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as apad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_RSA_PRIV_B64 = (
    "MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4wggE6AgEAAkEAmc3CuPiG"
    "L/LcIIm7zryCEIbl1SPzBkr75E2VMtxegyZ1lYRD+7TZGAPkvIsBcaMs"
    "6Nsy0L78n2qh+lIZMpLH8wIDAQABAkEAk82Mhz0tlv6IVCyIcw/s3f0E"
    "+WLmtPFyR9/WtV3Y5aaejUkU60JpX4m5xNR2VaqOLTZAYjW8Wy0aXr3zY"
    "IhhQQIhAMfqR9oFdYw1J9SsNc+CrhugAvKTi0+BF6VoL6psWhvbAiEAxP"
    "PNTmrkmrXwdm/pQQu3UOQmc2vCZ5tiKpW10CgJi8kCIFGkL6utxw93Ncj"
    "4exE/gPLvKcT+1Emnoox+O9kRXss5AiAMtYLJDaLEzPrAWcZeeSgSIzbL"
    "+ecokmFKSDDcRske6QIgSMkHedwND1olF8vlKsJUGK3BcdtM8w4Xq7BpS"
    "BwsloE="
)

_priv = None


def _load_priv():
    global _priv
    if _priv is None:
        _priv = serialization.load_der_private_key(
            base64.b64decode(_RSA_PRIV_B64), password=None
        )
    return _priv


def decrypt(encrypt_key: str, body: str) -> str:
    """解密 xwallet8.com 接口响应，返回明文 JSON 字符串。"""
    aes_key = base64.b64decode(
        _load_priv().decrypt(base64.b64decode(encrypt_key), apad.PKCS1v15())
    )
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    dec = cipher.decryptor()
    plain = dec.update(base64.b64decode(body)) + dec.finalize()
    return plain[: -plain[-1]].decode("utf-8")
