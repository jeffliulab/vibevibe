"""录音预处理 —— 针对"输入增益过高导致削顶"这个真实工况。

背景:这套系统的实际使用条件是 C920 摄像头麦 + ALSA 采集增益拉满(+44dB)。
实测录音里 0.2%~2.2% 的样本被削平到 ±1.0。这不是测试环境的瑕疵,
是天天都会遇到的常态,所以要在管线里处理掉,而不是要求用户改设备。

削顶的危害不在"响度不对",而在**谐波失真**:波形顶部被削平相当于
叠加了一堆本来不存在的高频谐波,而 ASR 的前端(mel 频谱)对高频
是敏感的。修复的目标就是把这些假谐波去掉。

好消息是实测削顶段极短(中位 6 个样本 / 0.38ms,最长 14 个),
这种短缺口用两侧波形插值重建的效果很好——信息虽然丢了,
但波形的连续性和频谱形态能恢复大半。

⚠️ 一句实话:重建**不能凭空找回被削掉的信息**,它只能减少谐波失真。
到底有没有用、有多大用,必须靠 A/B 跑分来证明(见 bench/compare.py 的
--preproc 参数),而不是想当然地默认打开。
"""

from __future__ import annotations

import numpy as np

from .config import PreprocessAudioConfig


def clip_ratio(pcm: np.ndarray, threshold: float = 0.99) -> float:
    """削顶样本占比。比峰值更能说明问题——峰值只要有一个样本到顶就是 1.0,
    分不清是偶发爆音还是持续爆音,比例才分得清。"""
    if len(pcm) == 0:
        return 0.0
    return float((np.abs(pcm) >= threshold).mean())


