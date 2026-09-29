# MaiBot Plugin SDK 合规审计

审计日期：2026-09-29

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
