"""兜底闸门。

背景:Qwen3-ASR 有一个已知且未修复的毛病——某些输入会让解码器
陷入无限重复,一直吐同样的 token 直到撞上 max_new_tokens
(官方 issue #129,截至 2026-04 仍有 148+ 未解决 issue)。

对听写工具来说这是最不能忍的失败:一屏乱码被粘进你正在写的东西里。
所以这里做三道闸:

  1. 解码循环内实时检测重复 → 一旦发现立刻停,不等跑满 512 个 token
  2. 整体超时 → 超了就放弃,报错而不是交出半成品
  3. 结果长度检查 → 太短当没听见,太长说明跑飞了

三道都是"宁可什么都不输出,也不要输出垃圾"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import GuardConfig


class GuardTripped(Exception):
    """闸门触发。带上原因,方便日志里看清是哪一道拦下的。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}{': ' + detail if detail else ''}")


@dataclass
class RepetitionGuard:
    """解码循环里的实时重复检测。

    用法:每生成一个 token 就调一次 feed();返回 True 表示该停了。

    检测两种情况:
      - 单个 token 连续重复 N 次(最常见的卡死形态)
      - 最近 k 个 token 组成的片段连续重复 M 次(绕圈说同一句话)
    """

    cfg: GuardConfig

    def __post_init__(self) -> None:
        self._tokens: list[int] = []
        self._run_token: int | None = None
        self._run_len = 0
        self.tripped_reason: str | None = None

    def feed(self, token: int) -> bool:
        """喂一个新 token。返回 True 表示检测到重复,应当停止解码。"""
        self._tokens.append(token)

        # 情况一:同一个 token 连着来
        if token == self._run_token:
            self._run_len += 1
        else:
            self._run_token = token
            self._run_len = 1
        if self._run_len >= self.cfg.single_token_repeat_max:
            self.tripped_reason = (
                f"单个 token 连续重复 {self._run_len} 次(阈值 "
                f"{self.cfg.single_token_repeat_max})"
            )
            return True

        # 情况二:末尾的 n-gram 连续重复
        n = self.cfg.repeat_ngram_size
        times = self.cfg.repeat_max_times
        if n > 0 and times > 1 and len(self._tokens) >= n * times:
            tail = self._tokens[-n:]
            repeated = all(
                self._tokens[-n * (i + 1) : len(self._tokens) - n * i] == tail
                for i in range(1, times)
            )
            if repeated:
                self.tripped_reason = (
                    f"末尾 {n}-gram 连续重复 {times} 次"
                )
                return True

        return False


class Deadline:
    """整段转写的超时闸。"""

    def __init__(self, timeout_sec: float) -> None:
        self.timeout_sec = timeout_sec
        self._start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def expired(self) -> bool:
        return self.timeout_sec > 0 and self.elapsed > self.timeout_sec

    def check(self) -> None:
        if self.expired():
            raise GuardTripped(
                "转写超时",
                f"已用 {self.elapsed:.1f}s,上限 {self.timeout_sec:.1f}s",
            )


def check_text(text: str, cfg: GuardConfig) -> str:
    """转写完成后的最终检查。通过则返回原文,否则抛 GuardTripped。"""
    stripped = text.strip()
    if len(stripped) < cfg.min_text_len:
        raise GuardTripped("识别结果为空", f"长度 {len(stripped)}")
    if cfg.max_text_len > 0 and len(stripped) > cfg.max_text_len:
        raise GuardTripped(
            "识别结果异常长",
            f"{len(stripped)} 字,上限 {cfg.max_text_len}——大概率是模型跑飞了",
        )
    return text
