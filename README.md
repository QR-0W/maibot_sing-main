# maibot-sing

MaiBot 插件：通过音乐来源搜索与 RVC 本地处理生成歌曲翻唱。此仓库是 [xiaocutedog/maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main) 的 QR-0W fork；上游原有的说话/MiMo TTS 等能力及致谢信息见下文。当前 `feat/conversational-singing` 功能分支**尚未合并、尚未部署**；插件与配置版本保持 `0.5.0`。可用语法及限制见 [对话式翻唱](<docs/CONVERSATIONAL_SINGING.md>)，本地后端、持久化及安全约束见 [部署与数据说明](<docs/DEPLOYMENT.md>)、[配置契约](<docs/CONFIG_CONTRACT.md>) 和 [SDK 合规审计](<docs/SDK_COMPLIANCE.md>)。历史 0.5.0 验收不能替代本功能分支验收；本次不在运行 profile 启用，也不测试真实 QQ。

## 功能概览

- 请求已知歌曲并进行人声分离、RVC 转换，可选混回伴奏。
- 本地后端复用已有 Natsume Iroha RVC v1 模型/index，不重新训练或声称存在英语版 Iroha CV。
- 完成的翻唱持久保存为 MP3 与 provenance 元数据；临时下载、分离 stems 等 scratch 与成品分开管理。
- 新任务使用 SQLite 持久账本：搜索/候选等待、queued、running、ready、failed、cancelled、interrupted；渲染与消息投递分别记录，不能把 ready 说成平台已送达。
- 旧 sidecar 仅为兼容路径，不会成为本地后端失败后的隐式无界 fallback；以契约规定为准。

> 运行环境、隔离要求、输出目录和部署前检查见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。操作前先审阅 [config.example.toml](config.example.toml) 与当前 [CONFIG_CONTRACT.md](docs/CONFIG_CONTRACT.md)。不要把包含凭证的真实 `config.toml` 提交到版本库。

## 安装与启用

