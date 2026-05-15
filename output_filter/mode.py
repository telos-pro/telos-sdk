"""``StelaMode`` —— 两个独立开关（STELA / RTK）的 4 态组合。

四态对照：

| label   | stela | rtk  | 含义                                       |
|---------|-------|------|--------------------------------------------|
| ``none``  | ✗   | ✗  | 纯透传，proxy 不改写任何字节                |
| ``stela`` | ✓   | ✗  | 只跑 STELA 管线（cache_control / ref-pool） |
| ``rtk``   | ✗   | ✓  | 只过 RTK 工具输出过滤，不打 cache 标记      |
| ``both``  | ✓   | ✓  | RTK 先缩工具结果，STELA 再稳前缀（默认推荐）|

设计成「两个布尔」而非单个枚举：proxy / dashboard 各自只关心其中一维时
不用反复 match 4 个分支，``mode.stela`` / ``mode.rtk`` 直接取。
"""

from __future__ import annotations

from dataclasses import dataclass

_LABEL_TO_FLAGS: dict[str, tuple[bool, bool]] = {
    "none": (False, False),
    "stela": (True, False),
    "rtk": (False, True),
    "both": (True, True),
}

# 所有合法 label，CLI / argparse choices 用。
MODE_LABELS: tuple[str, ...] = ("none", "stela", "rtk", "both")


@dataclass(frozen=True)
class StelaMode:
    """一次请求要启用哪些优化层。不可变，可安全跨 session 复用。"""

    stela: bool = True
    rtk: bool = False

    @property
    def label(self) -> str:
        for name, flags in _LABEL_TO_FLAGS.items():
            if flags == (self.stela, self.rtk):
                return name
        # 理论不可达：两个布尔必落在 4 态之一。
        return f"stela={self.stela},rtk={self.rtk}"

    @classmethod
    def from_label(cls, label: str | None) -> "StelaMode":
        """把 ``none|stela|rtk|both`` 解析成 ``StelaMode``。

        ``None`` / 空串 / 未知值都退化到默认 ``stela``（保持历史行为：
        proxy 在引入开关前等价于「只有 STELA」）。
        """
        if not label:
            return cls()
        flags = _LABEL_TO_FLAGS.get(label.strip().lower())
        if flags is None:
            return cls()
        return cls(stela=flags[0], rtk=flags[1])


DEFAULT_MODE = StelaMode()
