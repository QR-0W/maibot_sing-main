# 0.6.1 配置与请求契约

配置结构版本仍为0.6.0；本更新没有修改运行配置或新增迁移Hook。生产应用须获用户授权，真实媒体与QQ送达另行验收。

本契约对应 `feat/conversational-singing`，不是启用或生产 QQ 试发许可。配置结构以 [入口配置模型](<../plugin.py>) 为准，依赖与兼容范围以 [Manifest](<../_manifest.json>) 为准；[示例配置](<../config.example.toml>)仅供审阅，不能覆盖 Runner 管理的运行配置。遵循 [MaiBot Vibe Coding 指南](https://docs.mai-mai.org/plugin/vibe-coding)。

## 配置来源

- `PluginConfigBase + Field` 定义源码契约；Runner 生成、补齐当前安装实例的配置。插件不自行复制模板或新增配置迁移 Hook。
- 默认 `plugin.enabled=false`。当前兼容下界为 Host 1.3.0 / SDK 2.8.2；没有验证所有声明范围内的版本。
- 模型、独立推理环境、声音素材权利不随插件分发；不要修改 MaiBot 核心依赖或全局模型缓存。

## `[local]` 关键字段

| 字段 | 契约 |
|---|---|
| `backend` | 当前只支持 `local` 的持久受限渲染；不回退旧 sidecar。 |
| `output_dir` | 空值使用 `ctx.paths.data_dir/covers`；覆盖值须为绝对、非符号链接目录。账本首次绑定后不能靠热更搬迁库根。 |
| `model_path` / `index_path` | 管理员固定的 RVC 权重和索引绝对路径；用户不能指定任意模型或输入路径。 |
| `hubert_path` | 显式 HuBERT 路径，无开发机默认值。 |
| `demucs_repo_path` | 专用本地仓库，仅含受支持的 `htdemucs.yaml` 与签名 checkpoint；缺失即失败，不联网补权重。 |
| `rvc_script` / `rvc_upstream_path` | 固定、受审查 wrapper；upstream 必须是 wrapper 同目录的 `upstream`，与实际导入一致。 |
| `worker_python` | 包含已有推理依赖的隔离解释器；正常 venv 解释器符号链接可用。媒体运行时代码须兼容该解释器。 |
| `inference_lock` | 与本机其它重型实验共享的绝对锁路径；在媒体服务内部取得，不允许按作业换锁。 |
| `memory_max_mb` | 默认 **2048 MiB**，范围 512–6144；1 MiB=1,048,576 字节。限制整个独立媒体服务及所有子进程，不是共享 SDK Runner 的对象内存。 |
| `memory_headroom_mb` | 默认 512 MiB，范围 256–4096；每个有限共享祖先必须容纳完整媒体预算加此余量。 |
| `cpu_quota_percent` | 默认 150，范围 25–400；是 systemd CPU 配额，不代表线程数。 |
| `media_tasks_max` | 默认 64，范围 8–512，限制整个媒体服务的任务数。 |
| `max_queue` | 默认 2，范围 0–8；总未终结容量为 `max(1,max_queue+1)`，包括待选择、排队、运行及取消待对账。 |
| `max_duration_s` | 仅控制full完整来源，默认/上限300秒，最短30秒；excerpt完整来源统一30–900秒，所有片段仅分离唯一中心≤90秒原音频上下文。完整来源仍须成功EOF流式计帧，不能用90秒截断冒充完整源校验。 |
| `max_download_bytes` | 默认/上限 64 MiB；响应声明及流式累计字节都校验。 |
| `timeout_s` | 历史兼容字段，不控制新版阶段或整曲 RPC。 |
| `musicdl_python` / `local_source_allowlist` | 历史兼容字段，不提供普通聊天本地输入入口；在线来源由同一登录态按精确平台 ID 解析。 |

资源设置在生命周期首次 await 前验证并冻结，媒体单元通过环境获得同一快照，无缺字段时的旧 4 GiB 回退。`MemoryHigh` 自动取硬限的 75%，`MemorySwapMax=0`，`KillMode=control-group`，`OOMPolicy=kill`。Worker 验证实际 cgroup 的硬/软限、swap、CPU、任务数及 `memory.oom.group`。

启动前读取真实容器及目标用户服务 cgroup 的有限祖先容量/用量；在共享锁内、启动媒体子进程前再次检查。未知容量、不可读 cgroup 或余量不足明确拒绝，不悄悄缩小预算。用量包括被计费的文件缓存，不等于不可回收进程 RSS。其它服务仍可能增长，**本策略不保证整个容器永不 OOM**，也不会停止或修改它们。

## 两个入口，一份接纳实现

| 入口 | 参数来源 | 默认长度 | 默认伴奏 | 默认投递 |
|---|---|---|---|---|
| `/翻唱 歌名 - 艺人` | 校验后的 Host 原始直接文本 | full | 有 | file |
| 自然语言 → `cover_song` | LLM 填格式化 `title` / `artist` | 12–18 秒 excerpt | 无 | voice |

Command 仅匹配 `/翻唱`，不拦截自然句式。Tool 是正常 deferred 工具，必须由 MaiCore/LLM 发现并调用，不设置 `core_tool=True`，不借 Hook 建另一条入口。两者都调用 [JobService](<../services/job_service.py>) 的结构化接纳；只在目录搜索边界拼接 query，不再拆回 title/artist，也不禁止字段内部合法的 ` - `。

- Command 的 `--full/--excerpt`、`--with-instrumental/--without-instrumental`、`--file/--voice` 独立覆盖，每个维度只能出现一次；支持 `--album`、`--source-id`、`-v`，未知/重复/矛盾选项拒绝。
- Tool 的可选参数为 `render_mode`、`instrumental`、`delivery_mode`、`album`、`source_id`；歌名或艺人未知时应先问用户，不能编造。
- **明确请求翻唱即授权完成后向当前原会话最多尝试一次投递，不需要 `--auto-reply`。** 旧旗标只兼容；历史不带许可的作业不升级。
- Tool自动冻结准确过滤后的首个候选（包含Live）；首项超限/不可访问明确拒绝，不跳后项。Command多个候选仍由本人 `/翻唱选择 任务ID 序号` 选择。不会按时长推断studio或权利；默认候选期限600秒。
- `/翻唱状态`、`/翻唱取消` 保留；`cover_song_status(job_id)` 只读本会话当前用户的任务。
- Tool 返回 `success/job_id/state/code/content`，可附 `choices/delivery_state`。这是接纳或状态结果，不等待整曲，不把已接纳等同已渲染或已发送。

## 信任与重放

Command 校验当前 NapCat/Host 原始消息 ID、QQ 账号/目标/已注册路由及直接 text，拒绝引用、@、语音/ASR、转发、notify 和 WebUI 虚拟 QQ。原始消息 ID 参与持久去重，许可标记为 `napcat-direct-text-v2`。

Tool 校验 SDK/MaiCore 提供的一致 `stream_id/chat_id`、QQ 平台及用户，并拒绝虚拟会话。按用户接受的 Host/LLM 会话信任边界工作，标记为 `napcat-tool-session-v1`；**不是原始消息锚点或密码学签名**，本分支不修改核心，也不自行计算真实聊天流 ID。

Tool 的完整用户/会话/参数意图在 SQLite 事务中绑定随机任务 token：活动任务复用，终态更新时间后 60 秒内重放复用，此后允许新请求。未提交搜索预约也有限期；不是永久禁止用户再唱同一首歌。Command 保持原消息去重。旧 v1/缺标记的 pending 不自动投递，sent/unknown 不重发。

## 执行、持久成品与错误通知

- 每阶段一个有限用户服务；模型工作严格串行。只有证明旧 unit 及子进程已停止，才释放运行槽。
- decode/separate/convert/mix/encode/validate 的阶段时限分别为 120/600/180/60/90/60 秒，unit 另加 30 秒退出余量。RVC 单次输入至多 25 秒；没有整曲 900 秒 RPC。
- 永久 MP3 在全部字节、来源、配方和阶段收据验证后发布。成品及可浏览硬链接不自动删除；工作区、日志、未封存输出保留诊断。
- 音频 outbox：`pending → dispatching → sent/unknown`。只有 `sent=True` 加有效平台消息 ID 才确认送达；超时/异常/布尔回执为 unknown，永不自动重发。
- 失败/中断的公开文本通知使用独立 `(job_id,kind)` 账本，仅新接纳且明确许可的作业获得资格。claim 在 RPC 前持久化；重载的 dispatching 转 unknown，不重发、不影响媒体投递字段。消息只使用公开错误码/文案，不输出私有异常。
- SIGKILL 本身不等于 OOM；只有 systemd 明确 `oom-kill` 才报告 `unit_oom`。确认 OOM 后终止整个媒体服务、不发布/发送未完成音频、不自动重试，已验证的旧阶段产物保留。
- `/说` / `speak_voice` 的 TTS 与旧后处理尚未纳入当前受限链路，明确不支持；不合成、不读取参考音频，不回退未受限路径。音乐登录/二维码与固定音色列表保留。

音质、角色身份及训练素材权利是独立验收项。私有 Aris 测量峰值稍高于 2048 MiB，不据此声称它能在默认额度内完成。更多说明见[对话式翻唱](<CONVERSATIONAL_SINGING.md>)与[部署验收](<DEPLOYMENT.md>)。
