---
name: video2article
description: |
  把一段视频 / 播客重写成「阅读版本」文章（Markdown）：按内容主题分小节、逐节完整展开，
  让读者不打开视频、只看文字就能完整理解内容，读起来像一篇 Blog。
  Reactive: 用户给出链接或视频/单集 ID，并说「重写成文章 / 阅读版 / 视频转文章 / 整理成文字 /
  帮我读一下这个视频 / 这个视频讲了什么(要详细)」。
  转写来源：YouTube 拉字幕；小宇宙取官方逐字稿（scripts/xyz.py，需自带 token）；
  B 站等无字幕来源做音频转写（pi 下用 pi-voice，否则 scripts/asr.py）。
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
| B 站或其他无字幕来源 | pi + `@earendil-works/pi-voice` | **C1. 本地转写** |
| B 站或其他无字幕来源 | 非 pi（Codex / Claude Code / Cursor / shell） | **C2. 自建转写** |

**小宇宙永远先试 B**，只有 B 走不通（用户不愿登录，或重试后仍拿不到稿）才退到 C。

### A. YouTube：拉字幕

```bash
ID=<video_id>; mkdir -p /tmp/v2a-$ID && cd /tmp/v2a-$ID

# 必须看这一步：人工轨和自动轨分列在两个小节（"Available subtitles" / "Available automatic captions"）
uvx yt-dlp --list-subs --skip-download "<URL>"

# 一次只指定一种语言。多语言写法（"en.*,zh.*"）会连续请求多条轨道并触发 HTTP 429
uvx yt-dlp --skip-download --write-subs --write-auto-subs \
  --sub-langs "en" --sub-format vtt -o "%(id)s.%(ext)s" "<URL>"
# 中文视频用 zh-Hans / zh-Hant，按上一步列出的轨道名写

python3 "<本 skill 目录>/scripts/vtt2txt.py" $ID.en.vtt > transcript.txt

# 元信息：必须每个字段一次 --print。用 "%(title)s|%(uploader)s|..." 单行分隔在标题含 | 时会错位
uvx yt-dlp --skip-download --print "%(title)s" --print "%(uploader)s" \
  --print "%(duration)s" --print "%(webpage_url)s" "<URL>"
```

**人工还是自动，只能从 `--list-subs` 看**——两条轨都写成同一个 `$ID.en.vtt`，光看文件名分不出来。优先人工轨：标点、数字、专名都比自动轨可靠。

自动字幕的两个固有噪声：**数字被写成英文单词**（`28x28` 变 `28x 28`，`3` 变 `three`，可直接还原）；**`[Music]` `[Applause]` 等方括号不是人说的**，除非本身构成内容，否则不写进文章。

yt-dlp 会警告「No supported JavaScript runtime」——拿字幕不受影响，但格式可能不全。真遇到拿不到的情况，装一个 JS runtime（`brew install deno`）再重试。

无字幕轨时的退路：下音频走 C；或用浏览器自动化打开视频页取描述区的 Transcript。

### B. 小宇宙：官方逐字稿（默认路径）

小宇宙自带逐字稿，不用做本地转写。但它同样是机器稿（接口里写着 `vendorDeclaration: "*文稿由小宇宙自动生成"`），专名照样会错。

优势是决定性的：**无输出上限、自带 `startMs` 时间戳、一次 HTTP 拿全**（实测 4 小时那期单请求 71792 字、覆盖 100%）。所以走这条时，C 里那套分片 / 静音吸附 / 超限对半切全都不需要。

```bash
python3 "<本 skill 目录>/scripts/xyz.py" "<单集URL|eid>" -o transcript.txt --json transcript.json
python3 "<本 skill 目录>/scripts/xyz.py" --whoami      # 只验凭据
```

脚本自处理：凭据续期与回写、`data:null` 重试、CDN 的 App UA、元信息与官方章节、覆盖率统计。stdout 是逐字稿（每约 120s 插 `[HH:MM:SS]` 锚点），stderr 是元信息 / 章节 / 统计。

#### 凭据是这条路的前提

逐字稿正文在小宇宙私有接口后面，**网页和公开 API 只给音频**。凭据没有申请渠道，只能是你自己账号的登录态。查找顺序 `$XYZ_REFRESH_TOKEN` → `~/.config/xiaoyuzhou/refresh_token`；两个都没有就先取：

**首选：用任意浏览器自动化工具自动取**（ego-browser / Playwright / Chrome DevTools MCP 之类都行）

```
1. 打开登录页 https://accounts.xiaoyuzhoufm.com/login —— 登录站在 accounts，不是 www
   （www 根路径是营销落地页，没有登录入口）
2. 切「扫码登录」，把控制权交给用户，让他用手机小宇宙 App 扫码
3. 用户确认后，从浏览器读名为 x-jike-refresh-token 的 cookie
   （CDP Network.getCookies，或工具自己的 cookie / 存储接口）
4. 写 ~/.config/xiaoyuzhou/refresh_token（目录 700、文件 600）
```

