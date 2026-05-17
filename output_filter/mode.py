"""``TelosMode`` —— 两个独立开关（TELOS / RTK）的 4 态组合。

四态对照：

| label   | telos | rtk  | 含义                                       |
|---------|-------|------|--------------------------------------------|
| ``none``  | ✗   | ✗  | 纯透传，proxy 不改写任何字节                |
| ``telos`` | ✓   | ✗  | 只跑 TELOS 管线（cache_control / ref-pool） |
| ``rtk``   | ✗   | ✓  | 只过 RTK 工具输出过滤，不打 cache 标记      |
| ``both``  | ✓   | ✓  | RTK 先缩工具结果，TELOS 再稳前缀（默认推荐）|

设计成「两个布尔」而非单个枚举：proxy / dashboard 各自只关心其中一维时
不用反复 match 4 个分支，``mode.telos`` / ``mode.rtk`` 直接取。
"""

from __future__ import annotations

from dataclasses import dataclass

_LABEL_TO_FLAGS: dict[str, tuple[bool, bool]] = {
    "none": (False, False),
    "telos": (True, False),
    "rtk": (False, True),
    "both": (True, True),
}

# 所有合法 label，CLI / argparse choices 用。
MODE_LABELS: tuple[str, ...] = ("none", "telos", "rtk", "both")


@dataclass(frozen=True)
class TelosMode:
    """一次请求要启用哪些优化层。不可变，可安全跨 session 复用。"""

    telos: bool = True
    rtk: bool = False

    @property
    def label(self) -> str:
        for name, flags in _LABEL_TO_FLAGS.items():
            if flags == (self.telos, self.rtk):
                return name
        # 理论不可达：两个布尔必落在 4 态之一。
        return f"telos={self.telos},rtk={self.rtk}"

    @classmethod
    def from_label(cls, label: str | None) -> "TelosMode":
        """把 ``none|telos|rtk|both`` 解析成 ``TelosMode``。

        ``None`` / 空串 / 未知值都退化到默认 ``telos``（保持历史行为：
        proxy 在引入开关前等价于「只有 TELOS」）。
        """
        if not label:
            return cls()
        flags = _LABEL_TO_FLAGS.get(label.strip().lower())
        if flags is None:
            return cls()
        return cls(telos=flags[0], rtk=flags[1])


DEFAULT_MODE = TelosMode()
