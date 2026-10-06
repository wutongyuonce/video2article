---
name: video2article
description: |
  把一段视频 / 播客重写成「阅读版本」文章（Markdown）：按内容主题分小节、逐节完整展开，
  让读者不打开视频、只看文字就能完整理解内容，读起来像一篇 Blog。
  Reactive: 用户给出链接或视频/单集 ID，并说「重写成文章 / 阅读版 / 视频转文章 / 整理成文字 /
  帮我读一下这个视频 / 这个视频讲了什么(要详细)」。
  转写来源：YouTube 拉字幕；小宇宙取官方逐字稿（scripts/xyz.py）；无字幕来源优先用 Qwen3-ASR 本地转写，也可在 macOS 用 Whisper.cpp CLI。
  不适合：只要一句话摘要或要点列表；只要逐字稿原文（跑 yt-dlp 即可，不必重写）。
---

# 视频 → 阅读版本

把一段视频 / 播客重写成「阅读版本」：按主题分小节、逐节完整展开，让读者只靠阅读就能完整理解内容，像在读一篇 Blog 版文章。

### 输出要求

**1. Metadata**：Title / Author / URL

**2. Overview**：一段话点明核心论题与结论。

**3. 按主题梳理**
- 每节根据内容详细展开，让我不必二次查看视频；每节不少于 500 字。
- 方法 / 框架 / 流程重写成条理清晰的步骤或段落。
- 关键数字、定义、原话如实保留核心词，括号内补注释。

**4. 框架 & 心智模型**：抽象出的 framework / mindset，每个不少于 500 字。

### 风格与限制

- 永远不要高度浓缩。
- 不新增事实；含混表述保持原意并注明不确定性。
- 专有名词保留原文，括号给中文释义（若转录中出现或能直译）。
- 要求类的问题不用体现出来（例如「> 500 字」）。
- 避免段落过长，可拆成多个逻辑段落（bullet points）。

## 第一步：拿到转写

**唯一不可违背的原则：先拿到真实转写，再动笔。** 不允许凭印象或标题推测内容。拿不到就停下来告诉用户，不要用记忆补。

| 来源 | 环境 | 走哪条路 |
|---|---|---|
| YouTube | 任意 | **A. 拉字幕**（首选，与视频时长无关） |
| 小宇宙 | 任意 | **B. 官方逐字稿**（默认；没凭据先取凭据） |
| B 站或其他无字幕来源 | 任意 | **C. Qwen3-ASR 本地转写**（首选；macOS 可用 Whisper.cpp CLI） |

**小宇宙永远先试 B**，只有 B 走不通（用户不愿登录，或重试后仍拿不到稿）才退到 C。

### A. YouTube：拉字幕

优先使用 PATH 中已安装的 `yt-dlp`（例如通过 Homebrew 安装的版本）；只有找不到本机命令时，才通过 `uvx` 临时运行。下面的 shell 函数会按此顺序选择：

```bash
ID=<video_id>; mkdir -p /tmp/v2a-$ID && cd /tmp/v2a-$ID

yt_dl() {
  if command -v yt-dlp >/dev/null 2>&1; then
    command yt-dlp "$@"
  else
    uvx yt-dlp "$@"
  fi
}

# 必须看这一步：人工轨和自动轨分列在两个小节（"Available subtitles" / "Available automatic captions"）
yt_dl --list-subs --skip-download "<URL>"

# 一次只指定一种语言。多语言写法（"en.*,zh.*"）会连续请求多条轨道并触发 HTTP 429
yt_dl --skip-download --write-subs --write-auto-subs \
  --sub-langs "en" --sub-format vtt -o "%(id)s.%(ext)s" "<URL>"
# 中文视频用 zh-Hans / zh-Hant，按上一步列出的轨道名写

python3 "<本 skill 目录>/scripts/vtt2txt.py" $ID.en.vtt > transcript.txt

# 元信息：必须每个字段一次 --print。用 "%(title)s|%(uploader)s|..." 单行分隔在标题含 | 时会错位
yt_dl --skip-download --print "%(title)s" --print "%(uploader)s" \
  --print "%(duration)s" --print "%(webpage_url)s" "<URL>"
```

**人工还是自动，只能从 `--list-subs` 看**——两条轨都写成同一个 `$ID.en.vtt`，光看文件名分不出来。优先人工轨：标点、数字、专名都比自动轨可靠。

自动字幕的两个固有噪声：**数字被写成英文单词**（`28x28` 变 `28x 28`，`3` 变 `three`，可直接还原）；**`[Music]` `[Applause]` 等方括号不是人说的**，除非本身构成内容，否则不写进文章。

yt-dlp 会警告「No supported JavaScript runtime」——拿字幕不受影响，但格式可能不全。真遇到拿不到的情况，装一个 JS runtime（`brew install deno`）再重试。

无字幕轨时的退路：下音频走 C；或用浏览器自动化打开视频页取描述区的 Transcript。

### B. 小宇宙：官方逐字稿（默认路径）

先取平台官方逐字稿，再考虑本地 ASR。它包含时间戳、节目元信息和官方章节，可用章节作为文章大纲；但仍是机器稿，专有名词需要核对。