本插件遵循 MaiBot 官方的 [Vibe Coding 插件开发指南](https://docs.mai-mai.org/plugin/vibe-coding)、[Manifest](https://docs.mai-mai.org/plugin/manifest)、[生命周期](https://docs.mai-mai.org/plugin/lifecycle) 与 [配置管理](https://docs.mai-mai.org/plugin/config) 约定。将仓库作为独立插件目录放入 MaiBot 的 `plugins/` 下；不要复制文件到 MaiBot 的 `src/`、`dashboard/` 或全局 `config/`：

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/QR-0W/maibot_sing-main.git maibot-sing
```

重启 MaiBot 后，在插件管理界面确认 manifest 校验与依赖解析没有报错，再由管理员显式启用。插件默认 `plugin.enabled = false`；在受限本地后端的模型路径、隔离 Python、推理锁和输出路径完成审查前，不应启用渲染。

## 配置

配置结构和默认值唯一由 [plugin.py](<plugin.py>) 中的 `config_model` 定义。Runner 首次加载时在已安装插件目录生成 `config.toml`，后续模型新增字段也由 Runner 补齐；仓库中的 [config.example.toml](config.example.toml) 仅供审阅，不是运行时配置源。请通过 WebUI 或 Runner 管理的运行时配置修改当前实例，不要提交生成的 `config.toml`、登录态、密钥或数据库。

关键部署字段及边界见 [配置契约](docs/CONFIG_CONTRACT.md)。`local.backend` 应保持为受限的 `local`；模型、索引、HuBERT、Demucs、RVC 脚本、隔离解释器和共享推理锁均由管理员提供绝对路径。旧 sidecar 配置不能替代受限本地执行路径。

## 请求入口与操作

两种语法进入**同一个 `@Command("翻唱")`**，沿用 Host 的命令禁用/权限检查；不是 Hook 副作用，不借用 Tool 身份，也不是任意 LLM 聊天都能触发。

| 入口 | 默认渲染 | 默认伴奏 | 默认投递格式（须另行授权） |
|---|---|---|---|
| `/翻唱 准确歌名 - 艺人名` | `full` 整曲 | 有伴奏 | `file` 文件 |
| `唱一下《准确歌名 - 艺人名》`（也接受 `唱一段《…》`、`唱《…》`） | `excerpt` 12–18 秒片段 | 干声、无伴奏 | `voice` 语音 |

- 两入口都可覆盖 `--full` / `--excerpt`、`--with-instrumental` / `--without-instrumental`、`--file` / `--voice`；每个维度最多指定一次，冲突和同值重复均拒绝，不采用“最后一个生效”。
- 保留 `--album 专辑名`、`--source-id 平台曲目ID`、`-v 管理员固定音色`；这些选择器也不能重复，不能借 `-v` 传任意模型路径。
- **两入口都必须在原消息末尾唯一追加字面量 `--auto-reply`，才授权完成后向原会话最多尝试一次自动投递。没有这个原文 flag，均只生成保存，不自动发送音频**；`--voice` / `--file` 仅选格式，不授予发送许可。合法请求仍可返回任务/候选文本。

以下仅为语法示例，不是发送或执行指令（不应在真实 QQ 上自动试发）：

```text
/翻唱 Creep - Radiohead
唱一下《Creep - Radiohead》
唱一段《Creep - Radiohead》 --full --with-instrumental --file
/翻唱 Creep - Radiohead --excerpt --without-instrumental --voice --auto-reply
```

任务操作与其他命令：
- `/翻唱选择 <任务ID> <序号>`：从插件已显示的候选快照中选择版本。
- `/翻唱状态 <任务ID>`：查询渲染与投递状态。
- `/翻唱取消 <任务ID>`：持久化取消请求；不能撤回已被平台接受的发送。
- `/说 <文本>`：按管理员的 MiMo 配置生成语音，仅发起一次文件 URI 投递；未知回执不换消息类型重发。
- `/音色列表`：显示当前已接纳服务的固定本地模型文件名，不查询已停用的旧 sidecar；文件名不代表角色身份或素材权利已验证。
- `/qq音乐登录`、`/网易云音乐登录`、`/163logintest`、`/qqlogintest`：仅 operator 使用的登录与诊断命令。

**消息来源限制：**上述所有命令仅接受当前 NapCat/Host 账号与目标元数据可验证的 QQ 直接文字。raw Host 必须为 1–32 个直接 text 组件，空格拼接后 ≤2048 字符并与 Host 原文完全一致；拒绝 @、引用、语音、转发、card、notify。`reply_to` 可省略或为 `None`，其他非 `None` 值均拒绝。不可信来源连拒绝文本也不发送。普通 WebUI、WebUI 虚拟 QQ 和缺字段旧适配器不能用于生产自动测试。来源证明为 `napcat-direct-text-v2`；旧 v1 或缺证明的 pending 不自动投递，已 sent/unknown 不重发，不手改账本追认许可。详见[配置契约](<docs/CONFIG_CONTRACT.md>)。

**投递边界：**file/voice 均要求 detailed ack 为 `sent=True` 且有有效非空字符串 `message_id`，否则为 unknown；不换格式 fallback、不自动重试。真实 QQ file 尚未测试，本次不部署 profile、不做 QQ 试发。

插件还声明了供 LLM 发现的翻唱与说话 Tool，但当前 Host 的 Tool 参数缺少可信原始消息锚点：它们只给出原会话 Command 指引，不凭自由会话 ID 入队或发送，默认也不要提升为核心工具。`/163cookie` 已停止解析和应用聊天输入中的秘密，仅指向配置或扫码流程；不要在聊天粘贴凭证。实际启用前请核对 [SDK 合规审计](docs/SDK_COMPLIANCE.md) 和[配置契约](docs/CONFIG_CONTRACT.md)中的边界。

## 模型来源与限制

使用的角色模型候选是《碧蓝档案》枣伊吕波（Natsume Iroha），不是《魔法纪录》环彩羽（Tamaki Iroha）。现有公开材料倾向于日语语料，但作者没有公开训练集清单，因此训练语言与内容仍属未完全核实；“国际服”不等同英语配音，也不代表英语CV或英语训练数据存在。模型卡上的许可 tag 不等于游戏录音、声优表演或所有相关声音素材均获授权，更不构成商业使用许可。使用者须自行确认适用权利及许可。

模型选型暂留现有 Iroha baseline，没有已证同身份且更好的替代；`sid=0` 可加载、输出 finite 不证明角色身份、素材权利或品质，人工听辨待做。用户研究报告另存、不提交；本机路径、私有模型 hash 日志、音频和 credentials 不复制进 Git。

excerpt 采用速度 B：先分离整曲，再以 50 ms RMS/flatness 选一段，不足时最多两段按原时间顺序拼接，保留 ≤0.6 秒换气、丢弃 <1.2 秒碎片，以低能量端点、50/120 ms 淡入淡出及 RMS 归一处理，只执行一次 RVC。这不是副歌识别或语义乐句边界检测，不保证歌声质量或固定耗时。

新音频配方为 `sing-render-v3`，绑定 selector hash、`render_mode` 与实际 argv；`delivery_mode` 不改变 audio cache。metadata 保留精确源帧范围、selection 与 receipt，目录毫秒标签只是舍入显示。已完成 v2 成品只读兼容，旧 v2 运行中 plan 不直接升级续跑。细节见[对话式翻唱](<docs/CONVERSATIONAL_SINGING.md>)。

## 依赖与安全

插件依赖的唯一权威来源是 [_manifest.json](_manifest.json) 的 `dependencies`。MaiBot Host 负责检查冲突并安装缺失依赖；不要依据 `requirements.txt` 手工改动 MaiBot 核心环境，也不要把该文件当成插件加载契约。不得把密钥、登录态、模型、歌曲音频、日志、SQLite 数据库或运行配置提交到仓库。推理和资源限制要求见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 上游说明与鸣谢

本项目基于 [xiaocutedog/maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main)，上游 README 中的原始功能介绍与致谢如下。此 fork 的后端和运行约束可能与上游说明不同；请勿据上游旧部署步骤覆盖本仓库文档。

- [ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot) — MaiBot 的 Xiaomi MiMo v2.5 音色克隆语音回复插件。
- [maibot-music](https://github.com/pan-ice/maibot-music) — MaiBot 音乐插件，支持搜索点歌、解析音乐链接、发送语音音频。
- [Retrieval-based-Voice-Conversion-WebUI (RVC)](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — 语音音色转换框架。
- [Ultimate Vocal Remover (UVR5)](https://github.com/leebufan/Ultimate-Vocal-Remover) — 人声/伴奏分离工具。

## 验证

独立仓库测试：

```bash
python -m pytest tests -q
```

如需调用指定 MaiBot checkout 的真实 `ManifestValidator`，并用当前解释器中实际安装的 `maibot_sdk` 生成配置 Schema，请显式提供只读 Host 根目录并使用该 Host 的 Python 环境：

```bash
MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot \
  /path/to/MaiBot/.venv/bin/python -m pytest tests/test_manifest.py -q
```

该测试只导入插件定义、校验 manifest 并构建配置 Schema；不会调用 `create_plugin()`、`on_load()`、模型推理或消息发送。未设置 `MAIBOT_TEST_HOST_ROOT` 时，独立仓库会跳过 Host 专属校验，而不是猜测本机目录。本次审计实际覆盖 Host `1.3.0` 与 SDK `2.8.2`；manifest 的最低版本已收紧到这两个实测版本，因为可信 Command 原始消息 payload 与 detailed send `return_details` 是授权投递所需契约。此结果不承诺更旧版本兼容，也不等于正式 Runner 加载验收。完整的已验证项和待验项见 [SDK 合规审计](docs/SDK_COMPLIANCE.md)。

### 历史 v0.4.0 音频实验

旧实现曾完成片段、202 秒整曲及两段 30 秒试听，记录峰值内存约 1.83–1.94 GiB，详见 [历史验证记录](docs/VALIDATION.md)。这些结果仅说明当时的音频实验与资源测量，不证明当前持久化队列、Runner 生命周期、配置热重载、Host 加载或授权投递已经验收通过。

## 常见问题

- **插件未出现在管理界面**：先查看 Host 的 manifest 校验错误，确认目录根部包含 `_manifest.json` 与 `plugin.py`，并检查 Host/SDK 版本是否落在 manifest 的闭区间内。
- **依赖冲突或缺包**：以 `_manifest.json` 的 `dependencies` 为准排查 Host 依赖解析结果；不要手工把 `requirements.txt` 安装进 MaiBot 核心环境来掩盖冲突。
- **没有生成 `config.toml`**：确认 Runner 已成功导入入口并识别 `config_model`。该文件应由 Runner 在安装目录生成，不要从仓库复制真实配置。
- **配置页面无法渲染**：运行上面的 Host 专属 manifest/Schema 测试，检查 `PluginConfigBase` 字段是否都有默认值、`plugin.config_version` 是否存在。
- **任务无法开始**：确认插件仍为管理员有意启用，并逐项检查受限本地后端所需的绝对路径、systemd user service、共享推理锁与资源限制；不要回退到无隔离推理。
- **发送结果不确定**：不要自动重发。保留 JobStore 与私有日志供协调器对账，只有平台明确确认后才标记送达。

## 已知限制

当前权重在部分高音区会唱不上去或出现破音，《Aoi》开头等片段还原也不理想；这是尚未修复的音质问题，不是下载或编码缺陷。历史音质结果不能替代当前架构的整曲、生命周期与投递验收；在 [SDK 合规审计](docs/SDK_COMPLIANCE.md) 的待验项完成前，不应宣称新架构已经发布就绪。
