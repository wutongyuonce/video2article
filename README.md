# video2article

**把一段视频或播客重写成「阅读版本」文章。** 一个给 AI Agent 用的 Skill。

丢一个 YouTube 链接、B 站视频或小宇宙单集，产出一篇按主题重组的 Markdown 长文——读者不打开视频，只靠阅读就能完整理解内容。

---

## ⚠️ 免责声明

- 仅供**个人存档 / 学习 / 无障碍**用途。
- **不内置任何账号凭据**（BYO-token）。小宇宙逐字稿需要你自己的登录 token，只存在你本机。
- **不附带任何音频或逐字稿内容**——本仓库只有代码和文档。
- 小宇宙逐字稿接口是**逆向所得**的私有接口，平台改版可能随时失效。请勿高频批量抓取，尊重平台 ToS 与内容版权。
- 产出的文章请勿公开转载。

---

## 它解决什么问题

让 AI 写视频文章，最容易出的问题不是文笔，而是**它没看视频就开始写**。

这个 skill 的核心约束只有一条：

> **先拿到真实转写，再动笔。**

围绕这一条，它把「拿到转写」拆成几条路径，并且给每条路径配了**可判定的完整性校验**——转写不完整就不许进入写作环节。

## 转写路径

| 来源 | 路径 | 备注 |
|---|---|---|
| YouTube | yt-dlp 拉字幕 | 优先人工轨，与视频时长无关 |
| 小宇宙 | 官方逐字稿 API | 默认路径；需 BYO-token，无上限且自带时间戳 |
| B 站 / 其他无字幕来源 | 本地 ASR | 中文优先 Qwen3-ASR；Pi 可用 `@earendil-works/pi-voice`，长音频或非 Pi 环境用 `asr.py`；macOS 可用 Whisper.cpp 备用 |

## 安装

```bash
# pi
git clone https://github.com/wutongyuonce/video2article ~/.pi/agent/skills/video2article

# Claude Code（项目级）
git clone https://github.com/wutongyuonce/video2article .claude/skills/video2article
```

依赖：

- Python 3.9+、`ffmpeg`（`brew install ffmpeg`）
- `uv` / `uvx`（运行 `yt-dlp`、`transcribe-cpp`、`huggingface_hub`，无需预先 pip install）
- 本地 ASR 需要 `ffmpeg` / `ffprobe`；macOS 备用 Whisper CLI：`brew install whisper.cpp ffmpeg`
- 可选：`@earendil-works/pi-voice`（Pi 环境下的本地转写工具）

## 用法

把链接丢给 agent，说「重写成文章」即可。脚本也可以单独跑：

```bash
# 小宇宙官方逐字稿
python3 scripts/xyz.py "<单集URL|eid>" -o transcript.txt --json transcript.json
python3 scripts/xyz.py --whoami              # 只验凭据

# 本地 ASR：中文优先 Qwen3-ASR；长音频自动静音优先分片，截断片段会重试
AUDIO="audio.mp3" # 按实际下载的文件名填写
MODEL=$(uvx --from huggingface_hub hf download -q \
  handy-computer/Qwen3-ASR-1.7B-gguf Qwen3-ASR-1.7B-Q8_0.gguf)
uv run --with transcribe-cpp python scripts/asr.py "$AUDIO" "$MODEL" > transcript.txt

# macOS 备用：Whisper.cpp CLI（先 brew install whisper.cpp ffmpeg，模型需另行下载）
mkdir -p "$HOME/.cache/whisper.cpp"
MODEL="$HOME/.cache/whisper.cpp/ggml-large-v3.bin"
curl -L --fail -o "$MODEL" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
ffmpeg -i "$AUDIO" -ar 16000 -ac 1 -c:a pcm_s16le audio.wav
whisper-cli -m "$MODEL" -f audio.wav --language zh \
  --output-txt --output-file transcript

# WebVTT 字幕转纯文本（合并滚动窗口重复行）
python3 scripts/vtt2txt.py subtitles.en.vtt > transcript.txt
```

