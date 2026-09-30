# 对话式翻唱（未发布功能分支）

`feat/conversational-singing` **尚未合并、尚未部署**；插件与配置版本保持 `0.5.0`。本文描述已落地接口，不是部署许可，也不表示生产 QQ 或音质验收通过。本次不部署运行 profile、不做真实 QQ 试发。

遵循 [MaiBot 官方 Vibe Coding 指南](https://docs.mai-mai.org/plugin/vibe-coding)、[生命周期](https://docs.mai-mai.org/plugin/lifecycle)和[配置管理](https://docs.mai-mai.org/plugin/config)。配置由 Runner 管理，详情见[配置契约](<CONFIG_CONTRACT.md>)；历史与当前证据分列于 [SDK 合规审计](<SDK_COMPLIANCE.md>)。

## 1. 可用入口与默认值

[plugin.py](<../plugin.py>) 的同一个 `@Command("翻唱")` 使用[严格解析器](<../services/request_options.py>)同时匹配斜杠语法和有限自然语法，经过 Host Command 的禁用/权限检查。没有 Hook 入队副作用，没有借用 Tool 身份，也不是“LLM 理解任意聊天后自动唱歌”。

| 入口 | `entry_kind` | `render_mode` | `instrumental` | `delivery_mode` |
|---|---|---|---|---|
| `/翻唱 歌名 - 艺人` | `command` | `full` 整曲 | `true` 有伴奏 | `file` 文件 |
| `唱一下《歌名 - 艺人》` | `natural` | `excerpt` 12–18 秒 | `false` 干声、无伴奏 | `voice` 语音 |

自然语法也接受 `唱一段《歌名 - 艺人》` 和 `唱《歌名 - 艺人》`。必须有书名号和明确的 `歌名 - 艺人`；后面只能有受支持的选项，不接受额外闲聊、换行或从引用/语音识别出来的文字。

**两入口无原文末尾唯一字面量 `--auto-reply` 都只生成保存，不自动发送音频。** `voice` / `file` 是将来获准投递的格式，不是发送许可；合法请求仍会有任务、选曲、状态等 Command 文本响应。

### 选项

| 维度 | 覆盖选项 | 规则 |
|---|---|---|
| 渲染长度 | `--full` / `--excerpt` | 一维最多一个，含同值重复也拒绝 |
| 伴奏 | `--with-instrumental` / `--without-instrumental` | 与入口默认值独立覆盖；冲突、重复拒绝 |
| 投递格式 | `--file` / `--voice` | 不改变渲染长度、伴奏或许可；冲突、重复拒绝 |
| 专辑 | `--album 专辑名` | 值可含空格，只能指定一次 |
| 来源曲目 | `--source-id 平台曲目ID` | 只接受明确 ID，只能指定一次 |
| 音色 | `-v 管理员固定音色` | 单个已配置别名，只能指定一次，不接受任意模型路径 |
| 自动投递许可 | `--auto-reply` | 无值，原消息最后且仅一次 |

未知选项、缺值、无值 flag 后带额外文字、任何维度重复均拒绝，不采用“最后一个生效”。许可和所有请求选择器只从验证后的 Host 原文解析，不从 Tool 参数或 `matched_groups` 补造。

### 语法示例（仅展示，不实际执行/发送）

```text
/翻唱 Creep - Radiohead
唱一下《Creep - Radiohead》
唱《Creep - Radiohead》 --with-instrumental --file
唱一段《Creep - Radiohead》 --full --with-instrumental --file
/翻唱 Creep - Radiohead --excerpt --without-instrumental --voice --auto-reply
/翻唱 Creep - Radiohead --album 专辑名 --source-id 平台曲目ID -v 固定音色 --auto-reply
```

前四条没有自动发送许可。后两条仅演示授权语法，**不要将这些示例当作真实 QQ 测试指令**。多个候选时使用 `/翻唱选择 任务ID 序号` 明确选择；两入口都不会擅自选第一首。可用 `/翻唱状态 任务ID` 和 `/翻唱取消 任务ID`；取消不能撤回已被平台接受的发送。

## 2. 直接文本与 QQ 身份边界

- Host `raw_message` 必须为 1–32 个直接 `{type: "text", data: "…"}` 组件，不能多出其他键。Host 用单个空格拼接后总长 ≤2048 字符，且必须同时与 handler `text` 和 `processed_plain_text` 完全相等；不展开嵌套数据、不规范化原文。
- 拒绝 @/at、引用、语音、转发、card 和 notify。`is_notify` 必须为 `False`；`reply_to` 可省略或为 `None`，其他任何非 `None` 值（包括空字典、空字符串、`False`）均拒绝。mention 标志不能表示被提及。
- 原始消息 ID、用户、QQ 平台、会话、当前 NapCat codec 的账号/消息类型/群或私聊目标，必须与 Host 已注册网关路由元数据一致。普通 WebUI、WebUI 虚拟 QQ 和缺字段的旧适配器不能提供此许可；失败不向该 stream 发送拒绝文本或其他媒体，也不搜索/入队。
- 持久来源证明为 `napcat-direct-text-v2`。旧 `napcat-host-route-v1`（v1）或缺证明的 pending 在扫描和事务 claim 两处被拦截，不追认、不自动投递；已 sent/unknown 的旧记录也不重发。需要新请求时必须由真实用户自行从可信原会话发起，不能改账本补许可。
- 证明是当前 Host/NapCat 信任边界标记，不是密码学签名，不能防御拥有网关 RPC 权限的恶意插件。Tool 只返回 Command 指引，不凭自由会话 ID、模型声称的同意或自然语言意图入队/发送。

实现见[请求校验](<../services/request_options.py>)、[入口](<../plugin.py>)、[账本](<../services/job_store.py>)和[任务服务](<../services/job_service.py>)。

## 3. 速度 B：整曲分离，短段只做一次 RVC

[渲染计划](<../runtime/render_plan.py>)的 excerpt 路径是：

```text
decode → separate（整曲）→ excerpt → convert_000（一次 RVC）→ mix → encode → validate
```

源音频仍须满足 30–300 秒的完整输入限制。不是先截原曲再分离，节省的是后续 RVC 的处理量，不能保证固定耗时或指定倍数加速。

[选择器](<../runtime/excerpt_selection.py>)在已经分离、时间轴相同的 44.1 kHz stems 上工作：

1. 每 50 ms（2205 帧）计算 RMS 与 spectral flatness，以声学活跃/音调性启发式打分；优先选一段可用区间。
2. 保留 ≤0.6 秒的换气间隔，丢弃 <1.2 秒碎片。一段不足以达到最短输出时，最多选择两段，**按原曲时间顺序**拼接；不重排、不循环凑时长。
3. 目标约 15 秒，实际 PCM 输出严格为 12–18 秒；选择低能量端点，每段淡入 50 ms / 淡出 120 ms，对人声做有界 RMS 归一。伴奏沿同一组源帧范围同步截取、淡入淡出、拼接，避免混入未选区间。
4. 拼好的单个人声片段仅做一次 RVC，保留原音高、不自动变调、不加混响；按选择证据对齐后输出干声，或在显式选择伴奏时混入同步片段。缺乏足够可用区间时失败，不回退整曲。

**这不是副歌识别，也不是语义乐句边界检测。** 低能量端点不保证完整歌词、自然乐句或歌声质量；RMS/flatness、有限数值及编码成功都不能证明角色音色或好听。

## 4. metadata、音频缓存与恢复

- 新渲染配方为 `sing-render-v3`，见[配方身份](<../runtime/recipe_identity.py>)。绑定真实 source/模型/index/引擎资产、运行依赖版本、selector 源码 hash（`hashes.excerpt_selection`）、`render_mode` 和实际执行 argv；私有路径在配方中替换为占位符。`instrumental` 通过实际 mix argv 进入音频身份。
- `delivery_mode` 只决定 file/voice 出站，不进入音频配方，因此改投递格式不改变 audio cache；同一个原始消息也不能改内容重放来另取许可。渲染模式或伴奏变化则不是同一音频配方。
- excerpt 的永久 metadata 保存 `selection`（`sing-excerpt-selection-v1`）和 `excerpt_receipt`，包括 sample rate、source/output frames、精确 `source_ranges` 与对应输出帧、分析阈值、fade 和 normalization。范围为整数 44.1 kHz 帧的半开 `[start_frame, end_frame)`；原时间顺序和同步关系不能从文件名猜测。
- [成品提交](<../services/artifact_store.py>)校验 selection 与阶段 receipt 的字节/hash 绑定后才发布；[目录标签](<../services/library_catalog.py>)将源范围舍入到毫秒显示，一段标片段，两段标拼接。**目录毫秒标签不是精确帧身份**，恢复以 metadata/receipts 为准。
- 已完成 `sing-render-v2` 成品可只读校验/访问，不重新发布或覆盖；旧 v2 运行中 plan 不能直接升级为 v3 续跑，也不能通过改 schema、删收据或换 unit 绕过。旧成品及旧证据保留，恢复差异需要维护者单独处理。

## 5. file/voice 的一次投递契约

[Outbox](<../services/delivery_outbox.py>)只消费已持久化、仍有效的许可：`pending → dispatching → sent/unknown`。`voice` 构造 `voiceurl` 和本地 file URI；`file` 构造 `file` 与 `{url, name}`，均只发起一次 `ctx.send.custom(..., return_details=True)`。历史“翻唱唯一使用 voiceurl”的描述不适用于本分支；独立 `/说` 仍是单次 voiceurl。

只有详细 ack 同时满足 `sent is True` 和有效非空字符串 `message_id` 才记为 sent。仅上传成功、布尔成功、空 ID、超时、异常或 `False` 都不足以确认平台送达，记 unknown。**真实 QQ file 尚未测试**；Host serializer/ledger 的自动化证据不等于 NapCat/QQ 真实文件送达。

不切换 file/voice fallback，不自动重试；重启也不把 unknown/sent 退回 pending。同一次 RPC 的迟到有效 ack 可以结算原尝试，不会触发第二次发送。这是最多尝试一次，不是保证恰好送达一次；claim 后崩溃可能零次送达。媒体路径必须对实际平台进程可读，未确认前不启用自动投递。

## 6. 模型结论与验证范围

暂留现有 Natsume Iroha baseline：目前没有已证同身份且更好的替代。`sid=0` 可以加载、输出 finite 只证明相应技术检查，不证明角色身份、训练素材权利或歌声品质；人工听辨待做。高音破音、沙哑及开头表现不佳等历史限制未宣称修复，也不将“国际服”当作英语配音证据。

已报告的目标测试范围：renderer **191 passed**；入口 + Host real serializer / ledger / outbox **340 passed**。这些是分组证据，不相加、不宣称当前全量通过；完整分支结果另行记录。历史 0.5.0 的 359 项记录不替代本功能验收。

在已准备好的隔离测试环境运行，命令只作复现指引：

```bash
MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot python -m pytest tests -q
```

`MAIBOT_TEST_HOST_ROOT` 指向只读 Host checkout；测试解释器应已有所需 SDK/测试依赖。媒体测试用已有 `soundfile`、NumPy 等依赖的隔离 media 环境；缺依赖时选择正确的既有环境或说明未执行，**不改 MaiBot 环境、不安装依赖来凑通过**。这不是部署或真实 QQ 端到端测试命令。本次文档任务不运行推理、登录或消息发送。

研究报告、真实机器路径、私有模型 hash 日志、音频、数据库、运行配置和 credentials 均私有保存，不复制进 Git；面向用户的研究报告另存、不提交。仓库只记录泛化接口、限制与证据类别。