```bash
python3 "<本 skill 目录>/scripts/xyz.py" "<单集URL|eid>" -o transcript.txt --json transcript.json
python3 "<本 skill 目录>/scripts/xyz.py" --whoami # 检查凭据
```

正文需要本人小宇宙登录态。脚本优先读 `$XYZ_REFRESH_TOKEN`，其次读 `~/.config/xiaoyuzhou/refresh_token`；凭据缺失时先让用户登录并提供 `x-jike-refresh-token` cookie。凭据只保存在本地，不能写入产出文件或提交仓库。

脚本会刷新凭据并重试瞬时的空结果；成功时输出带时间锚点的逐字稿、元信息、章节和覆盖率。重试后仍拿不到稿，再切换到 C 的 Qwen 本地转写，并说明使用了退路。

### C. 音频转写（B 站 / 其他无字幕来源）

先下载音频（B 站优先取音频轨；其他站点按默认格式下载）：

```bash
yt_dl() {
  if command -v yt-dlp >/dev/null 2>&1; then
    command yt-dlp "$@"
  else
    uvx yt-dlp "$@"
  fi
}

# B 站
yt_dl -f 30232 -o "audio.%(ext)s" "<URL>"
# 其他站点
yt_dl -o "audio.%(ext)s" "<URL>"
AUDIO="audio.mp3" # 按实际下载的扩展名填写
```

#### 首选：Qwen3-ASR

中文或中英混说优先 **Qwen3-ASR-1.7B**。

**Pi 中使用 pi-voice：**安装插件和 FFmpeg，在 `/voice-settings` 里选择 Qwen3-ASR；然后让 Pi 调用 `transcribe_file` 工具（它不是 shell 命令）。

```bash
pi install npm:@earendil-works/pi-voice
brew install ffmpeg
```

`transcribe_file` 适合 35 分钟以内的音频；更长音频或非 Pi 环境，使用仓库中的 `scripts/asr.py`。需要 `uv` 和 FFmpeg（macOS 可用 `brew install uv ffmpeg`）：

```bash
# 模型下载到 Hugging Face 默认缓存，可供 Pi Voice 和脚本共用
MODEL=$(uvx --from huggingface_hub hf download -q \
  handy-computer/Qwen3-ASR-1.7B-gguf Qwen3-ASR-1.7B-Q8_0.gguf)

uv run --with transcribe-cpp python \
  "<本 skill 目录>/scripts/asr.py" "$AUDIO" "$MODEL" > transcript.txt
```

脚本会静音优先分片、自动重试被截断片段，并输出转写字数密度。密度低于 5 字/秒只作复核提示：检查音频开头、结尾和片段，不要单凭密度判定漏转。长音频优先用此脚本，不要手工拼切片。

#### 备用：macOS Whisper.cpp CLI

Qwen 不可用时，可用 Whisper 自带 CLI。Homebrew 安装的是运行工具，模型文件需另行下载：

```bash
brew install whisper.cpp ffmpeg
mkdir -p "$HOME/.cache/whisper.cpp"
MODEL="$HOME/.cache/whisper.cpp/ggml-large-v3.bin"
curl -L --fail -o "$MODEL" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin

ffmpeg -i "$AUDIO" -ar 16000 -ac 1 -c:a pcm_s16le audio.wav
whisper-cli -m "$MODEL" -f audio.wav --language zh \
  --prompt "节目主题、主持人姓名和本期专有名词" \
  --output-txt --output-file transcript
```

`--output-file transcript` 会生成 `transcript.txt`。提示词只放简短的背景和专名。

### 转写校验

- **YouTube 字幕**：优先人工字幕；自动字幕中的 `[Music]`、`[Applause]` 等非语音标记不写进正文。
- **小宇宙逐字稿**：检查 `xyz.py` 输出的时间戳覆盖率，低于 90% 时停止并重试或切换 ASR。
- **本地 ASR**：核对音频开头和结尾，确认全文没有提前结束。`asr.py` 的低密度提示只用于触发复核；未确认完整前不要开始写作。

### 专有名词核对

ASR 和平台逐字稿都可能把人名、公司名、术语和数字识别错。用节目标题、简介、章节或可靠来源核对；无法确认的内容保留不确定性，不要猜写。

## 第二步：写作

按「输出要求」组织。切小节时**不要沿用视频的时间轴顺序**，按主题重组；一节讲不完就拆成多节。

## 第三步：交付前自检

- [ ] 转写完整性已验证（B 看覆盖率；C 核对音频开头、结尾，脚本低密度提示仅供复核）
- [ ] 每个小节、每个 framework / mindset 的正文达到 500 字下限（数一下，不要目测）
- [ ] 没有出现转写里找不到的数字、人名、结论
- [ ] 人名、公司名和术语已核对；无法确认的标了「不确定」
- [ ] `[Music]` / `[Silence]` 之类的非语音标记没被写进正文
- [ ] 含混处已标注，而不是被我补成了确定说法
- [ ] 正文里没有自指的要求类句子（「> 500 字」等）
- [ ] 说明了实际走了哪条路径（含是否退到过 ASR）、哪里有缺口

## 交付形式

默认写到 **`~/Desktop/`**（文件名取自视频标题），并在回复里给出完整路径。用户指定了目录就按用户说的；用户要求「直接贴出来」时不出文件。
