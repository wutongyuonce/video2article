#!/usr/bin/env python3
"""本地音频/视频转写，中文和中英混说优先使用 Qwen3-ASR-1.7B。

用法:
    python3 asr.py <audio|video> [model.gguf|MLX模型目录]
        [--chunk 60]    目标片长（秒），默认 60
        [--window 12]   在目标切点前后多少秒内找静音，默认 12
        [--fixed]       关闭静音优先，退回固定时长切分
    python3 asr.py --selftest

依赖: ffmpeg / ffprobe 在 PATH 上；缺少推理库时由 uv 在 Python 3.12 环境中安装。
未指定模型时先查本地 Qwen3-ASR；GGUF 用 transcribe-cpp，MLX 用 mlx-audio。
模型下载由调用方处理，脚本只加载已有本地文件。

脚本按短片段处理长录音，优先把切点放在静音处；模型明确报告输出截断时，
自动对半切片重试。找不到静音时退回固定切点。
"""

from __future__ import annotations

import argparse
import array
from contextlib import contextmanager, redirect_stdout
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

TARGET_SECONDS = 60.0
WINDOW_SECONDS = 12.0
# 静音吸附最多延长到 target+window；限制片长以便输出截断时可快速恢复。
MAX_SAFE_SECONDS = 75.0
SILENCE_DB = -30
SILENCE_MIN_SECONDS = 0.3
SAMPLE_RATE = 16000
# 常见中文口播约 5–6 字/秒；慢速或停顿较多时会更低，密度只作核验提示。
DENSITY_WARNING_THRESHOLD = 5.0
MLX_MAX_TOKENS = 4096


def model_backend(model_path: str) -> str:
    """只接受本地 GGUF 文件或带完整配置与权重的 Qwen3-ASR MLX 目录。"""
    path = Path(model_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"模型不存在: {path}")
    if path.is_file() and path.suffix.lower() == ".gguf":
        with path.open("rb") as f:
            if f.read(4) == b"GGUF":
                return "transcribe-cpp"
        raise ValueError(f"不是有效的 GGUF 文件: {path}")
    if path.is_dir():
        config = json.loads((path / "config.json").read_text())
        if config.get("model_type") != "qwen3_asr":
            raise ValueError(f"不是 Qwen3-ASR 模型目录: {path}")
        required = ["tokenizer_config.json", "preprocessor_config.json"]
        if not (path / "tokenizer.json").is_file():
            required += ["vocab.json", "merges.txt"]
        index = path / "model.safetensors.index.json"
        if index.is_file():
            weights = set(json.loads(index.read_text())["weight_map"].values())
        else:
            weights = {"model.safetensors"}
        if not weights or any(
            not (path / name).is_file() or (path / name).stat().st_size == 0
            for name in [*required, *weights]
        ):
            raise ValueError(f"MLX 模型文件不完整: {path}")
        return "mlx-audio"
    raise ValueError("请传入 .gguf 文件或包含配置、分词器和 .safetensors 权重的 MLX 目录")


def find_local_model(roots=None) -> str:
    """只搜索已知模型目录；显式传模型路径时无需搜索。"""
    if roots is None:
        home = Path.home()
        hf_home = Path(os.environ.get("HF_HOME", home / ".cache/huggingface"))
        roots = [
            Path(os.environ.get("HF_HUB_CACHE", hf_home / "hub")),
            home / "Library/Containers/com.voxt.Voxt/Data/Library/Application Support/Voxt/model-storage/mlx-audio",
            home / "models",
            home / ".lmstudio/models",
            home / ".cache/lm-studio/models",
        ]
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    candidates = {}
    for root in roots:
        if not root.is_dir():
            continue
        # 模型仓库可能位于作者子目录；只深入名称匹配的 Qwen3-ASR 仓库。
        repos = [p for p in root.iterdir()]
        repos += [p for d in list(repos) if d.is_dir() for p in d.iterdir()]
        for repo in repos:
            if "qwen3asr" not in re.sub(r"[^a-z0-9]", "", repo.name.lower()):
                continue
            paths = [repo] if repo.is_file() else [
                *repo.rglob("*.gguf"), *(p.parent for p in repo.rglob("config.json"))
            ]
            for path in paths:
                try:
                    backend = model_backend(str(path))
                except (OSError, ValueError, KeyError) as exc:
                    print(f"  忽略不可用的模型候选 {path}: {exc}", file=sys.stderr)
                    continue
                if backend == "mlx-audio" and not apple_silicon:
                    continue
                candidates[str(path.resolve())] = backend
    if not candidates:
        raise FileNotFoundError("常见目录中未找到可用的本地 Qwen3-ASR；请按 SKILL.md 下载模型或显式传入路径")
    return min(candidates, key=lambda p: (
        "1.7b" not in p.lower(),
        candidates[p] != ("mlx-audio" if apple_silicon else "transcribe-cpp"), p,
    ))