def _clipped_runs(pcm: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """找出所有连续的削顶区间,返回 [(起, 止), ...],止是开区间。"""
    clipped = np.abs(pcm) >= threshold
    if not clipped.any():
        return []
    diff = np.diff(clipped.astype(np.int8))
    starts = list(np.where(diff == 1)[0] + 1)
    ends = list(np.where(diff == -1)[0] + 1)
    if clipped[0]:
        starts.insert(0, 0)
    if clipped[-1]:
        ends.append(len(pcm))
    return list(zip(starts, ends))


def declip(
    pcm: np.ndarray,
    threshold: float = 0.99,
    context: int = 8,
    max_run: int = 64,
    max_overshoot: float = 2.0,
) -> tuple[np.ndarray, int]:
    """重建被削平的波峰。返回 (处理后的音频, 修复的段数)。

    做法:对每个削顶段,拿它两侧各 context 个未削顶的样本,
    用三次多项式拟合出这段本来应该长什么样,再填回去。
    重建出来的峰值会超过 1.0(本来就该超过,不然也不会削顶),
    所以调用方**必须**在之后做一次归一化,否则存盘时又被削一遍。

    只修短段。太长的段(超过 max_run)说明不是削顶而是别的问题
    (比如信号饱和或者设备故障),硬插值只会造出假波形,不如不动。
    """
    out = pcm.astype(np.float64, copy=True)
    n = len(out)
    repaired = 0

    for start, end in _clipped_runs(pcm, threshold):
        run_len = end - start
        if run_len > max_run:
            continue  # 太长,不瞎补

        left_lo = max(0, start - context)
        right_hi = min(n, end + context)
        # 取两侧未削顶的样本当锚点
        left_idx = np.arange(left_lo, start)
        right_idx = np.arange(end, right_hi)
        left_idx = left_idx[np.abs(out[left_idx]) < threshold] if len(left_idx) else left_idx
        right_idx = right_idx[np.abs(out[right_idx]) < threshold] if len(right_idx) else right_idx

        anchors = np.concatenate([left_idx, right_idx])
        if len(anchors) < 4:
            continue  # 锚点太少,拟合不出可信的形状,保持原样

        gap = np.arange(start, end)
        # 拟合前**必须**把横坐标平移到缺口中心附近。
        # 直接拿绝对样本下标(可能是十几万)去做三次拟合,x³ 就是 1e15 量级,
        # 数值条件数爆炸,解出来是垃圾——受控实验里实测能把信噪比搞到 -145dB。
        origin = (start + end) / 2.0
        try:
            coeffs = np.polyfit(anchors - origin, out[anchors], deg=3)
            rebuilt = np.polyval(coeffs, gap - origin)
        except (np.linalg.LinAlgError, ValueError):
            continue

        # 防跑飞:重建值必须跟原来的削顶方向一致,而且不能离谱地大。
        #
        # 上限**必须相对于削顶阈值**,不能写死成绝对值——踩过这个坑:
        # 原本写的是绝对上限 2.5,在阈值只有 0.1 的场景下等于允许
        # 造出 25 倍于阈值的假波形,三次拟合一旦外推飙起来就直接
        # 把信噪比搞坏 20dB(受控实验里实测到了)。
        # 真实的轻中度削顶,被削掉的峰一般不会超过阈值的 1.5~2 倍。
        sign = np.sign(out[start:end])
        sign[sign == 0] = 1.0
        ceiling = threshold * max_overshoot
        magnitude = np.clip(np.abs(rebuilt), threshold, ceiling)
        out[start:end] = sign * magnitude
        repaired += 1

    return out.astype(np.float32), repaired


def normalize(
    pcm: np.ndarray,
    target_peak: float = 0.85,
    target_rms: float = 0.0,
) -> np.ndarray:
    """把整体音量缩放到一个稳妥的范围。

    默认按峰值归一化到 0.85,留一点余量。如果指定了 target_rms,
    则优先按 RMS 归一化(更接近"感知响度"),但仍然保证峰值不越界。

    为什么要做:削顶重建之后峰值会超过 1.0,不缩回来存 wav 时会被
    再削一次,前面的活儿就白干了。
    """
    x = pcm.astype(np.float32, copy=True)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak <= 0:
        return x

    if target_rms > 0:
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        if rms > 0:
            x = x * (target_rms / rms)
            peak = float(np.abs(x).max())

    if peak > target_peak:
        x = x * (target_peak / peak)
    return x.astype(np.float32)


def highpass(pcm: np.ndarray, sample_rate: int, cutoff_hz: float = 80.0) -> np.ndarray:
    """砍掉低频隆隆声(空调、桌面震动、麦克风支架传导)。

    注:实测这批 C920 录音里 80Hz 以下只占 0.7%~1.8% 的能量,
    所以默认是**关**的。留着这个函数是为了换麦克风或换环境后备用。
    """
    from scipy import signal

    sos = signal.butter(4, cutoff_hz, btype="highpass", fs=sample_rate, output="sos")
    return signal.sosfilt(sos, pcm).astype(np.float32)


def preprocess(
    pcm: np.ndarray,
    sample_rate: int,
    cfg: PreprocessAudioConfig,
) -> tuple[np.ndarray, dict]:
    """按配置跑整条预处理链。返回 (处理后的音频, 这一趟干了什么)。

    返回的字典是给日志和 bench 用的,能看清每一步实际起了多少作用。
    """
    info: dict = {
        "clip_ratio_before": clip_ratio(pcm, cfg.clip_threshold),
        "declipped_runs": 0,
        "highpass": False,
        "gain": 1.0,
    }
    if not cfg.enabled or len(pcm) == 0:
        info["clip_ratio_after"] = info["clip_ratio_before"]
        return pcm, info

    x = pcm
    if cfg.declip:
        x, repaired = declip(
            x, threshold=cfg.clip_threshold,
            context=cfg.declip_context, max_run=cfg.declip_max_run,
            max_overshoot=cfg.declip_max_overshoot)
        info["declipped_runs"] = repaired

    if cfg.highpass_hz > 0:
        x = highpass(x, sample_rate, cfg.highpass_hz)
        info["highpass"] = True

    if cfg.normalize:
        before = float(np.abs(x).max()) or 1.0
        x = normalize(x, target_peak=cfg.target_peak, target_rms=cfg.target_rms)
        after = float(np.abs(x).max()) or 1.0
        info["gain"] = after / before

    info["clip_ratio_after"] = clip_ratio(x, cfg.clip_threshold)
    return x, info
