#!/usr/bin/env python3
"""把 yt-dlp 下载的 WebVTT 字幕转成纯文本。

用法:
    vtt2txt.py subtitles.en.vtt > transcript.txt
    cat subtitles.en.vtt | vtt2txt.py > transcript.txt
    vtt2txt.py --selftest
"""

import html
import re
import sys

TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s+-->")
TAG = re.compile(r"<[^>]*>")
HEADER = ("WEBVTT", "Kind:", "Language:", "STYLE", "REGION", "NOTE")


def _normalize(line: str) -> str:
    line = html.unescape(TAG.sub("", line.replace("&nbsp;", " ")))
    return re.sub(r"\s+", " ", line).strip()


def _merge(acc: str, cue: str) -> str:
    """把一条 cue 接到已积累文本后，吃掉滚动窗口造成的重叠部分。"""
    if not acc:
        return cue
    if acc.endswith(cue):
        return acc
    # 取 acc 的后缀与 cue 的前缀的最长公共部分
    for k in range(min(len(acc), len(cue)), 3, -1):
        if acc.endswith(cue[:k]):
            return acc + cue[k:]
    return acc + " " + cue


def clean(raw: str) -> str:
    """VTT 文本 -> 去掉时间轴、标签、cue id、滚动重复行的纯文本。"""
    lines = raw.splitlines()
    acc = ""
    cue: list[str] = []
    seen_timestamp = False

    def flush() -> None:
        nonlocal acc, cue
        if cue:
            acc = _merge(acc, " ".join(cue))
        cue = []

    for i, line in enumerate(lines):
        if TIMESTAMP.match(line):
            seen_timestamp = True
            flush()
            continue
        # cue id: 紧跟在时间轴之前的那一行（可能纯数字，也可能带字母）
        if i + 1 < len(lines) and TIMESTAMP.match(lines[i + 1]):
            flush()
            continue
        stripped = _normalize(line)
        if not stripped:
            continue
        if not seen_timestamp and stripped.startswith(HEADER):
            continue
        cue.append(stripped)
    flush()
    return acc.strip()


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        sample = """WEBVTT
Kind: captions
Language: en

1
00:00:01.200 --> 00:00:03.360
All right, so here we are, in front of the
elephants

00:00:03.360 --> 00:00:05.000
in front of the
elephants

00:00:05.318 --> 00:00:07.974
the cool thing about these guys is that they
have really...

00:00:07.974 --> 00:00:12.616
have really...

00:00:12.616 --> 00:00:14.367
really really long trunks

00:00:14.421 --> 00:00:15.733
&amp; that&#39;s cool
"""
        got = clean(sample)
        assert "00:00" not in got, f"时间轴没清掉: {got}"
        assert "WEBVTT" not in got and "Kind:" not in got, f"头部没清掉: {got}"
        assert got.count("elephants") == 1, f"滚动重复没去干净: {got}"
        assert got.count("have really...") == 1, f"滚动重复没去干净: {got}"
        assert "really really long trunks" in got, f"漏掉真实内容: {got}"
        assert "& that's cool" in got, f"实体转义没还原: {got}"
        assert got.startswith("All right"), f"开头错位: {got}"
        assert "  " not in got, f"出现双空格: {got}"
        # 跨 cue 的插入式重叠
        assert clean(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nfoo bar baz\n\n"
            "00:00:01.000 --> 00:00:02.000\nbar baz qux\n"
        ) == "foo bar baz qux", "跨 cue 重叠未处理"
        # 正文里的纯数字不能被当成 cue id 丢掉
        assert clean(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n2024\n\n"
            "00:00:01.000 --> 00:00:02.000\nwas a year\n"
        ) == "2024 was a year", "正文数字被误删"
        assert clean("") == "", "空输入应返回空串"
        print("selftest ok")
        return 0

    if len(argv) > 1 and not argv[1].startswith("-"):
        with open(argv[1], encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    text = clean(raw)
    if not text:
        print("no text extracted", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
