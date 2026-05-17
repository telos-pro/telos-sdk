"""工具输出 token 估算 —— 无第三方依赖的启发式。

dashboard 历史上用固定 ``chars / 4`` 把过滤省下的字符折成 token，对代码 /
日志 / CJK 误差很大。这里提供一个按「单词 / 数字 / 标点 / 空白游程」分块的
启发式估算，更贴近 BPE（cl100k 量级）粒度：

- ASCII 字母词：约每 4 字符 1 个 subword token（长词会被 BPE 拆分）。
- 数字 / 标点 / CJK 等单字符：各算 1 token（代码 / 日志里标点密集，
  固定 chars/4 会严重低估）。
- 空白：单个空格并入相邻 token（0 计），长空白游程（缩进 / 多换行）
  约每 3 字符 1 token。

纯函数、永不抛错；任何异常退回 ``len(text) // 4`` 兜底。
"""

from __future__ import annotations

import re

# 字母词 | 空白游程 | 任意单字符（数字 / 标点 / CJK …）
_CHUNK_RE = re.compile(r"[A-Za-z]+|\s+|.", re.DOTALL)


def estimate_tokens(text: str) -> int:
    """估算 ``text`` 的 token 数（启发式，非精确 tokenizer）。"""
    if not text:
        return 0
    try:
        total = 0.0
        for m in _CHUNK_RE.finditer(text):
            chunk = m.group()
            c0 = chunk[0]
            if c0.isspace():
                # 单空格并入相邻 token；长空白游程约 3 字符 / token
                total += 0 if len(chunk) == 1 else max(1, len(chunk) // 3)
            elif c0.isascii() and c0.isalpha():
                # ASCII 词：BPE 子词约每 4 字符 1 个，至少 1
                total += max(1, round(len(chunk) / 4))
            else:
                # 数字 / 标点 / CJK 单字符：各 1 token
                total += 1
        return max(1, int(round(total)))
    except Exception:  # noqa: BLE001 — 估算器永不抛错
        return max(1, len(text) // 4)
