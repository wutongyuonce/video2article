#!/usr/bin/env python3
"""本地转写音频/视频：静音优先分片 + 超限重试。

用法:
    uv run --with transcribe-cpp python asr.py <audio|video> <model.gguf>
        [--chunk 60]    目标片长（秒），默认 60
        [--window 12]   在目标切点前后多少秒内找静音，默认 12
        [--fixed]       关闭静音优先，退回固定时长切分
    python3 asr.py --selftest

依赖: Python 3.9+、ffmpeg / ffprobe 在 PATH 上、transcribe-cpp。

为什么分片: LLM 式 ASR（qwen3 / voxtral / canary / cohere / moss）整段音频是一次
连续生成，有生成 token 上限——Qwen3-ASR 实测约 474 字就 OutputTruncated，且与音频
长度和 n_ctx 都无关。窗口式模型（whisper / sensevoice / parakeet）没有这个上限，
但分片对它们无害（只是多几次调用）。

为什么按静音切而不是固定时长: 固定时长会在句子中间斩断，而静音点落在停顿处。
静音检测整片扫一遍只要约 0.6 秒（21 分钟音频），相对转写耗时（约 80 秒）可忽略。
找不到静音时自动退回固定切点，所以永不退化。
"""

from __future__ import annotations

import array
import re
import subprocess
import sys

TARGET_SECONDS = 60.0
WINDOW_SECONDS = 12.0
# 片长硬上限：Qwen3-ASR 实测 75s → 427 字（安全），90s 就截断。
# 切点吸附最远漂移到 target+window，所以必须保证它仍在上限内。
MAX_SAFE_SECONDS = 75.0
SILENCE_DB = -30
SILENCE_MIN_SECONDS = 0.3
SAMPLE_RATE = 16000
# 中文口播约 5.5–6 字/秒；低于这个值基本就是漏片或被截断，而不是语速慢
MIN_SANE_DENSITY = 5.0


def _run(cmd: list[str]) -> bytes:
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 失败: {p.stderr.decode(errors='replace').strip()[:400]}")
    return p.stdout


def duration(path: str) -> float:
    out = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration", "-of", "csv=p=0", path,
    ])
    return float(out.decode().strip())


