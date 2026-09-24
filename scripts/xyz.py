#!/usr/bin/env python3
"""取小宇宙官方逐字稿：refresh -> transcriptUrl -> 逐字稿。

用法:
    python3 xyz.py <单集URL|eid> [-o 文件] [--json 文件] [--token-file 路径]
    python3 xyz.py --whoami          # 只验证凭据能不能用
    python3 xyz.py --selftest

默认把逐字稿打到 stdout（每约 120s 插一个 [HH:MM:SS] 锚点），元信息与统计打到
stderr。--json 额外存下 {meta, segments} 原始结构供程序消费。

凭据: 依次找 $XYZ_REFRESH_TOKEN、~/.config/xiaoyuzhou/refresh_token。
      都没有就报错退出（拿凭据的办法见 SKILL.md，本脚本不做登录）。续期成功后
      会用响应头里的新 token 原子回写，所以凭据文件必须可写。

为什么必须有凭据: 逐字稿正文在小宇宙私有接口后面，网页和公开 API 只给音频。
没有凭据时退路是下音频做本地转写（SKILL.md 的 B / C 路径），不要用记忆补内容。

相对本地 ASR 的收益（均为实测）: 无输出上限（4 小时单请求 71792 字、覆盖 100%），
thus 不需要分片；自带 startMs 时间戳。代价: 官方稿也是机器稿
（vendorDeclaration = "*文稿由小宇宙自动生成"），同音错字照样要交叉核对。

两个实测得出的硬要求:
  1. CDN 校验 App UA，浏览器 UA 直接 403。
  2. {"data": null} 是瞬时抖动而非"这期没稿"，必须重试（实测一轮 27/30，
     复测 30/30），不能一见到 null 就判定无稿。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.xiaoyuzhoufm.com"
WEB_BASE = "https://www.xiaoyuzhoufm.com"
REFRESH_URL = f"{API_BASE}/app_auth_tokens.refresh"
TRANSCRIPT_URL = f"{API_BASE}/v1/episode-transcript/get"

UA_WEB = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
# CDN 白名单要求的 App UA（改成普通 UA 会 403）
UA_APP = "Xiaoyuzhou/2.7.0 (build:1234; iOS 17.0.0)"

DEFAULT_TOKEN_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", "").strip()
    or os.path.join(os.path.expanduser("~"), ".config"),
    "xiaoyuzhou",
    "refresh_token",
)

RETRIES = 3
BACKOFF = 0.5  # 秒；第 n 次重试前等 BACKOFF * 2**(n-1)
ANCHOR_MS = 120_000
# 实测密度 5.08–9.57 字/秒，比视频口播（5.5–6）宽得多，所以只作软提示不作判据
DENSITY_SANE = 3.0


def app_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "x-jike-device-id": "00000000-0000-0000-0000-000000000000",
        "x-jike-app-version": "2.7.0",
        "User-Agent": UA_APP,
        "Content-Type": "application/json",
    }
    h.update(extra or {})
    return h


def mask(value: str | None) -> str:
    return f"<{len(value)}字符 {value[:12]}…>" if value else "<空>"


def _request(url: str, headers: dict[str, str], data: bytes | None = None):
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read()
            if body.startswith(b"\x1f\x8b"):
                import gzip

                body = gzip.decompress(body)
            low = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, low, body
    except urllib.error.HTTPError as exc:
        low = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return exc.code, low, exc.read()


def read_refresh_token(explicit: str | None = None) -> str:
    env = os.environ.get("XYZ_REFRESH_TOKEN", "").strip()
    if env:
        return env
    path = explicit or DEFAULT_TOKEN_FILE
    if not os.path.exists(path):
        raise SystemExit(
            f"没有凭据：{path} 不存在，$XYZ_REFRESH_TOKEN 也没设。\n"
            "拿凭据的办法见 SKILL.md「附：小宇宙官方逐字稿」。"
        )
    token = open(path, encoding="utf-8").read().strip()
    if not token:
        raise SystemExit(f"凭据文件是空的：{path}")
    return token


def save_refresh_token(token: str, explicit: str | None = None) -> bool:
    """原子回写，权限 0600。回写不是硬要求（实测旧 token 刷新后仍可用），
    但轮换确实发生，存最新值最稳。

    写不进去只警告不抛：refresh 已经成功，逐字稿还能取，不该因为凭据文件只读
    （比如用户 chmod 400）就把整件事搞挂。
    """
    path = explicit or DEFAULT_TOKEN_FILE
    if os.environ.get("XYZ_REFRESH_TOKEN", "").strip():
        return True  # 用户用环境变量管，不替他落盘
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            fh.write(token)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(
            f"  ⚠ 新 refresh token 回写失败（{exc}）——本次不影响，但下次可能要重取凭据",
            file=sys.stderr,
        )
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def refresh_access(refresh_token: str) -> tuple[str, str | None]:
    """换一枚 access token。返回 (access, new_refresh)；小宇宙每次都换发新 refresh。"""
    status, headers, body = _request(
        REFRESH_URL, app_headers({"x-jike-refresh-token": refresh_token}), b""
    )
    if status != 200:
        raise SystemExit(
            f"续期失败 HTTP {status}：{body[:200]!r}\n"
            "凭据可能已失效，重新登录拿一枚（见 SKILL.md）。"
        )
    access = headers.get("x-jike-access-token")
    if not access:
        raise SystemExit(f"续期响应没有 x-jike-access-token；响应头：{sorted(headers)}")
    return access, headers.get("x-jike-refresh-token")


def parse_eid(raw: str) -> str:
    m = re.search(r"/episode/([0-9a-fA-F]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{16,}", raw.strip()):
        return raw.strip()
    raise SystemExit(f"无法从 {raw!r} 解析 eid")


BLOCK_TAG = re.compile(
    r"</?(?:p|div|li|ul|ol|br|tr|td|th|table|h[1-6]|blockquote|section)\b[^>]*>", re.I
)
TIMESTAMP_LINE = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)\s*[、.．:：-]?\s*(.*)$")


def extract_chapters(shownotes_html: str) -> list[dict[str, str]]:
    """从 shownotes 里抽官方章节（带时间戳的行）。这些不需要凭据，可当文章骨架。

    关键是只能把块级标签换成换行：官方 shownotes 里时间戳和标题隔着内联标签
    （`<a class="timestamp">00:02:35</a> 标题`），全部标签都换行会把两者拆开，
    逼出 `ts=00:02 / title=35` 这种错切。标题也可能在时间戳的下一行，所以
    裸时间戳行要往下取一行当标题。
    """
    if not shownotes_html:
        return []
    text = BLOCK_TAG.sub("\n", shownotes_html)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)

    lines = [ln.strip() for ln in text.splitlines()]
    chapters: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        m = TIMESTAMP_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        ts, title = m.group(1), m.group(2).strip()
        if not title:  # 标题被换行隔在下一行
            for nxt in lines[i + 1:]:
                if nxt and not TIMESTAMP_LINE.match(nxt):
                    title = nxt
                    break
                if nxt:
                    break
        if title:
            chapters.append({"ts": ts, "title": title})
        i += 1
    return chapters


def find_episode(eid: str) -> dict:
    """从单集页 __NEXT_DATA__ 拿元信息与 transcriptMediaId（免凭据）。"""
    status, _, body = _request(f"{WEB_BASE}/episode/{eid}", {"User-Agent": UA_WEB})
    if status != 200:
        raise SystemExit(f"取单集页失败 HTTP {status}")
    html = body.decode("utf-8", "replace")
    start = html.find('id="__NEXT_DATA__"')
    if start < 0:
        raise SystemExit("单集页里没有 __NEXT_DATA__，页面结构可能变了")
    data = json.loads(html[html.index(">", start) + 1: html.index("</script>", start)])

    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "EPISODE" and node.get("eid") == eid:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not found:
        raise SystemExit(f"单集页里没找到 eid={eid} 的 EPISODE 节点")
    ep = found[0]
    podcast = ep.get("podcast") or {}
    return {
        "eid": eid,
        "url": f"{WEB_BASE}/episode/{eid}",
        "title": ep.get("title") or "",
        "podcast": podcast.get("title") or "",
        "author": podcast.get("author") or "",
        "duration": ep.get("duration") or 0,
        "pubDate": ep.get("pubDate") or "",
        "mediaId": (ep.get("transcript") or {}).get("mediaId") or ep.get("transcriptMediaId") or "",
        "chapters": extract_chapters(ep.get("shownotes") or ""),
    }


def fetch_transcript(eid: str, media_id: str, access: str, on_note=None) -> list[dict]:
    """取逐字稿。对 {"data": null} 重试——实测那是瞬时抖动，不是「这期没稿」。"""
    if not media_id:
        raise SystemExit("这期没有 transcriptMediaId，无法取逐字稿")
    payload = json.dumps({"mediaId": media_id, "eid": eid}).encode()
    last = ""
    for attempt in range(RETRIES):
        status, _, body = _request(
            TRANSCRIPT_URL, app_headers({"x-jike-access-token": access}), payload
        )
        if status == 200:
            try:
                data = (json.loads(body) or {}).get("data") or {}
            except ValueError:
                data = {}
            turl = data.get("transcriptUrl")
            if turl:
                if on_note and data.get("vendorDeclaration"):
                    on_note(f"  来源声明: {data['vendorDeclaration']}")
                return _load_segments(turl)
            last = f'data={json.dumps(data, ensure_ascii=False)[:120]}'
        else:
            last = f"HTTP {status} {body[:120]!r}"
        if attempt < RETRIES - 1:
            wait = BACKOFF * 2 ** attempt
            if on_note:
                on_note(f"  第 {attempt + 1} 次未拿到（{last}），{wait:.1f}s 后重试")
            time.sleep(wait)
    raise SystemExit(
        f"{RETRIES} 次都没拿到逐字稿（{last}）。\n"
        "仍为 null 时应视为这期不可用，退回下音频做本地转写（SKILL.md 的 B / C 路径）。"
    )


def _load_segments(transcript_url: str) -> list[dict]:
    for attempt in range(RETRIES):
        # App UA 是硬要求：浏览器 UA 拿到的是一张 403 页面
        status, _, body = _request(transcript_url, {"User-Agent": UA_APP})
        if status == 200:
            segs = json.loads(body.decode("utf-8", "replace"))
            return [
                s for s in segs
                if isinstance(s, dict) and str(s.get("text", "")).strip()
            ]
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF * 2 ** attempt)
    raise SystemExit(f"取逐字稿 CDN 失败 HTTP {status}（检查是否被换掉了 App UA）")


def hhmmss(ms: int) -> str:
    total = max(int(ms), 0) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def render(segments: list[dict], anchor_ms: int = ANCHOR_MS) -> str:
    """每约 anchor_ms 插一个锚点。不按段换行——逐字稿要当连续文本读。"""
    out: list[str] = []
    next_anchor = 0
    for seg in segments:
        ms = int(seg.get("startMs") or 0)
        if ms >= next_anchor:
            out.append(f"\n\n**[{hhmmss(ms)}]** ")
            next_anchor = ms + anchor_ms
        out.append(str(seg["text"]).strip())
    text = "".join(out).strip()
    return re.sub(r"[ \t]+", " ", text)


def _selftest() -> int:
    assert parse_eid("https://www.xiaoyuzhoufm.com/episode/6a7cbeb017676351c5710266") == \
        "6a7cbeb017676351c5710266"
    assert parse_eid("6a7cbeb017676351c5710266") == "6a7cbeb017676351c5710266"
    for bad in ("", "https://example.com/x", "abc"):
        try:
            parse_eid(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"{bad!r} 应被拒")

    assert hhmmss(0) == "00:00:00"
    assert hhmmss(12967440) == "03:36:07"
    assert hhmmss(-5) == "00:00:00"

    chapters = extract_chapters(
        "<p>OUTLINE:</p><p>00:02:35 英伟达的赌注与边界</p>"
        "<p>01:02:46 如何在英伟达拿到GPU</p><p>随便一句没有时间戳的话</p>"
    )
    assert chapters == [
        {"ts": "00:02:35", "title": "英伟达的赌注与边界"},
        {"ts": "01:02:46", "title": "如何在英伟达拿到GPU"},
    ], chapters
    assert extract_chapters("") == []

    # 官方真实结构：时间戳在 <a class="timestamp"> 里，标题在同一行的内联标签之后。
    # 把所有标签都换成换行会退化成 ts=00:02 / title=35。
    real = (
        '<p><span><a class="timestamp" data-timestamp="155">00:02:35</a> 英伟达的赌注与边界</span></p>'
        '<p><span><a class="timestamp" data-timestamp="1797">00:29:57</a> 意外、顿悟、转变</span></p>'
    )
    assert extract_chapters(real) == [
        {"ts": "00:02:35", "title": "英伟达的赌注与边界"},
        {"ts": "00:29:57", "title": "意外、顿悟、转变"},
    ], extract_chapters(real)

    # 标题被 <br> 隔到下一行时也要接上，且不能把下一个时间戳当标题
    assert extract_chapters("<p>00:02:35<br>英伟达的赌注</p><p>00:29:57<br>顿悟</p>") == [
        {"ts": "00:02:35", "title": "英伟达的赌注"},
        {"ts": "00:29:57", "title": "顿悟"},
    ]
    # 裸时间戳后面没有任何标题 -> 不产出空标题章节
    assert extract_chapters("<p>00:02:35</p><p>00:29:57</p>") == []

    segs = [
        {"text": "第一句。", "startMs": 0},
        {"text": "第二句。", "startMs": 30_000},
        {"text": "  ", "startMs": 60_000},
        {"text": "第三句。", "startMs": 200_000},
    ]
    got = render([s for s in segs if str(s.get("text", "")).strip()])
    assert got.startswith("**[00:00:00]**"), got
    assert "**[00:03:20]**" in got, got           # 200s 处应新开锚点
    assert got.count("**[") == 2, got            # 30s 不新开
    assert "第三句。" in got and "  " not in got

    # 元信息缺失也不能炸
    assert render([]) == ""

    # 凭据文件只读时回写失败不能抛（refresh 已成功，逐字稿还该照取）
    import contextlib
    import io as _io
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("XYZ_REFRESH_TOKEN", None)
        locked = os.path.join(d, "ro")
        os.makedirs(locked, mode=0o500)
        with contextlib.redirect_stderr(_io.StringIO()):  # 警告是预期的，别吓到跑自检的人
            assert save_refresh_token("x", os.path.join(locked, "t")) is False
    print("selftest ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True, description="取小宇宙官方逐字稿")
    ap.add_argument("target", nargs="?", help="单集 URL 或 eid")
    ap.add_argument("-o", "--out", help="写到文件（默认 stdout）")
    ap.add_argument("--json", dest="json_out", help="额外存 {meta, segments} 原始结构")
    ap.add_argument("--token-file", help=f"凭据文件（默认 {DEFAULT_TOKEN_FILE}）")
    ap.add_argument("--whoami", action="store_true", help="只验证凭据")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.target and not args.whoami:
        ap.error("需要 <单集URL|eid>，或 --whoami")
    # 先做纯解析，参数错了就别去刷新凭据
    eid = parse_eid(args.target) if args.target else None

    refresh = read_refresh_token(args.token_file)
    access, new_refresh = refresh_access(refresh)
    if new_refresh and new_refresh != refresh:
        save_refresh_token(new_refresh, args.token_file)
    print(f"  凭据刷新完成 access={mask(access)} refresh={mask(new_refresh)}", file=sys.stderr)
    if args.whoami:
        print("凭据可用。", file=sys.stderr)
        return 0

    meta = find_episode(eid)
    print(
        f"  {meta['title']}\n"
        f"  {meta['podcast']} / {meta['author']} · {meta['duration']}s\n"
        f"  官方章节 {len(meta['chapters'])} 个 · mediaId {mask(meta['mediaId'])}",
        file=sys.stderr,
    )
    for chapter in meta["chapters"]:
        print(f"    {chapter['ts']}  {chapter['title']}", file=sys.stderr)

    segments = fetch_transcript(eid, meta["mediaId"], access,
                                on_note=lambda m: print(m, file=sys.stderr))
    chars = sum(len(str(s["text"])) for s in segments)
    last_ms = max((int(s.get("startMs") or 0) for s in segments), default=0)
    seconds = last_ms / 1000 or 1
    coverage = last_ms / 1000 / meta["duration"] if meta["duration"] else 0
    density = chars / seconds
    print(
        f"  {len(segments)} 段 · {chars} 字 · 末尾 {hhmmss(last_ms)} · "
        f"覆盖 {coverage:.0%} · 密度 {density:.2f} 字/秒",
        file=sys.stderr,
    )
    # 实测播客密度 5.08–9.57，所以这里只提示明显异常，不判定截断
    if coverage and coverage < 0.9:
        print(f"  ⚠ 覆盖率只有 {coverage:.0%}，逐字稿可能不完整", file=sys.stderr)
    if density < DENSITY_SANE:
        print(f"  ⚠ 密度 {density:.2f} 偏低，逐字稿可能漏段", file=sys.stderr)

    text = render(segments)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"  已写入 {args.out}", file=sys.stderr)
    else:
        print(text)
    if args.json_out:
        payload = {"meta": {**meta, "stats": {
            "segments": len(segments), "chars": chars,
            "lastMs": last_ms, "coverage": round(coverage, 4),
            "density": round(density, 3)}}, "segments": segments}
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"  已写入 {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
