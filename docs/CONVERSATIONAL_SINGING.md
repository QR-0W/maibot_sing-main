# 对话式翻唱（0.6.1 有界上下文）

本说明描述源码接口；生产更新/重载须用户明确授权。自动化、真实Runner加载、实际媒体、平台送达与人工听辨是不同验收层，不以纯回归或加载成功证明音频质量或QQ送达。

## 1. 正常聊天走 Tool，斜杠命令走 Command

| 请求 | 入口 | 谁提供曲目字段 | 长度默认 | 伴奏默认 | 格式默认 |
|---|---|---|---|---|---|
| 「你能唱一下朴树的生如夏花吗？」 | LLM 发现并调用 `cover_song` | LLM 填 `title/artist` | 12–18 秒 excerpt | 干声 | voice |
| `/翻唱 生如夏花 - 朴树` | `@Command` | 原始直接文本 | full | 有伴奏 | file |

自然句式不再由 Command 正则抢先拦截；没有 Hook 或隐藏备用入口。Tool 使用 [MaiBot 官方指南](https://docs.mai-mai.org/plugin/vibe-coding)推荐的正常 deferred 发现，不设置 `core_tool=True`，也不把旧 `ActivationType.ALWAYS` 当作发现保证。

LLM 不知道歌名或艺人时应先问用户；想听原唱/播放歌曲时不能误用翻唱工具。`title/artist` 在业务层保持结构化，只在搜索时拼 query，不再从拼接字符串反拆，不禁字段内部合法的 ` - `。Command 语法在最后一个 ` - ` 分隔歌名/艺人；有歧义的名称宜由 Tool 的独立字段表达。

**既然用户请求翻唱，就授权完成后最多尝试一次送达，无需 `--auto-reply`。** 旧旗标只作命令兼容。三维独立覆写：

- Command：`--full/--excerpt`、`--with-instrumental/--without-instrumental`、`--file/--voice`。
- Tool：`render_mode='full'/'excerpt'`、`instrumental=true/false`、`delivery_mode='file'/'voice'`。
- 可附专辑与来源 ID；命令另有 `-v` 固定模型别名。选项重复、矛盾、未知或缺值均拒绝，不采用最后一个覆盖前面的行为。

两入口调用同一个 [JobService](<../services/job_service.py>) 搜索/接纳接口。Tool按用户批准的first-candidate-v1自动冻结准确过滤后的首个候选，包含Live；首项不可访问或超过请求模式源时长上限就拒绝，不跳到后项。Command多个候选保留本人 `/翻唱选择 任务ID 序号`。不以时长推断studio；按冻结平台与曲目ID取链，不偷换录音。合法内部斜杠及其两侧空白统一比对，不删除标题单词或版本标记。

## 2. Tool 返回与有限去重

`cover_song` 只进行有界目录搜索和持久接纳，不等待整曲。返回：

```json
{"success": true, "job_id": "<任务ID>", "state": "queued", "code": "queued", "content": "<可读状态>"}
```

待选择时附 `choices`；`delivery_state` 与渲染状态分开。`success=true` 的接纳不证明已经生成或发送。`cover_song_status(job_id)` 限本会话当前用户读取；命令仍有 `/翻唱状态`、`/翻唱取消`。

Command 用原消息 ID 去重。Tool 没有不可覆盖原消息锚点，使用会话用户与完整意图的原子绑定：活动任务复用，终态更新时间后 60 秒内重放复用，窗口之后允许新请求；未提交搜索预约也有有限期限。相同服务内并发/取消重放共享搜索任务，跨进程依赖既有独占 scheduler ownership，最终任务接纳有 SQLite 唯一约束。不是永久禁止同一首歌，也不是严格按原消息 exactly-once。

## 3. 许可边界

- Command：当前 NapCat/Host 直接 text、原消息 ID、账号/路由/目标一致性检查；拒绝引用、@、语音/ASR、转发、card、notify 和虚拟 QQ。无可信来源时不搜索、入队或向该 stream 发拒绝文本。
- Tool：按用户接受的 SDK/MaiCore/LLM 会话上下文信任，一致 `stream_id/chat_id`、QQ 用户与非虚拟会话检查；不修改主程序，不自行计算聊天流 ID。
- 分别记录 `napcat-direct-text-v2` 与 `napcat-tool-session-v1`。它们是信任边界标签，不是密码学签名；Tool token 不是原始用户消息 ID。Host 的模型优先参数合并限制仍存在，不声称已修复核心。
- 旧 `auto_reply=false`、旧 v1 或缺许可 pending 不追认升级；已 sent/unknown 不重发。取消不能撤回已被平台接受的发送。

实现见[入口](<../plugin.py>)、[解析器](<../services/request_options.py>)及[账本](<../services/job_store.py>)。

## 4. 片段链路与音频身份

excerpt 当前为：

```text
source_probe（完整EOF流式计帧）→ decode（唯一中心原音频上下文）→ separate（仅此窗口）→ excerpt → convert_000（一次RVC）→ mix → encode → validate
```

excerpt完整来源为30–900秒，full仍为30–配置上限且最多300秒；64MiB/100秒下载和120秒完整解码超时不变。probe-v2以256KiB块流式数到成功EOF，不缓存整曲PCM。冻结44.1k整数帧：N=min(source_frames,90×44100)，start=(source_frames−N)//2，end=start+N，使用半开[start,end)。decode先重采样再按此帧窗输出original.wav，只分离此窗口。所有excerpt都走此策略；不扫描多窗、不重试、不回退整曲，也不声称中心窗包含副歌。

[选择器](<../runtime/excerpt_selection.py>)在同时间轴的 44.1 kHz stems 上每 50 ms（2205 帧）用 RMS/flatness 启发式评分，保留不超过 0.6 秒换气，丢弃不足 1.2 秒碎片。优先一段，不足则至多两段按原时间顺序拼接，不循环凑时长。目标约 15 秒，输出 12–18 秒；低能量端点、人声有界归一、50 ms 淡入/120 ms 淡出；伴奏使用完全相同的源帧范围同步处理。无足够区间就失败，不回退整曲。

保留原音高，不自动变调或添加混响。**这不是副歌识别、语义乐句边界检测或音质修复。** 数值、时长及编码成功不能证明歌声好听或像角色。

[配方](<../runtime/recipe_identity.py>) `sing-render-v4`绑定真实source/权重/引擎资产、依赖版本、context helper与selector源码hash、render_mode、source_frames/context_window及严格冻结的全部argv/预算/输入输出。instrumental 通过 mix argv 影响音频身份；delivery_mode 只改变出站格式，不改变音频缓存。依赖版本不等于全 OS/二进制指纹。

永久 metadata 保存 selection 与 excerpt receipt，包括精确整数 44.1 kHz 源/输出帧、半开范围 `[start,end)`、淡入淡出/归一参数。目录毫秒标签只是舍入显示，恢复以 metadata 和字节收据为准。selection-v2记完整源帧数、冻结窗口与原来源绝对范围，analysis仅对应窗口；probe/recipe/selection必须逐字段绑定。旧v2/v3完成成品、v1 probe/selection保持原意与指纹只读兼容；旧已claim单元先核对所有权，但旧未完成计划禁止新代码续跑，不删收据或改schema绕过。

## 5. 内存、故障与永久成品

默认媒体硬限 **2048 MiB**、软限 **1536 MiB**、swap=0、CPU=150%、Tasks=64；所有媒体阶段与子进程都在有限 systemd 用户服务内。预算在启动时冻结，Worker 检查实际 cgroup，无旧 4 GiB 缺省回退。详见[配置契约](<CONFIG_CONTRACT.md>)。

启动前检查真实容器及目标服务的有限共享祖先，锁内、模型加载前再查。余量须大于等于完整预算加默认 512 MiB reserve；不足或未知则零媒体启动，给出 `resource_budget_exhausted/cgroup_unavailable`。不改变无关服务、全局缓存或容器限额，不保证其它进程不会耗尽全容器。

`OOMPolicy=kill` 与 `KillMode=control-group` 终止整个自有服务，外部 coordinator 保持独立。只有明确 systemd `oom-kill` 才报 `unit_oom`，SIGKILL 不冒充 OOM。旧 worker/children 未证明停止前保留槽；不发布未完成音频，不自动重试，保留已验证分块。

失败/中断通知用独立 `(job_id,kind)` 文本账本，只为新明确许可作业建资格，先 claim 再 RPC；仅正向详细 ack 和平台消息 ID 才记 sent。重启的 dispatching 转 unknown，不重发，不使用媒体 delivery token，不输出私有异常文案。

成品只在来源/配方/全部计划收据校验后发布到永久库。MP3、可浏览硬链接、metadata 与索引不自动删除；磁盘留存由管理员审查。TTS、旧 sidecar/后处理未纳入当前受限链路时明确不支持；`/说` / `speak_voice` 不生成或发送语音，不作未受限 fallback。

## 6. 一次 file/voice 投递

[媒体 outbox](<../services/delivery_outbox.py>)消费持久许可：`pending → dispatching → sent/unknown`。voice 用 `voiceurl`，file 用 `{url,name}`，均只一次 `send.custom(...,return_details=True)`。只有 `sent is True` 与非空平台 ID 确认 sent；仅上传、布尔成功、超时或异常都是 unknown，不换格式、不重发。claim 后崩溃可能零次送达，最多尝试一次不是保证恰好送达一次。

**当前分支尚未做真实 QQ/NapCat 端到端送达。** serializer/假体测试不能替代平台验收；启用前还要确认实际发送进程能读永久产物路径。

## 7. 证据与验证方式

当前资源模块的定向离线运行：80 项通过，独立服务请求 512 MiB 硬限、384 MiB 软限、零 swap、100% CPU、Tasks=64、180 秒时限；该次 systemd 报告 peak 34.7M。随后协调器测试组因实际余量不足 1 GiB 被启动门槛拒绝，**没有运行**。此前任何全量数字均属旧源码，不作为当前实现通过证明。

验证须先测真实祖先余量；只用已有解释器/依赖，在专用 bounded service 中逐文件或有界组运行，每组新进程。先在单元内验证应用限额，再测试并记录 cgroup peak/时间。禁止裸 shell 全量 pytest、并发重型测试或为通过提高预算；安装/模型还必须在服务内取得共享推理锁。不修改 MaiBot 环境来凑依赖。

音频私有比较与哼唱诊断单独留存。Aris 两次历史推理实际峰值略高于 2048 MiB，不能声明默认限额可运行；不同模型、分离器及录音可造成限制，人工听辨与训练素材权利未验收。报告/音频/机器路径/模型 hash 日志/数据库/凭证不提交公开仓库。
