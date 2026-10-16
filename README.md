# maibot-sing

让 MaiBot 用管理员配置的 RVC 音色翻唱已有歌曲：搜索准确来源、分离人声、保留原旋律转换，并把成品长期保存在可浏览音频库。

本项目是 [xiaocutedog/maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main) 的 [QR-0W fork](https://github.com/QR-0W/maibot_sing-main)。**0.6.1 为有界上下文更新**；生产应用与重载只在用户授权后进行。源码、隔离测试与成功加载不等于真实媒体、生产 QQ 或音质验收；不会向 QQ 试发回归音频。

[使用](#使用) · [配置](#配置) · [运行边界](#运行边界) · [文档](#文档) · [开发](#开发与验证)

## 为什么使用它

- **正常说话，也能请求翻唱。** 自然聊天由 LLM 调用格式化 `cover_song` Tool；确定性操作仍可用 `/翻唱`。
- **三维独立选择。** 整曲或片段、有无伴奏、文件或语音条互不捆绑，用户请求不需要再加 `--auto-reply`。
- **任务不占整曲 RPC。** SQLite 记录接纳、候选、排队、阶段与取消；接纳返回任务 ID，渲染与投递状态分开。
- **内存额度看得见。** 默认媒体硬限 2048 MiB，整个独立服务和子进程受限；余量不足明确失败，不偷降预算或走旧后端。
- **成品找得到。** 永久 MP3、真实来源与配方 metadata、可浏览硬链接和索引；不会自动删除用户音频。
- **不重复试发。** 音频和失败文本分别持久 claim，只有明确平台回执及消息 ID 才确认送达，未知结果不重发。

这些是实现设计，不是角色身份、音质提升、素材权利或保证送达的承诺。

## 安装与审阅

遵循 [MaiBot 官方插件指南](https://docs.mai-mai.org/plugin/vibe-coding)，在 MaiBot 的 `plugins/` 下保留独立仓库，不向核心目录复制源码。下例仅展示仓库布局，不授权现网安装或启用：

```bash
git clone https://github.com/QR-0W/maibot_sing-main.git /path/to/MaiBot/plugins/maibot-sing
```

先检查 [Manifest](<_manifest.json>)、[配置契约](<docs/CONFIG_CONTRACT.md>)与[部署验收](<docs/DEPLOYMENT.md>)。当前最低 Host 1.3.0 / SDK 2.8.2；没有验证整个声明版本范围。插件默认禁用。

模型、索引、HuBERT、本地 Demucs repo、受审查 RVC wrapper 和独立推理环境由管理员提供，不随插件分发，不自动下载权重或读全局模型缓存。不能为插件修改 MaiBot 核心依赖。

## 使用

### 正常聊天

```text
你能唱一下朴树的生如夏花吗？
```

MaiCore/LLM 发现并调用 `cover_song(title="生如夏花", artist="朴树")`。不知道曲目或艺人应先询问，不编造；想听原唱时应使用播放功能，不误触翻唱。Command 不再抢先拦截自然句式，Tool 不强制提升为核心工具。

### 斜杠命令

```text
/翻唱 生如夏花 - 朴树
/翻唱 生如夏花 - 朴树 --excerpt --without-instrumental --voice
/翻唱选择 <任务ID> <序号>
/翻唱状态 <任务ID>
/翻唱取消 <任务ID>
```

| 入口 | 长度默认 | 伴奏默认 | 投递默认 |
|---|---|---|---|
| `/翻唱 歌名 - 艺人` | full 整曲 | 有 | file 文件 |
| `cover_song` Tool | excerpt，12–18 秒 | 干声 | voice 语音条 |

长度 `--full/--excerpt`、伴奏 `--with-instrumental/--without-instrumental`、格式 `--file/--voice` 独立覆盖，每个维度最多一次，重复/矛盾/未知选项拒绝。Tool 对应 `render_mode/instrumental/delivery_mode` 参数；两者可用专辑和准确来源 ID 缩小候选，Command 另有固定音色别名 `-v`。

**请求翻唱就是授权完成后最多一次投递，无需 `--auto-reply`。** 旧 flag 仅作兼容；历史没有许可的任务不会被升级。Tool自动冻结准确过滤后的首项（可含Live），首项阻塞时拒绝，不跳到下一项；Command仍可显式选择版本，不按时长猜studio。

Tool 返回 `success/job_id/state/code/content`，必要时附候选列表；这是接纳/状态，不是整曲已完成。`cover_song_status(job_id)` 限本会话当前用户读取。活动 Tool 任务复用，终态更新时间后的 60 秒内重放复用，之后可以重新请求同一首歌；Command 保持原消息 ID 去重。

其他可见入口：固定音色列表、仅 operator 的 QQ/网易云扫码登录与状态检查。`/163cookie` 不应用聊天携带的秘密。**`/说` 与 `speak_voice` 暂不支持**：TTS/旧后处理没有进入当前受限链路，因此不合成、不读取参考音频、不回退旧 sidecar。

## 配置

[入口](<plugin.py>)的 `config_model` 是结构/默认值来源；Runner 管理运行配置。[示例](<config.example.toml>)只供审阅，不能复制覆盖真实配置或提交其中的凭证。

```toml
[local]
memory_max_mb = 2048       # MiB，不是十进制 MB
memory_headroom_mb = 512
cpu_quota_percent = 150
media_tasks_max = 64
```

`MemoryHigh` 自动取 75%（默认 1536 MiB），swap 固定为 0。管理员还须填写模型/索引/HuBERT/Demucs、wrapper/upstream、worker Python 与共享锁等实际绝对路径；默认不包含开发者机器路径。当前旧 RVC/TTS 兼容配置在 UI 明确标为退休，仅固定模型显示名仍使用 `rvc.default_model`。

永久输出默认是 SDK 数据目录中的 `covers/`；覆盖值须为非符号链接绝对目录，首次绑定后不能靠热更搬迁。配置细节及迁移边界见[配置契约](<docs/CONFIG_CONTRACT.md>)。

## 架构与运行边界

```text
LLM Tool 参数校验 ─┐
                   ├→ JobService 搜索/选曲/持久接纳
/翻唱 原文解析 ────┘              ↓
                     StageCoordinator → 有限媒体服务
                              ↓
                     字节收据 → 永久产物 → 单次 outbox
```

所有可达媒体，包括来源探测、解码、分离、转换、混音、编码和校验，共用独立单元预算与阶段所有权；SDK Runner 不加载模型或读取完整音频。服务内部取得共享 inference lock，模型工作严格串行。启动前检查真实有限祖先 cgroup，锁内、模型加载前再查；不能从宿主 MemTotal 猜容器容量。

预算不足/不可验证就明确拒绝，不启动媒体。`KillMode=control-group` 与 `OOMPolicy=kill` 终止整个自有服务；只有真实 `oom-kill` 证据才报告 OOM，SIGKILL 不冒充 OOM。前一 unit 与子进程未证明停止前不释放槽；保留成功收据和字节，不发布或发送半成品，不自动重试。

本策略不能保证其它服务增长时全容器永不 OOM，也不会调整它们。音频与失败通知都最多尝试一次，不保证恰好送达；claim 后崩溃可能零次。发送超时、布尔回执或缺消息 ID 为 unknown，重启不重发。

Command 检查当前 NapCat/Host 直接文本、原消息与路由，拒绝引用、转发、语音/ASR、@、notify 和虚拟 QQ。Tool 按用户接受的 SDK/MaiCore/LLM 会话信任工作，一致 QQ 用户/stream/chat 检查不等于原消息强锚点或密码学签名；本分支不修改核心，也不自行计算聊天流 ID。

## 音频与已知限制

- 所有片段请求统一先完整受控流式计帧，冻结唯一中心原音频上下文（至多90秒），只分离此上下文，再以RMS/flatness选12–18秒，最多两段按原序拼接，仅一次RVC。完整来源允许30–900秒；full仍为30–配置上限且最多300秒。不是副歌识别或语义乐句检测，不扫描多窗口、不回退整曲。
- 保留原音高；`sing-render-v4`配方冻结真实资产、helper/selector hash、完整源帧数、上下文及全部实际argv。selection-v2使用原来源绝对帧；旧v2/v3完成成品只读兼容，不续跑旧计划。目录毫秒标签只是舍入显示。
- 当前保留 Natsume Iroha（《碧蓝档案》枣伊吕波）基线，不是《魔法纪录》环彩羽；国际服不等于英语配音。模型卡许可不授予所有录音/声音素材权利。
- Aris 私有历史推理峰值略高于 2048 MiB，不能宣称默认预算可完成。RMS、高频、有限值、无削波和加载成功不能证明哼唱保留、音质好、角色身份或训练充分。
- 原有沙哑、高音及分离丢失限制尚未宣称修复；模型替代与自训练还需权利、干净数据、设备和人工听辨。
- 当前分支未完成真实 QQ file/voice 送达与最终整链路验收；离线 serializer/ledger 不等于生产平台结果。

## 文档

- [对话式翻唱](<docs/CONVERSATIONAL_SINGING.md>)：结构化参数、选择、精确帧与投递。
- [配置契约](<docs/CONFIG_CONTRACT.md>)：默认值、许可、预算与留存。
- [部署验收](<docs/DEPLOYMENT.md>)：隔离验证门槛、迁移与回滚。
- [SDK 合规记录](<docs/SDK_COMPLIANCE.md>)：当前与历史证据范围。
- [变更记录](<CHANGELOG.md>) · [项目开发规则](<AGENTS.md>)。

## 开发与验证

先按[项目规则](<AGENTS.md>)和[部署验收](<docs/DEPLOYMENT.md>)测实际余量，再在已有限额的专用服务中逐目标文件验证，每文件新进程。TMPDIR/TEMP/TMP、basetemp、缓存与pycache必须显式位于经stat验证的磁盘数据目录，禁用pytest缓存或指定磁盘路径，不使用/tmp与/run/user存放测试文件。纯目标测试固定 512 MiB 硬限/384 MiB 软限、零 swap、有限时限，并须至少 1 GiB 祖先余量；禁止裸全量 pytest、并发重型测试、安装依赖或涨预算凑通过。

0.6.1本次磁盘隔离验证按成功日志去重：30个定向文件、1262项通过，无当前跳过；其中媒体阶段仅执行75个有界用例，17个其它参数变体未重新执行，**不宣称全量通过**。范围含曲名/首候选、900秒来源、90秒上下文、recipe/probe/selection绑定、历史身份、旧claim恢复、生命周期、只读Host AST及mock单次投递。成功unit最高实际charged peak为385.9MiB，OOM计数为0，预算未提高。首次断言/导入错误和180秒timeout日志均保留；媒体拆分独立窗口进程，reload-race修正mock签名并加有限等待后通过。已有缓存依赖只供隔离测试读取，没有安装依赖、audio-lab测试或真实模型推理；真实QQ及音频质量仍未验收。只按用户明确授权应用/重载，不发送QQ回归音频。

欢迎通过 [Issues](https://github.com/QR-0W/maibot_sing-main/issues)提交可复现问题；说明插件/Host/SDK 版本、公开错误码与验证层，不公开 token、cookie、数据库、用户音频或个人日志。

## 致谢与许可证

感谢上游 [maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main)、[ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)、[maibot-music](https://github.com/pan-ice/maibot-music)、[RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)、[UVR5](https://github.com/leebufan/Ultimate-Vocal-Remover)与 [Demucs](https://github.com/facebookresearch/demucs)。上游历史步骤不替代本 fork 的资源/许可契约。

README 信息层次参考 [Codewhale 中文 README](https://github.com/Hmbown/Codewhale/blob/main/README.zh-CN.md)，文本与架构说明针对本项目原创；没有虚构截图、徽章或音质结论。

[MIT](<LICENSE>)仅覆盖相应源码；模型、歌曲录音与声音素材的权利须单独确认。