用 CDP 或工具的存储接口而不是 `document.cookie`，是因为 cookie 可能是 httpOnly。

**兜底：让用户手动取**（没有浏览器自动化工具时）——F12 → Application / 存储 → Cookies → `x-jike-refresh-token` 复制值（或 Network 里看任意请求的 `Cookie:` 那一行），存到上面那个路径。

**凭据纪律**：只存本地，**绝不写进产出文件或提交仓库**。

别去猜 token 什么时候过期：解出来 payload 只有 `data` / `v` / `iv` / `iat`，**没有 `exp`**，时效由服务端定。`xyz.py` 的做法是每次运行都先刷一次、拿到就用，所以 TTL 不相关。refresh 每次都会换发新的（`iat` 随之更新），回写是为了跟上轮换而不是续命；回写失败只警告不中断（refresh 已成功，逐字稿照取）。用 `$XYZ_REFRESH_TOKEN` 时脚本不落盘。

#### 拿到稿之后

`xyz.py` 会一并给出元信息（标题 / 播客 / 作者 / 时长，Metadata 小节要用）和**官方章节（带时间戳）**——章节不需要凭据就能拿，直接当文章小节的骨架，比从零按主题重组省事。单集页公开的 `.shownotes` 里作者也常放大纲，值得读一遍再动笔。

#### 失败处理

- **`{"data": null}` 是偶发瞬时抖动，不是「这期没稿」**，必须重试（`xyz.py` 内置 3 次退避）。见到 null 就退回 ASR 是错的。
- 重试仍为 null 才当这期不可用，退到 C 做本地转写，并告诉用户走了退路。
- CDN 只认 App UA，浏览器 UA 直接 403。别改 `xyz.py` 里的 `UA_APP`。
- 私有接口是逆向所得，平台改版会失效；个人存档用途，产出别公开转载。

### C. 音频转写（B 站 / 其他无字幕来源）

#### 第 0 步：下音频

```bash
# B 站：字幕基本不能用（没人工轨，自动轨也很烂），走音频；未登录即可下载
uvx yt-dlp -f 30232 -o "bili.%(ext)s" "<URL>"

# 小宇宙（只在 B 走不通时才用）：没有字幕轨，同样下音频
uvx yt-dlp -o "xyz.%(ext)s" "<URL>"
```

小宇宙音频免登录。yt-dlp 没有专用 extractor、走 generic，`--list-subs` 明确回 `has no subtitles`。不想用 yt-dlp 时等价方案：

```bash
curl -s "https://www.xiaoyuzhoufm.com/api/episodes/<eid>" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["enclosure"]["url"])'
```

**yt-dlp 对小宇宙的元数据很烂**：只有 `title` 准，`uploader` 是 `www.xiaoyuzhoufm.com`、`duration` 是 `NA`。Metadata 小节的作者和时长走上面那个 API。

#### C1. pi + pi-voice

**前提：装了 `@earendil-works/pi-voice`，且 ffmpeg 在 PATH 上**（`brew install ffmpeg`）。`transcribe_file` 是该插件注册的工具，**不是系统命令**——上游 `transcribe.cpp` 只发布库文件，不提供 CLI。缺 ffmpeg 会报 `File transcription requires FFmpeg`。

**Qwen 必须分片**：有没有输出上限取决于架构族，不取决于具体模型。窗口式（whisper / sensevoice / parakeet）逐窗独立解码 → 无上限，整个文件直接喂；LLM 式（qwen3 / voxtral / canary / cohere / moss）整段一次连续生成 → 有上限。Qwen3-ASR 实测上限约 **474 字**，与音频长度、与 `n_ctx` 都无关（`n_ctx` 从 8192 调到 262144，输出全是 474 字）。

所以按 **60s 为目标**分片，切点吸附到静音中点：

```bash
# 找可切的静音点。不要加 -v error，会吞掉 silencedetect 的 INFO 输出，误判成「没有静音」
ffmpeg -i <音频> -af silencedetect=noise=-30dB:d=0.3 -f null - 2>&1 | grep silence_

# 按吸附后的切点切段（示例切点 58.6 / 119.1）。不用重采样，transcribe_file 自己处理
ffmpeg -v error -ss 0    -t 58.6 -i <音频> part00.mp3
ffmpeg -v error -ss 58.6 -t 60.5 -i <音频> part01.mp3
```

每段依次喂 `transcribe_file`，把结果拼接。**为什么要吸附**：固定时长硬切会斩断句子，Qwen 会在切口补一个句号，造出原话不存在的假句界（实测硬切「…所以暂时。| 测试不了啊。」vs 静音切「…所以暂时测试不了啊。|」）。找不到静音就退回固定切点，不退化。片长硬上限 75s（75s → 427 字仍安全，90s 就截断——90s 片段实测报 `run output truncated`）。

超限会抛错、不会静默返回半截——某片报错就对半切重试。窗口式模型不用分片。