def ensure_backend(backend: str) -> None:
    module = "mlx_audio" if backend == "mlx-audio" else "transcribe_cpp"
    if find_spec(module) is not None:
        return
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError(f"缺少 {backend} 和 uv；请先安装 uv 后重试")
    package = "mlx-audio[stt]==0.5.9" if backend == "mlx-audio" else "transcribe-cpp"
    print(f"  通过 uv 准备 {package}（复用缓存或安装缺少的依赖）", file=sys.stderr)
    raise SystemExit(subprocess.call([
        uv, "run", "--no-project", "--python", "3.12", "--with", package,
        "python", str(Path(__file__).resolve()), *sys.argv[1:],
    ]))


class OutputTruncated(Exception):
    """MLX 命中生成 token 上限，交由共用分片逻辑重试。"""


class MLXSession:
    def __init__(self, model_path: str):
        import mlx.core as mx
        from mlx_audio.stt.utils import load_model

        self.mx = mx
        self.model = load_model(Path(model_path), strict=True)

    def run(self, pcm: array.array):
        result = self.model.generate(
            self.mx.array(pcm.tolist(), dtype=self.mx.float32),
            max_tokens=MLX_MAX_TOKENS, verbose=False,
        )
        if result.generation_tokens >= MLX_MAX_TOKENS:
            raise OutputTruncated("MLX 输出达到 token 上限")
        return result

    def close(self):
        self.model = None
        self.mx.clear_cache()


