"""对照跑分用的指标。

这里定义的四个指标里,**技术词命中率是最重要的那个**。
原因:中英混说的失败方式很特别——模型往往整句读起来通顺,
但把嵌在中文里的英文词音译成了中文("commit" → "康米特")。
这种错误在 CER 上只体现为几个字的差异,看起来不严重,
实际却让听写完全没法用(你还得手动改回去,那还不如自己打)。
所以要单独把它拎出来量化。
"""

from __future__ import annotations

import re
import unicodedata

# 归一化时去掉的标点。中英文都要覆盖。
# 注意:整条必须是**一个**原始字符串,中间不能出现让 Python 提前结束
# 字面量的裸引号,否则后半段会退化成非 raw 字符串,转义就乱了。
PUNCT_CHARS = (
    "，。！？、；：（）《》【】…—"      # 中文标点
    "“”‘’"          # 中文弯引号 “ ” ‘ ’
    ",.!?;:'\"()<>[]{}-_/\\|@#$%^&*+=~`"  # 英文标点
)
PUNCT_PATTERN = re.compile("[" + re.escape(PUNCT_CHARS) + "]")
WHITESPACE_PATTERN = re.compile(r"\s+")
ASCII_WORD_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9'\-]*")


def normalize(text: str, keep_space: bool = False) -> str:
    """归一化:全角转半角、去标点、统一小写、压缩空白。

    比较文本时不该因为标点和大小写的差异就判错——
    那些是模型的书写风格,不是识别错误。
    """
    text = unicodedata.normalize("NFKC", text)
    text = PUNCT_PATTERN.sub(" ", text)
    text = text.lower()
    text = WHITESPACE_PATTERN.sub(" " if keep_space else "", text).strip()
    return text


def levenshtein(a: list, b: list) -> int:
    """编辑距离。用滚动数组,长文本也不会吃内存。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(
                prev[j] + 1,        # 删除
                cur[j - 1] + 1,     # 插入
                prev[j - 1] + (ca != cb),  # 替换
            )
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    """字错率(Character Error Rate)。按字符算,适合中文。"""
    ref = list(normalize(reference))
    hyp = list(normalize(hypothesis))
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein(ref, hyp) / len(ref)


def wer(reference: str, hypothesis: str) -> float:
    """词错率(Word Error Rate)。只看 ASCII 单词,用来衡量英文部分。

    中文没有词边界,混在一起算 WER 没有意义,所以这里
    只抽出英文单词来比——这恰好也是我们最关心的部分。
    """
    ref = ASCII_WORD_PATTERN.findall(reference.lower())
    hyp = ASCII_WORD_PATTERN.findall(hypothesis.lower())
    if not ref:
        return float("nan")  # 这句里本来就没英文,不参与统计
    return levenshtein(ref, hyp) / len(ref)


def term_hits(terms: list[str], hypothesis: str) -> tuple[int, list[str]]:
    """技术词命中:这些词有没有以**英文形式**出现在识别结果里。

    返回 (命中数, 没命中的词列表)。

    匹配放得比较宽(忽略大小写和空格),因为我们要判断的是
    "模型有没有把它当英文词识别出来",而不是苛求一模一样的写法。
    """
    if not terms:
        return 0, []
    hyp_norm = normalize(hypothesis, keep_space=True)
    hyp_tight = hyp_norm.replace(" ", "")

    missed = []
    for term in terms:
        t_norm = normalize(term, keep_space=True)
        t_tight = t_norm.replace(" ", "")
        if t_norm and (t_norm in hyp_norm or t_tight in hyp_tight):
            continue
        missed.append(term)
    return len(terms) - len(missed), missed


def has_repetition(text: str, ngram: int = 8, times: int = 3) -> bool:
    """粗查结果里有没有明显的重复(无限重复 bug 的痕迹)。

    守护进程里的闸门是在解码循环里按 token 判的;这里是拿最终文本
    再查一遍,用来统计"这个模型在这批语料上跑飞了几次"。
    """
    s = normalize(text)
    if len(s) < ngram * times:
        return False
    for i in range(len(s) - ngram * times + 1):
        chunk = s[i:i + ngram]
        if all(s[i + ngram * k: i + ngram * (k + 1)] == chunk for k in range(1, times)):
            return True
    return False
