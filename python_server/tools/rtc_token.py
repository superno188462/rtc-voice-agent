"""火山 RTC AccessToken 的 Python 等价实现。

二进制字段顺序、Little Endian 编码、HMAC-SHA256 和 Base64 拼接均与原
Server/token.js 保持一致。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum

VERSION = "001"
APP_ID_LENGTH = 24
_PROCESS_NONCE = random.SystemRandom().randrange(0, 0x1_0000_0000)


class Privileges(IntEnum):
    """RTC Token 权限编号，与原 Node 实现一致。"""

    PrivPublishStream = 0
    PrivPublishAudioStream = 1
    PrivPublishVideoStream = 2
    PrivPublishDataStream = 3
    PrivSubscribeStream = 4


class ByteBuffer:
    """生成 RTC Token 所需的定长小端二进制结构。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def put_uint16(self, value: int) -> "ByteBuffer":
        self.buffer.extend(struct.pack("<H", value))
        return self

    def put_uint32(self, value: int) -> "ByteBuffer":
        self.buffer.extend(struct.pack("<I", value))
        return self

    def put_bytes(self, value: bytes) -> "ByteBuffer":
        self.put_uint16(len(value))
        self.buffer.extend(value)
        return self

    def put_string(self, value: str) -> "ByteBuffer":
        return self.put_bytes(value.encode("utf-8"))

    def put_tree_map_uint32(self, value: dict[int, int]) -> "ByteBuffer":
        self.put_uint16(len(value))
        # JavaScript Object.keys 对整数键按升序枚举，需保持完全相同的字节顺序。
        for key in sorted(value):
            item = value[key]
            self.put_uint16(key)
            self.put_uint32(item)
        return self

    def pack(self) -> bytes:
        return bytes(self.buffer)


@dataclass
class AccessToken:
    """创建可供 Web RTC SDK 使用的客户端进房 Token。"""

    app_id: str
    app_key: str
    room_id: str
    user_id: str
    issued_at: int = field(default_factory=lambda: int(time.time()))
    nonce: int = field(default_factory=lambda: _PROCESS_NONCE)
    expire_at: int = 0
    privileges: dict[int, int] = field(default_factory=dict)

    def add_privilege(self, privilege: Privileges, expire_timestamp: int) -> None:
        """增加权限；发布流权限自动展开为音频、视频和数据权限。"""

        self.privileges[int(privilege)] = expire_timestamp
        if privilege == Privileges.PrivPublishStream:
            self.privileges[int(Privileges.PrivPublishVideoStream)] = expire_timestamp
            self.privileges[int(Privileges.PrivPublishAudioStream)] = expire_timestamp
            self.privileges[int(Privileges.PrivPublishDataStream)] = expire_timestamp

    def expire_time(self, expire_timestamp: int) -> None:
        """设置 Token 的绝对过期 Unix 时间戳。"""

        self.expire_at = expire_timestamp

    def pack_message(self) -> bytes:
        """按照 Node token.js 的字段顺序生成待签名消息。"""

        return (
            ByteBuffer()
            .put_uint32(self.nonce)
            .put_uint32(self.issued_at)
            .put_uint32(self.expire_at)
            .put_string(self.room_id)
            .put_string(self.user_id)
            .put_tree_map_uint32(self.privileges)
            .pack()
        )

    def serialize(self) -> str:
        """生成 `001 + AppId + Base64(message, signature)` 格式的 RTC Token。"""

        message = self.pack_message()
        signature = hmac.new(self.app_key.encode(), message, hashlib.sha256).digest()
        content = ByteBuffer().put_bytes(message).put_bytes(signature).pack()
        return f"{VERSION}{self.app_id}{base64.b64encode(content).decode()}"
