"""合并细碎文字事件，并在重复正文失控前终止生成。"""

import time
from collections.abc import Callable

from app.providers.client import ProviderFailure


class TextStream:
    """按字符数或时间发布增量；检查仍按原始片段执行，不延迟取消响应。"""

    def __init__(self, publish: Callable[[str], None]) -> None:
        """每次模型调用独立缓冲，避免工具阶段和不同模型调用的文字混在一起。"""
        self.publish = publish
        self.pending = ""
        self.tail = ""
        self.last_flush = time.monotonic()

    def add(self, text: str) -> None:
        """连续三次重复至少八十字符时停止，避免正常短词重复触发保护。"""
        # 只检查有界尾部，检测不随整篇回答长度增长；跨网络分片仍能识别重复。
        self.tail = (self.tail + text)[-2400:]
        for width in range(80, min(800, len(self.tail) // 3) + 1):
            if self.tail[-width:] * 3 == self.tail[-width * 3:]:
                raise ProviderFailure("model_repetition")
        self.pending += text
        if len(self.pending) >= 128 or time.monotonic() - self.last_flush >= 0.15:
            self.flush()

    def flush(self) -> None:
        """模型正常结束或异常退出时发布尾部，保证已接收的部分回答不丢失。"""
        if self.pending:
            self.publish(self.pending)
            self.pending = ""
            self.last_flush = time.monotonic()