@contextmanager
def open_session(model_path: str, backend: str):
    if backend == "transcribe-cpp":
        from transcribe_cpp import Model

        with Model(model_path) as model:
            yield model.session()
    else:
        session = MLXSession(model_path)
        try:
            yield session
        finally:
            session.close()


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
    if not math.isfinite(target) or not math.isfinite(window) or target <= 0 or window < 0:
        raise ValueError("chunk 必须是有限正数，window 必须是有限非负数")
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
    """取 [start, end) 音频，转成两个后端共用的 16 kHz 单声道 float32 PCM。"""
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
        if not any(cls.__name__ == "OutputTruncated" for cls in type(exc).__mro__):
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
    backend = model_backend(model_path)
    total = duration(path)
    silences = detect_silences(path) if use_silence else []
    cuts = plan_cuts(total, silences, chunk, window)
    bounds = list(zip([0.0, *cuts], [*cuts, total]))

    print(
        f"  音频 {total:.0f}s · 静音点 {len(silences)} 个 · 切 {len(bounds)} 片"
        f"{'' if use_silence else '（固定切分）'}",
        file=sys.stderr,
    )

    parts: list[str] = []

    def note(n: int) -> None:
        print(f"    [超限，对半切] {n / SAMPLE_RATE:.0f}s", file=sys.stderr)

    with redirect_stdout(sys.stderr), open_session(model_path, backend) as session:
        for i, (a, b) in enumerate(bounds):
            print(f"    [{i:02d}] {a:7.1f}s – {b:7.1f}s  ({b - a:.1f}s)", file=sys.stderr)
            parts += transcribe_chunk(session, load_pcm(path, a, b), note)

    text = "\n".join(p for p in parts if p.strip())
    # 密度只提示复核：语速慢、停顿多的音频也可能低于阈值。
    chars = len(re.sub(r"\s", "", text))
    if total > 0 and chars:
        density = chars / total
        warn = (
            "   ⚠ 请核对音频开头和结尾"
            if density < DENSITY_WARNING_THRESHOLD else ""
        )
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

    # --- 本地模型发现和格式判定：复用已有模型，不接受残缺 MLX 目录 ---
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        gguf = root / "Qwen3-ASR-1.7B-Q8_0.gguf"
        gguf.write_bytes(b"GGUF")
        mlx = root / "mlx-community_Qwen3-ASR-1.7B-8bit"
        mlx.mkdir()
        (mlx / "config.json").write_text('{"model_type":"qwen3_asr"}')
        for name in ["tokenizer_config.json", "preprocessor_config.json", "tokenizer.json"]:
            (mlx / name).write_text("{}")
        (mlx / "model.safetensors").write_bytes(b"weights")
        assert model_backend(str(gguf)) == "transcribe-cpp"
        assert model_backend(str(mlx)) == "mlx-audio"
        with patch.object(sys, "platform", "darwin"), patch.object(platform, "machine", return_value="arm64"):
            assert find_local_model([root]) == str(mlx.resolve())
        with patch.object(sys, "platform", "linux"):
            assert find_local_model([root]) == str(gguf.resolve())
        (mlx / "model.safetensors.index.json").write_text(
            '{"weight_map":{"weight":"missing-shard.safetensors"}}'
        )
        try:
            model_backend(str(mlx))
        except ValueError:
            pass
        else:
            raise AssertionError("缺少分片权重时不能当作完整模型")
        with TemporaryDirectory() as empty:
            try:
                find_local_model([Path(empty)])
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("没有本地模型时必须明确报错")

    # --- MLX token 上限使用共用二分重试，保留全部音频样本 ---
    from types import SimpleNamespace

    mlx_session = MLXSession.__new__(MLXSession)
    mlx_session.mx = SimpleNamespace(array=lambda values, dtype: values, float32="float32")
    mlx_session.model = SimpleNamespace(generate=lambda audio, **kwargs: SimpleNamespace(
        text=f"<{len(audio)}>", generation_tokens=len(audio),
    ))
    assert transcribe_chunk(
        mlx_session, array.array("f", bytes(MLX_MAX_TOKENS * 2 * 4))
    ) == [f"<{MLX_MAX_TOKENS // 2}>"] * 4

    # 缺依赖才启动 uv，且只安装选中的后端；已有依赖时直接复用。
    with patch(__name__ + ".find_spec", return_value=object()), patch.object(subprocess, "call") as call:
        ensure_backend("mlx-audio")
        call.assert_not_called()
    for backend, package in [("mlx-audio", "mlx-audio[stt]==0.5.9"), ("transcribe-cpp", "transcribe-cpp")]:
        with patch(__name__ + ".find_spec", return_value=None), patch.object(shutil, "which", return_value="uv"), patch.object(subprocess, "call", return_value=0) as call:
            try:
                ensure_backend(backend)
            except SystemExit as exc:
                assert exc.code == 0
            else:
                raise AssertionError("缺依赖时应在 uv 环境中重新运行")
            command = call.call_args.args[0]
            assert command[command.index("--with") + 1] == package
            assert "--no-project" in command

    for target, window in [(0, 12), (-1, 12), (60, -1), (float("nan"), 12)]:
        try:
            plan_cuts(300, [], target, window)
        except ValueError:
            pass
        else:
            raise AssertionError("无效片长或窗口必须在处理音频前报错")

    print("selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--chunk", type=float, default=TARGET_SECONDS)
    parser.add_argument("--window", type=float, default=WINDOW_SECONDS)
    parser.add_argument("--fixed", action="store_true")
    args = parser.parse_args(argv[1:])
    if not Path(args.audio).is_file():
        parser.error(f"音频文件不存在: {args.audio}")
    for command in ("ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            parser.error(f"缺少 {command}；请先安装 FFmpeg")
    model_path = str(Path(args.model).expanduser().resolve()) if args.model else find_local_model()
    backend = model_backend(model_path)
    if backend == "mlx-audio" and not (
        sys.platform == "darwin" and platform.machine() == "arm64"
    ):
        parser.error("MLX 后端需要 Apple Silicon Mac；请使用 GGUF 模型")
    plan_cuts(0, [], args.chunk, args.window)
    print(f"  模型 {model_path}\n  后端 {backend}", file=sys.stderr)
    ensure_backend(backend)
    print(transcribe_file(
        args.audio, model_path,
        chunk=args.chunk, window=args.window, use_silence=not args.fixed,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
