"""转写结果的后处理。

只做确定性的、可配置的清理,不做任何"智能改写"——
听写工具最忌讳自作聪明改你说的话。
"""

from __future__ import annotations

from .config import PostprocessConfig

# 中文与英文句末标点。口述时通常不想要句末的句号,
# 但问号感叹号是有意义的,所以只处理句号。
TRAILING_PERIODS = ("。", ".")


def postprocess(text: str, cfg: PostprocessConfig) -> str:
    out = text

    # 技术词纠正表。配置项形如:
    #   replacements = [{ from = "康米特", to = "commit" }]
    # 顺序敏感,按配置里写的顺序依次替换。
    for rule in cfg.replacements:
        src = rule.get("from")
        dst = rule.get("to", "")
        if src:
            out = out.replace(src, dst)

    if cfg.strip_surrounding_space:
        out = out.strip()

    if cfg.strip_trailing_period:
        while out.endswith(TRAILING_PERIODS):
            out = out[:-1]

    return out