> 嫌麻烦就直接用 C2 的 `scripts/asr.py`：它把静音优先分片 + 超限对半切全自动了，模型反正已经在 HF 缓存里（pi-voice 下过），pi 环境下跑它也完全可以。

#### C2. 非 pi：自建转写

只需三样公开依赖：Python 3.9+、ffmpeg、`transcribe-cpp`。模型与库和 pi-voice 同一套，可复用其 HuggingFace 缓存。

```bash
# 不加 --local-dir：那会绕开 HF 缓存、在当前目录再落一份 2GB 且路径随 cwd 漂移。
# 默认落 ~/.cache/huggingface/hub（与 pi-voice 同目录，blob 内容寻址，已下过就是 no-op）。
# -q 只输出解析后的绝对路径。
MODEL=$(uvx --from huggingface_hub hf download -q \
  handy-computer/Qwen3-ASR-1.7B-gguf Qwen3-ASR-1.7B-Q8_0.gguf)
# 中文替代：SenseVoiceSmall-gguf（241 MB，无输出上限，但专名明显较差）

# 下音频见「第 0 步」。

# 脚本内建静音优先分片 + 超限对半切重试；可调 --chunk 60 --window 12，--fixed 退回固定切分
uv run --with transcribe-cpp python \
  "<本 skill 目录>/scripts/asr.py" <音频> "$MODEL" > transcript.txt
```

要自己写 Node 的话：`TranscribeModel.load(path)` → `model.transcribe(pcm)`，输入 16 kHz 单声道 float32 PCM。**pi-voice 不检查 `result.truncated` 标志，自己写必须捕 `OutputTruncated` 或查该标志。**

### 完整性校验（两条路都要做）

- **B 路径**看时间戳覆盖率：`xyz.py` 会打印 `覆盖 X%`，低于 90% 自己告警。实测 30 期在 98%–100%（偶尔 >100% 是末尾 `[Silence]` 时间戳超过 duration 字段，不是问题）。
- **C 路径**算字数密度 `转写字数 ÷ 音频秒数`（只数非空白字符）。`asr.py` 跑完会直接把这行打到 stderr，低于 5 字/秒 自己加 ⚠。偏低说明漏片或被截断——停下来重做，**不要拿半截转写去写文章**。

别把两条判据混用：播客实测密度 5.08–9.57 字/秒，比视频口播宽得多，所以密度只适合判断 ASR 是否漏片。

一个交叉验证的例子（同一期 719.8s 播客）：官方逐字稿去标记后 4174 字（5.81 字/秒），本地 ASR 4189 字（5.82 字/秒），**差 0.4%**。两条独立路径互相印证，说明这套判据是可靠的。

### 专有名词必须交叉核对（两条路都要做）

**官方逐字稿也是机器稿**，所以这不是「本地转写的补救措施」，而是两条路一视同仁。错误集中在人名、队名、术语、数字，且多是**同音替换、读起来完全通顺**，不能靠「读得顺」判断对错。实测：

- 本地 ASR 把 UP 主本人的 `泽元` 转成 `泽淵`（出现 4 次）、`GEN.G` → `GENGIE`、`老生常谈` → `老生长长`、`狗熊有话说` → `狗熊软话说`
- 官方逐字稿把 `狗熊有话说` 转成 `狗讯话说`

同一个节目名，两条路各错成一个不同的错法——所以不能因为「官方出的」就免检。同一份转写内部还会自相矛盾（`BO5` 与 `比欧武` 并存）——**发现不一致就是错误的明确信号**。

用标题、简介、评论区、官方章节或搜索核对。核实不了的保留原转写音并标注「不确定」，不要写成一个看着像真的名字。

**官方逐字稿还带 `[Music]` `[Silence]` 这类标记**，和 YouTube 自动字幕同款：不是人说的，除非本身构成内容，否则不写进文章。

## 第二步：写作

按「输出要求」组织。切小节时**不要沿用视频的时间轴顺序**，按主题重组；一节讲不完就拆成多节。

## 第三步：交付前自检

- [ ] 转写完整性已验证（B 看覆盖率，C 看密度）
- [ ] 每个小节、每个 framework / mindset 的正文达到 500 字下限（数一下，不要目测）
- [ ] 没有出现转写里找不到的数字、人名、结论
- [ ] 专有名词已交叉核对（两条路都要）；核实不了的标了「不确定」
- [ ] `[Music]` / `[Silence]` 之类的非语音标记没被写进正文
- [ ] 含混处已标注，而不是被我补成了确定说法
- [ ] 正文里没有自指的要求类句子（「> 500 字」等）
- [ ] 说明了实际走了哪条路径（含是否退到过 ASR）、哪里有缺口

## 交付形式

默认写到 **`~/Desktop/`**（文件名取自视频标题），并在回复里给出完整路径。用户指定了目录就按用户说的；用户要求「直接贴出来」时不出文件。
