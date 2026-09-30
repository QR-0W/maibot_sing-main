# MaiBot Plugin SDK 合规审计

当前文档核对：`feat/conversational-singing` 功能分支，尚未合并、尚未部署；manifest/config 仍为 `0.5.0`。初审及程序修订 `81203d7` 的结果保留为下方历史基线，不能套用于当前功能分支。

## 未发布功能分支：接口与证据范围

以 [MaiBot 官方 Vibe Coding 指南](https://docs.mai-mai.org/plugin/vibe-coding)、[Manifest](https://docs.mai-mai.org/plugin/manifest)、[生命周期](https://docs.mai-mai.org/plugin/lifecycle)和[配置管理](https://docs.mai-mai.org/plugin/config)为基线。源码接口已落地，操作见[对话式翻唱](<CONVERSATIONAL_SINGING.md>)；本次不部署运行 profile、不测试 QQ，不修改 Host 环境或安装依赖。

| 项目 | 当前实现与限制 |
|---|---|
| 组件与身份 | [入口](<../plugin.py>)的同一个 `@Command("翻唱")` 用[严格解析器](<../services/request_options.py>)匹配 `/翻唱 歌名 - 艺人` 与 `唱一下《歌名 - 艺人》` / `唱一段《…》` / `唱《…》`。沿用 Host Command 检查，不使用 Hook 副作用或 Tool 身份，不表示任意 LLM 聊天可触发。Tool 仍只提供 Command 指引。 |
| 独立默认维度 | command = full/伴奏/file；natural = 12–18 秒 excerpt/干声/voice。`--full/--excerpt`、`--with-instrumental/--without-instrumental`、`--file/--voice` 独立覆盖，各维度冲突和重复均拒绝。保留 `--album`、`--source-id`、`-v`，选择器也不能重复。 |
| 原文许可 | **两入口都只有原文末尾唯一 `--auto-reply` 才授权自动投递；没有它都只生成保存，不自动发送音频。** file/voice 不是许可。许可不从 Tool、匹配组或模型转述构造；合法任务文本响应与音频投递分开。 |
| Host 直接文本 | raw 仅 1–32 个直接 text 组件，空格拼接后 ≤2048 字符，与 Host/handler 文本完全一致。拒绝 @、引用、语音、转发、card、notify；`reply_to` 可省略/`None`，其他非 `None` 值一律拒绝；保留 QQ 账号/目标/Host route 检查。不可信来源不发送拒绝消息。 |
| 持久来源证明 | [账本](<../services/job_store.py>)使用 `napcat-direct-text-v2`；旧 v1（`napcat-host-route-v1`）及无证明 pending 在扫描和 claim 两处拒绝，不自动投递；sent/unknown 不重发。不是密码学签名，不能防御有网关权限的恶意插件。 |
| 渲染与收据 | [计划](<../runtime/render_plan.py>)先整曲分离，再以[选择器](<../runtime/excerpt_selection.py>)的 50 ms RMS/flatness 选一段，不足则两段按原时间顺序拼接，保留 ≤0.6 秒换气、丢弃 <1.2 秒碎片，低能量端点 + 50/120 ms fade + RMS 归一，片段仅一次 RVC。不等于副歌识别、语义乐句边界、音质改善或固定耗时。 |
| 音频身份 | [配方](<../runtime/recipe_identity.py>)为 `sing-render-v3`，含 selector 源码 hash、`render_mode` 与实际 argv；`delivery_mode` 不改变 audio cache。metadata 保存精确源帧、selection 与 excerpt receipt，[目录](<../services/library_catalog.py>)毫秒标签仅舍入显示。已完成 v2 成品只读兼容，旧 v2 运行中 plan 不能直接升级续跑。 |
| 单次 file/voice | [Outbox](<../services/delivery_outbox.py>)调用 `ctx.send.custom(..., return_details=True)`；voice 用 voiceurl，file 用 file 的 `{url, name}`。只有 `sent is True` 且有效非空字符串 `message_id` 才记 sent，其余 unknown。仅上传成功不足以确认送达，不切换格式 fallback、不自动重试。独立 `/说` 仍为一次 voiceurl。 |

### 已报告目标测试（不是全量验收）

- renderer：**191 passed**，覆盖选择、计划、精确帧/收据与成品/缓存兼容相关目标测试。
- 入口 + Host real serializer / ledger / outbox：**340 passed**，覆盖严格语法与 raw Host 直接文本、来源拒绝、许可冻结、两种投递格式及 detailed ack/不重复投递边界。
- 上述为目标集合证据，不能相加或称当前全量通过；全量结果由后续独立记录给出。本次文档核对不重新运行这些测试，不将历史 359 项结果当作功能分支验证。
- **真实 QQ file 未测试**。real serializer 是 Host 序列化契约证据，不是生产 QQ/NapCat 端到端送达；不在现有 profile 部署或做 QQ 试发。仍需单独授权的端到端验证、平台路径可读性确认及人工听辨。
- 模型暂留现 Iroha baseline，没有已证同身份更佳替代；sid0 可加载且输出 finite 不证明角色、权利或品质，高音/沙哑问题未宣称修复。

使用已有依赖的隔离测试解释器及只读 Host checkout，泛化复现命令：

```bash
MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot python -m pytest tests -q
```

媒体测试需要隔离 media 环境中已有的 `soundfile`、NumPy 等依赖；缺依赖时选择正确既有环境或记录未执行，不修改 MaiBot 环境、不安装依赖补测试。版本/依赖权威来源仍为 [_manifest.json](<../_manifest.json>)，不改 manifest/config 版本。用户研究报告另存不提交；本机报告、真实路径、私有模型 hash 日志、音频、credentials 与运行数据库不复制到 Git。

---

## 历史 0.5.0 验收摘要（仅程序修订 81203d7）

以下记录核对于 2026-09-30，初审日期 2026-09-29；其中“当前”“完整”仅指该历史修订，不包括上方未发布 feature。

- 最终完整测试：指定当前 Host/已安装 SDK 后 **359 passed**；独立模式未提供 Host 时，两项 Host 专属测试按约定跳过。
- 实际 MaiBot 1.3.0 Runner/PluginLoader、SDK 2.8.2 在私有目录完成配置生成/补齐、加载、self-config 热更、代码重载、部分初始化取消、单项 close 抛错后的其余资源清理、卸载和关停。IPC、Host 能力响应与音乐候选为假体，不连接生产聊天。
- 当前 Host 的 Command builder/serializer、NapCat codec 和 Host route-attach 原样函数经 AST 提取执行，避免导入业务模块。独立安全复核接受 2 条合法群/私聊来源，将普通 WebUI 与虚拟 QQ 的 22 条 Command 全部拦截；零新增发送调用、搜索及任务。独立目标测试 **91 passed / 1 deselected**。
- 原始文本明确带 `--auto-reply` 才能生成持久许可；同时要求当前受信任 NapCat/Host 元数据。全部 11 个 Command 在副作用前检查，失败不向该 stream 回复错误。旧缺来源标记的 pending 在扫描和原子 claim 处都被拒绝；标记不是密码学签名，不能防御有消息网关权限的恶意插件。当前普通 WebUI、虚拟 QQ 和缺元数据的旧适配器均拒绝，不宜用生产 WebUI 自动测试。
- `/163cookie` 不再解析、应用、存储或回显聊天输入凭据；管理员应使用受保护配置/扫码流程。不能据此保证已经发到聊天历史的秘密被删除。
- 真实媒体与 Runner 是分层验证：受限整曲冷运行、同 job/run-token/unit 的宿主中断恢复、不可变成品与目录重放已有独立证据；精确解码帧策略下，该整曲配方完全相同，17 个 stage 的已校验收据可复用。合成 44.99/300 秒音频确认规划与实际 PCM 帧数一致。正常媒体试验不替代输出超限/取消测试，后者单独验证了子进程与 reader 清理。
- 一次早期 Runner 试验曾误导入 Host 业务模块并触发生产 SQLite 初始化检查，缺少事前快照，**不能证明没有写入**。后续在所有 Host import 前使用 SQLite/Python 写入审计，Host Command 改用 AST；最终 Runner 记录 46 次私有 SQLite 连接、0 次生产连接。Host 文件日志用无文件替身；父进程 Python 审计不覆盖子进程原生写入。此事故与模拟边界未被隐瞒或解释成始终只读。
- 以上关闭的是已确认代码缺陷及相应隔离验收项，不等于完整 Host/QQ 网络端到端、真实音色身份、模型素材权利或用户所述沙哑已解决。部署仍需私有配置迁移、旧文件基线、单独产物根、平台可读性及健康核验；程序和依赖未变的文档提交可视为同一受测代码。

原始测量和隔离脚本由维护者私有保存，不在公开插件仓库内分发真实配置、账号、数据库、音频或模型。以下保留初审快照以解释修复来源，不能把其中“未执行 Runner”或“仍接受 Cookie”等历史观察误当为当前状态。

## 历史初审快照

本清单以 MaiBot 官方 [Vibe Coding 插件开发指南](https://docs.mai-mai.org/plugin/vibe-coding)、[Manifest](https://docs.mai-mai.org/plugin/manifest)、[生命周期](https://docs.mai-mai.org/plugin/lifecycle) 和 [配置管理](https://docs.mai-mai.org/plugin/config) 为基线。它记录可复现证据和剩余风险，不是发布声明，也不把历史音频实验等同于当前架构验收。

状态含义：

- **通过**：已有源码或自动化证据支持该项。
- **部分通过**：静态结构存在，但版本下界、Host 生命周期或端到端行为尚未完整证明。
- **未完成**：当前仍有明确缺口；启用或发布前必须处理。

## 审计边界

- 本次只审阅插件独立仓库、当前只读 MaiBot Host checkout 和其已安装 SDK。
- 没有修改 MaiBot 主程序、真实 `config.toml`、模型、QQ/网易云登录态或线上插件。
- Host 专属测试不调用 `create_plugin()`、`on_load()`、模型推理或发送接口。
- 当前入口和持久化服务仍在集成；下列 Host/SDK 结果是一次静态导入与 Schema 构建证据，不能替代 Runner 实际加载、热重载、卸载和故障恢复测试。

## 逐项清单

| 项目 | 状态 | 证据 | 未完成/后续动作 |
|---|---|---|---|
| 独立插件目录 | 通过 | 仓库根目录含 `_manifest.json`、`plugin.py`、`README.md`、`.gitignore`、`tests/`、`docs/`。安装说明要求放入 `plugins/maibot-sing/`，不修改 Host `src/`、`dashboard/` 或全局配置。 | 尚未在干净 Host 的实际 `plugins/` 安装目录完成加载验收。 |
| Manifest v2 严格结构 | 通过 | `_manifest.json` 使用 `manifest_version: 2`、严格三段版本、HTTP(S) URL、Host/SDK 闭区间、`zh-CN` 默认语言。当前 Host `ManifestValidator` 已真实解析通过。 | 每次 manifest 或 Host 校验器变化后重跑 `tests/test_manifest.py`。 |
| Host 兼容区间 | **部分通过** | Manifest 下界已从未证明的 `1.0.0` 收紧到实测 Host `1.3.0`，上界为 `1.99.99`。当前入口的自动投递授权依赖该 Host 的 Command bridge 把完整原始消息字典传入 handler（含 `message_id`、`platform`、`session_id`、`message_info.user_info`、`processed_plain_text`、`is_command`），并逐项校验后才接受 consent。 | 当前 Host 源码与严格 validator 已核实，但还没有执行正式 Runner 加载/命令调用；不得再宣称兼容 Host 1.0.0，也不得把静态检查描述成端到端验收。 |
| SDK 最低版本 | **部分通过** | Manifest 下界已从未证明的 `2.5.1` 收紧到当前实际安装 SDK `2.8.2`，上界为 `2.99.99`。2.8.2 能导入插件定义并由 `SingPlugin.build_config_schema()` 生成 Schema；授权投递依赖的 `ctx.send.custom(..., return_details=True)` 也在该版本中保留 `sent/message_id` 详细回执。 | 当前只核实 2.8.2；不得再声称 2.5.1 可用。正式 Runner capability bootstrap 与平台回执仍需实际加载验证。 |
| 能力最小化 | 通过（当前 HostValidator） | Manifest 仅声明 live capability ID：`send.text`、`send.custom`、`send.image`；入口源码分别使用文本、定制语音和二维码图片发送，且当前 Host `ManifestValidator` 接受这些值。 | 官方示例中的概念性 `send_message` 字符串不替换当前 Host 的真实能力 ID。Runner capability bootstrap 仍需实际加载验证。 |
| Python 依赖权威来源 | 通过（文档契约） | README 已明确 `_manifest.json.dependencies` 是唯一插件依赖来源；当前 Host 校验器也检查声明与运行环境/Host 约束。 | 仓库仍存在历史 `requirements.txt` 副本。它不是加载契约，未来应删除或由 manifest 自动生成，避免漂移；不要手工安装它来修改 Host 核心环境。 |
| Runner 管理配置 | 通过（静态） | `SingPlugin.config_model = SingPluginConfig`；配置模型继承 `PluginConfigBase`，字段使用 `Field`，包含 `[plugin] enabled/config_version`。README 明确 Runner 生成/维护运行时 `config.toml`。 | 需要在安装目录验证首次生成、字段补齐与 WebUI 保存。 |
| 配置 Schema | 通过（当前 SDK） | 显式设置 `MAIBOT_TEST_HOST_ROOT` 后，测试使用当前解释器实际安装的 SDK 调用 `build_config_schema()`；核对插件 ID、版本以及 `plugin.enabled=false`、`plugin.config_version=0.5.0`。 | Schema 可生成不代表所有字段的 UI/秘密输入体验合格；登录凭证字段仍需单独安全审阅。 |
| 忽略本地配置/数据库/媒体 | 通过 | 插件目录 `.gitignore` 含精确 `/config.toml`，并忽略 SQLite 主文件及 `-wal`/`-shm`、日志、音频、权重和缓存；审计时未发现这些运行产物被 Git 跟踪。 | 发布前再次运行 tracked-file 检查，禁止提交私有日志、配置、数据库、音频、cookie 或 token。 |
| 完整生命周期与工厂 | **部分通过** | 入口定义 `on_load()`、`on_unload()`、`on_config_update()`、`create_plugin()`；卸载源码包含后台任务、客户端和 durable services 清理。 | 当前只做静态导入，**未**让 Runner 注入真实 `PluginContext` 或调用生命周期。入口集成完成后必须验证 load → self-config reload → unload，并检查所有任务/连接/文件句柄关闭。 |
| 组件选择 | 通过（静态） | 新入口使用 `@Command` 和 `@Tool`，未发现 `@Action`。Tool 没有被文档建议为 `core_tool`。 | 需要在实际 Host 验证组件同步、命令正则、权限和 Tool 发现。 |
| 简体中文用户文本 | **部分通过** | 命令说明、主要成功/失败提示和持久阶段公开错误以简体中文书写。 | 多处提示把底层 `exc` 直接拼入用户消息，仍可能暴露英文或私有诊断；应建立稳定中文公开错误映射后再认定完全通过。 |
| 网络超时与异常处理 | **部分通过** | 命令和部分客户端路径声明了超时并捕获异常。 | 本次未逐个复核所有 QQ/网易云/MiMo/RVC 网络调用；需专项检查每个请求都有有限超时、响应大小边界和取消清理。 |
| 登录凭证处理 | **未完成** | `.gitignore` 排除真实配置/环境文件，运行时配置由 Runner 管理。 | `/163cookie` 接受聊天命令中的登录凭证，存在会话记录/日志暴露风险。生产环境不应使用；应在后续授权任务中改为受保护的配置或专用秘密输入，再删除聊天携密路径。 |
| README 安装/配置/命令/排障 | 通过 | README 已补充官方规范链接、独立目录安装、Runner 配置职责、命令/Tool、依赖权威来源、验证命令和常见问题。 | 实际发布时需根据最终入口与 manifest 再核对命令列表和能力。 |
| 历史 v0.4.0 音频报告 | 不作为当前通过项 | `docs/VALIDATION.md` 记录旧实现的片段、整曲和资源测量；README 已明确其仅为历史实验。 | 当前 durable lifecycle、授权投递、整曲产物、Host load/unload 必须各自重新验收，不能引用旧音质/整曲报告代替。 |

## 自动化验证

### 独立仓库模式

```bash
python -m pytest -q tests/test_manifest.py
```

预期：本地结构测试通过，两个 Host 专属测试在未设置环境变量时明确跳过。审计运行结果：`2 passed, 2 skipped`。

### 指定只读 Host 与当前 installed SDK

```bash
MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot \
  /path/to/MaiBot/.venv/bin/python -m pytest -q tests/test_manifest.py
```

审计运行结果：`4 passed`。该命令真实调用指定 Host 的 `src.plugin_runtime.runner.manifest_validator.ManifestValidator`，并用执行测试的 Python 环境中实际导入的 `maibot_sdk` 调用配置 Schema 生成。环境变量未设置时不会猜测宿主目录；变量设置错误时测试会失败并指出缺少的验证器路径。

### 当前分支全套测试

```bash
MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot \
  /path/to/MaiBot/.venv/bin/python -m pytest -q
```

入口、manifest 与协调器集成稳定后，审计运行结果为 `229 passed`。这仍是进程内自动化测试结果，不代表插件已在真实 Runner 中加载、连接聊天平台或完成新架构整曲验收。

## 发布阻断项

以下项目完成前，不应把插件描述为“新架构已通过官方 SDK/Host 验收”：

1. 保持 manifest 下界不低于已核实的 Host `1.3.0` + SDK `2.8.2`；若未来要放宽到旧版本，必须先证明可信 Command payload 与 detailed send `return_details` 契约。
2. 在干净 Host 安装目录执行真实 Runner load、配置生成、self-config 热重载和 unload。
3. 完成真实执行资产绑定：阶段计划必须绑定管理员配置并已校验的 worker、模型、索引、HuBERT、Demucs/RVC 资产与可执行 argv；当前自动化通过不能替代该项。
4. 验证所有后台任务、网络客户端、数据库句柄和 systemd 协调器在卸载/失败时按契约清理或保留。
5. 移除聊天携带 `/163cookie` 凭证的生产路径，并复核所有用户可见异常映射。
6. 完成当前架构的受限整曲、故障恢复、授权投递及“不重复发送”验收；旧 v0.4.0 音频报告不能替代。