def detect_silences(
    path: str, noise_db: int = SILENCE_DB, min_seconds: float = SILENCE_MIN_SECONDS
) -> list[float]:
    """返回静音区间中点（秒）。注意不能用 -v error，会吞掉 silencedetect 的 INFO 输出。"""
    p = subprocess.run(
        ["ffmpeg", "-i", path,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    return sorted((s + e) / 2 for s, e in zip(starts, ends))


def plan_cuts(
    total: float,
    silences: list[float],
    target: float = TARGET_SECONDS,
    window: float = WINDOW_SECONDS,
) -> list[float]:
    """算出切点。优先吸附到目标切点附近的静音中点，否则退回目标切点本身。"""
    if target + window > MAX_SAFE_SECONDS:
        raise ValueError(
            f"chunk({target}) + window({window}) 超过安全上限 {MAX_SAFE_SECONDS}s，"
            "会导致输出被截断；请调小 --chunk 或 --window"
        )
    cuts: list[float] = []
    prev = 0.0
    while prev + target < total:
        goal = min(prev + target, total)
        lo, hi = goal - window, min(goal + window, total)
        near = [s for s in silences if lo <= s <= hi and s - prev >= window]
        cut = min(near, key=lambda s: abs(s - goal)) if near else goal
        cut = min(cut, prev + target + window)  # 双保险：吸附不得让片长越界
        if cut <= prev:  # 退化保护，避免死循环
            cut = goal
        cuts.append(cut)
        prev = cut
    return [c for c in cuts if 0 < c < total]


def load_pcm(path: str, start: float, end: float) -> array.array:
    """取 [start, end) 音频，转成 16 kHz 单声道 float32 PCM（transcribe-cpp 要的格式）。"""
    raw = _run([
        "ffmpeg", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{max(end - start, 0):.3f}",
        "-i", path, "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ])
    pcm = array.array("f")
    pcm.frombytes(raw)
    return pcm


def transcribe_chunk(session, pcm: array.array, on_truncated=None) -> list[str]:
    """转写一段 PCM；命中输出上限就对半切，两半都转完。"""
    if len(pcm) == 0:
        return []
    try:
        return [session.run(pcm).text]
    except Exception as exc:
        if type(exc).__name__ != "OutputTruncated":
            raise
        if on_truncated:
            on_truncated(len(pcm))
        mid = len(pcm) // 2
        if mid == 0:
            raise
        return transcribe_chunk(session, pcm[:mid], on_truncated) + transcribe_chunk(
            session, pcm[mid:], on_truncated
        )


def transcribe_file(
    path: str,
    model_path: str,
    chunk: float = TARGET_SECONDS,
    window: float = WINDOW_SECONDS,
    use_silence: bool = True,
) -> str:
    from transcribe_cpp import Model

    total = duration(path)
    silences = detect_silences(path) if use_silence else []
    cuts = plan_cuts(total, silences, chunk, window)
    bounds = list(zip([0.0, *cuts], [*cuts, total]))

    print(
        f"  音频 {total:.0f}s · 静音点 {len(silences)} 个 · 切 {len(bounds)} 片"
        f"{'' if use_silence else '（固定切分）'}",
        file=sys.stderr,
    )

    model = Model(model_path)
    session = model.session()
    parts: list[str] = []

    def note(n: int) -> None:
        print(f"    [超限，对半切] {n / SAMPLE_RATE:.0f}s", file=sys.stderr)

    try:
        for i, (a, b) in enumerate(bounds):
            print(f"    [{i:02d}] {a:7.1f}s – {b:7.1f}s  ({b - a:.1f}s)", file=sys.stderr)
            parts += transcribe_chunk(session, load_pcm(path, a, b), note)
    finally:
        model.close()

    text = "\n".join(p for p in parts if p.strip())
    # 自查：中文口播约 5.5–6 字/秒，明显偏低说明漏片或被截断（见 SKILL.md 的完整性校验）
    chars = len(re.sub(r"\s", "", text))
    if total > 0 and chars:
        density = chars / total
        warn = "   ⚠ 偏低，检查是否漏片" if density < MIN_SANE_DENSITY else ""
        print(f"  转写 {chars} 字 · 密度 {density:.2f} 字/秒{warn}", file=sys.stderr)
    return text


def _selftest() -> int:
    class OutputTruncated(Exception):
        pass

    class FakeSession:
        def __init__(self, limit: int) -> None:
            self.limit = limit
            self.seen: list[int] = []

        def run(self, pcm):
            self.seen.append(len(pcm))
            if len(pcm) > self.limit:
                raise OutputTruncated()
            return type("R", (), {"text": f"<{len(pcm)}>"})()

    # --- 对半切：不丢样本、不无限递归、异常不吞吐 ---
    s = FakeSession(100)
    out = transcribe_chunk(s, array.array("f", bytes(400 * 4)))
    assert out == ["<100>"] * 4, out
    assert sum(s.seen) >= 400, f"丢样本了: {sum(s.seen)}"
    assert transcribe_chunk(FakeSession(1000), array.array("f", bytes(50 * 4))) == ["<50>"]
    assert transcribe_chunk(FakeSession(10), array.array("f")) == []
    assert transcribe_chunk(FakeSession(1), array.array("f", bytes(4 * 4))) == ["<1>"] * 4

    class Boom:
        def run(self, pcm):
            raise ValueError("boom")

    try:
        transcribe_chunk(Boom(), array.array("f", bytes(4)))
    except ValueError:
        pass
    else:
        raise AssertionError("非 OutputTruncated 应原样抛出")

    # --- 切点规划 ---
    # 静音点落在目标附近 -> 吸附过去（300s / 60s = 4 个切点，第 4 个附近无静音则回退 242）
    cuts = plan_cuts(300, [58.0, 119.0, 182.0])
    assert cuts == [58.0, 119.0, 182.0, 242.0], cuts
    # 完全无静音 -> 退回固定切点（永不退化）
    assert plan_cuts(300, []) == [60.0, 120.0, 180.0, 240.0], plan_cuts(300, [])
    # 静音太远（超出 window）-> 不吸附
    assert plan_cuts(300, [20.0, 200.0]) == [60.0, 120.0, 180.0, 240.0]
    # 片长硬上限：吸附不得越界
    for c in (plan_cuts(1000, [float(x) for x in range(0, 1000)]),):
        spans = [b - a for a, b in zip([0.0, *c], [*c, 1000])]
        assert max(spans) <= MAX_SAFE_SECONDS, f"片长越界: {max(spans)}"
    # 切点必须严格递增且落在范围内
    cuts = plan_cuts(300, [61.0, 121.0, 181.0])
    assert cuts == sorted(set(cuts)) and all(0 < c < 300 for c in cuts), cuts
    # 目标+窗口超过安全上限 -> 立刻报错，不要静默截断
    try:
        plan_cuts(300, [], target=70, window=12)
    except ValueError:
        pass
    else:
        raise AssertionError("越界的 chunk/window 应报错")

    # --- ffmpeg 字节数 -> float32 样本数 ---
    raw = _run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "anullsrc=r=16000:cl=mono", "-t", "1", "-f", "f32le", "-"])
    pcm = array.array("f")
    pcm.frombytes(raw)
    assert len(pcm) == SAMPLE_RATE, f"期望 {SAMPLE_RATE} 个样本，得到 {len(pcm)}"

    print("selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()

    def opt(name: str, default: float) -> float:
        return float(argv[argv.index(name) + 1]) if name in argv else default

    args = [a for a in argv[1:] if not a.startswith("--")]
    for flag in ("--chunk", "--window"):
        if flag in argv:
            args = [a for a in args if a != argv[argv.index(flag) + 1]]
    if len(args) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    print(transcribe_file(
        args[0], args[1],
        chunk=opt("--chunk", TARGET_SECONDS),
        window=opt("--window", WINDOW_SECONDS),
        use_silence="--fixed" not in argv,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