三个脚本都带离线自检：

```bash
python3 scripts/xyz.py --selftest
python3 scripts/asr.py --selftest
python3 scripts/vtt2txt.py --selftest
```

`asr.py` 的低于 5 字/秒提示只用于触发复核，不单独证明漏转；开始写作前仍须核对音频首尾和专有名词。

## 小宇宙凭据

逐字稿正文在小宇宙私有接口后面，网页和公开 API 只给音频。凭据没有申请渠道，只能是你自己账号的登录态，按顺序查 `$XYZ_REFRESH_TOKEN` → `~/.config/xiaoyuzhou/refresh_token`。

取的办法：登录站是 **`accounts.xiaoyuzhoufm.com`**（不是 `www`，那是个营销落地页），扫码登录后从浏览器 cookie 里读出 `x-jike-refresh-token`。用任意浏览器自动化工具（ego-browser / Playwright / Chrome DevTools MCP）都能做，手动 F12 也行。完整步骤见 [SKILL.md](./SKILL.md)。

凭据只存本地、绝不入仓库，脚本续期后原子回写。

## 为什么这么设计（都是实测）

设计取舍不是猜的，下面每条都在真机上验证过：

| 结论 | 依据 |
|---|---|
| Qwen3-ASR 必须分片 | 实测输出上限约 **474 字**，与音频长度、与 `n_ctx` 都无关（8192→262144 输出全是 474 字）；75s 片段正常，**90s 报 `run output truncated`**。当前 `asr.py` 默认约 60s 一片，优先吸附静音，截断时再递归二分重试 |
| 分片要吸附静音 | 固定时长硬切会斩断句子，模型在切口补一个句号，**造出原话不存在的假句界** |
| 小宇宙官方稿无需分片 | 4 小时单集，**一次请求 71792 字、覆盖 100%** |
| 逐字稿 CDN 只认 App UA | 浏览器 UA 直接 **403 Forbidden** |
| `{"data": null}` 不是「这期没稿」 | 一轮 30 期里 3 期返回 null，同一期连打 15 次全过，复测 30 期首次即成功——**必须重试** |
| 官方逐字稿也是机器稿 | 接口标注 `vendorDeclaration: "*文稿由小宇宙自动生成"` |
| 两条路径互相印证 | 同一期 719.8s 播客：官方稿去标记后 **4174 字**（5.81 字/秒），本地 ASR **4189 字**（5.82 字/秒），**差 0.4%** |
| 完整性判据要分开 | 播客实测密度 **5.08–9.57 字/秒**，比视频口播宽得多，不能用一个阈值判断所有来源；脚本低于 5 字/秒只提示人工复核，完整性仍靠首尾和片段核对 |

还有一条来自踩坑：小宇宙官方 shownotes 里时间戳和标题隔着内联标签（`<a class="timestamp">00:02:35</a> 标题`），**把所有 HTML 标签都换成换行会把两者拆开**，把 `00:02:35 英伟达的赌注与边界` 错切成 `ts=00:02 / title=35`。只有块级标签该换行。

## 目录结构

```
video2article/
├── SKILL.md              # skill 定义：路径选择、判据、失败处理
├── README.md
├── LICENSE
└── scripts/
    ├── xyz.py            # 小宇宙官方逐字稿：续期 / 重试 / CDN / 元信息 / 官方章节
    ├── asr.py            # Qwen 本地 ASR：静音优先分片 + 截断重试 + 密度复核提示
    └── vtt2txt.py        # WebVTT -> 纯文本
```

## 参考

小宇宙逐字稿接口的发现来自 [hesorchen/xiaoyuzhou-juicer](https://github.com/hesorchen/xiaoyuzhou-juicer)。本仓库的调用链在其基础上做了端到端验证，并修正了两处与实测不符的说法（refresh token 轮换是否强制作废旧 token、刷新端点是否唯一）。

## License

[MIT](./LICENSE)
