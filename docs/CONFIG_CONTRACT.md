# 0.5.0 配置契约（开发中，尚未部署）

本契约描述当前隔离分支，不是启用指令。配置结构以 [plugin.py](../plugin.py) 的 `SingPluginConfig` 为准，依赖与版本范围以 [_manifest.json](../_manifest.json) 为准。遵循 MaiBot 官方[配置管理](https://docs.mai-mai.org/plugin/config)与[生命周期](https://docs.mai-mai.org/plugin/lifecycle)约定。

## 配置来源与兼容性

- Runner 根据 `config_model` 生成、补齐当前安装实例的运行配置；插件不复制模板覆盖它。
- [config.example.toml](../config.example.toml) 只用于审阅。`plugin.enabled=false`，`plugin.config_version="0.5.0"`；启用需管理员明确决定。
- 当前验证环境为 Host 1.3.0 / SDK 2.8.2，Manifest 下界与此一致。不要开启强制兼容来绕过缺少的原始消息或详细发送回执契约。
- 0.4.0 的六个路径不足以启动新版。新增 HuBERT、本地 Demucs 仓库、RVC 上游目录；字段补齐为空不等于完成配置。

## `[local]` 字段

| 字段 | 当前契约 |
|---|---|
| `backend` | 只允许 `local` 的持久化受限渲染，不回退旧 sidecar。 |
| `output_dir` | 空字符串使用 SDK `ctx.paths.data_dir/covers`；覆盖值必须为绝对非符号链接目录。改变此项不会搬迁旧成品。 |
| `model_path` / `index_path` | 管理员固定配置的 RVC checkpoint 与 FAISS 索引绝对路径；请求不能提供任意模型或本地输入路径。 |
| `hubert_path` | 显式 HuBERT 绝对路径，不采用外部脚本的开发者默认路径。 |
| `demucs_repo_path` | 专用本地目录，只含 `htdemucs.yaml` 与 `955717e8-8726e21a.th`。bag 内容为 `models: ['955717e8']`，不得使用符号链接或加入其他模型。真实执行调用 `get_model('htdemucs', repo=...)`，不读默认远程仓库或缺失时联网补权重。 |
| `rvc_script` | 经管理员审查的固定 RVC wrapper 绝对路径。此插件针对当前 wrapper 契约，不执行任意聊天命令。 |
| `rvc_upstream_path` | 必须等于 `rvc_script` 同目录下的 `upstream`，对应 wrapper 真正导入的源码树；不能填另一份无关源码来伪造资产指纹。 |
| `worker_python` | 包含推理依赖的隔离 Python 绝对路径；本机验证为 Python 3.9。虚拟环境解释器的正常符号链接可以使用，不要替换成会失去虚拟环境语义的裸系统解释器。 |
| `inference_lock` | 与本机其他模型实验共享的绝对锁路径；模型服务内部取得，不能每个任务换一把锁。 |
| `max_queue` | 默认 2，范围 0–8。当前总未终结任务容量为 `max(1, max_queue + 1)`，计入搜索、候选等待、排队、运行和取消待对账；不是仅统计等待推理的队列。 |
| `max_duration_s` | 默认 300，配置范围 30–300；固定最短输入为 30 秒。 |
| `max_download_bytes` | 默认及最大 64 MiB，配置最小 1 MiB；还校验响应和流式累计字节。 |
| `timeout_s` | 保留的历史字段，不控制新版持久化阶段，也不是整曲 RPC 等待时长。不要通过增大它修复阶段问题。 |
| `musicdl_python` / `local_source_allowlist` | 历史兼容/维护者回归字段。新版在线翻唱使用同一登录态的 CatalogueService 精确 ID 取链，正常聊天不提供本地路径入口。 |

不随插件分发权重或推理环境。外部环境依赖与插件 Runner 自身的 Manifest Python 依赖分开管理；不要为部署插件改变 MaiBot 核心依赖。

## 请求、选曲和明确许可

```text
/翻唱 准确歌名 - 艺人名
/翻唱 准确歌名 - 艺人名 --album 专辑名 --source-id 平台曲目ID --with-instrumental --auto-reply
/翻唱选择 任务ID 序号
/翻唱状态 任务ID
/翻唱取消 任务ID
```

- `--auto-reply` 必须位于最后；仅该显式参数表示同意在完成后向原会话自动发送一次。没有该参数时仅保存结果，不请求自动投递。
- Command 校验原始宿主消息的消息 ID、平台、会话、用户和文本；参数从原始文本解析。候选约束和许可均参与请求内容一致性检查。
- 多个候选保留展示顺序，由用户选择；选定后只解析该平台和曲目 ID。不可下载、仅试听或过长时不换另一条录音。标注时长只能检查媒体长度一致性，不能证明 studio 或使用权。
- 候选快照默认有效 600 秒。过期后不能选择旧快照；当前需取消旧任务并重新请求，未实现自动过期回收器。
- 当前 Host 的 Tool 参数不是可信原始消息身份，因此 Tool 只给出原会话 Command 指引，不凭模型参数入队或投递。不能把“自然语言默认第一候选”的纯选择函数误称为已完成可信 Tool 自动任务集成。
- 取消不会撤回已开始的平台发送；`dispatching`、`unknown`、`sent` 不允许再次自动发送。当前没有公开的中断阶段重试命令，不能通过手改 SQLite、删除日志或换 unit ID 绕过所有权。
- `/163cookie` 已停止应用聊天携带的凭证。请通过受保护配置或 operator 扫码流程登录；不要在聊天中粘贴 token/cookie。

## 存储与恢复

SDK 数据根目录内：

```text
.durable-scheduler.lock
jobs.sqlite3                  # jobs / offers / stage attempts / delivery
jobs/<job-id>/                 # 私有输入、配方、计划、阶段收据、日志
covers/<recipe-key>/           # 永久 cover.mp3 + metadata.json
covers/songs/                  # 可浏览硬链接
covers/library-index.json
covers/README.md
```

- 进程级 scheduler 锁防止两个实例同时恢复同一队列；线程 offload 持有重复文件描述符，卸载不能提前释放尚未结束的同步操作所有权。
- 每个阶段先持久化唯一 unit 身份，再执行受限服务。重启先对账既有 unit，不以“当前没查到进程”猜测上次已经失败。
- 缺少启动证据、systemd 状态未知或锁被占用时保留任务槽；`coordinator-error.json` 提供受限原因并指数退避，不能通过自动重试掩盖未知状态。
- source 在全量接收、文件 fsync 后以不覆盖的 link 提交。提交前取消可留下 `.part`；提交后取消保留完整 source，后续仍须探测校验。取消不是回滚已完成的磁盘提交。
- 完成阶段的收据和字节保留，不把成功分块当临时缓存删除。永久 MP3 只在来源、配方及全部计划收据验证后原子提交。
- 配方 `sing-render-v2` 绑定真实源码、权重、本地 Demucs 仓库和实际 argv；忽略可再生成的 `.git`/`__pycache__`，不忽略源码。记录的依赖版本不等于整个操作系统/二进制环境的可复现指纹。
- 旧 0.4.0 成品走独立 legacy 验证，保持原字节与位置；旧开发配方不作为 v2 缓存使用。不删除旧成品来“修复”新版本校验。
- 当前没有自动删除失败任务中间件或永久音频的保留期任务。管理员须监测磁盘；清理前证明没有活跃/未知 worker 并备份证据。

## 有界执行和投递

每个模型阶段独立 `systemd-run --user`：`MemoryMax=4G MemoryHigh=3G MemorySwapMax=0 CPUQuota=150% TasksMax=64 TimeoutStopSec=15`。模型进程自检 cgroup，并在服务内部持有共享推理锁。

阶段软期限：decode 120 秒、separate 600 秒、每个 convert 180 秒、mix 60 秒、encode 90 秒、validate 60 秒；unit 额外给予 30 秒退出/写证据余量。RVC 单次音频至多 25 秒。没有一次整曲 900 秒 RPC，也不承诺固定整曲完成时间。

固定基线参数：`pitch=0 f0_method=harvest index_rate=0.5 filter_radius=3 rms_mix_rate=0.25 protect=0.33 seed=20260928`，无自动变调。`protect=0.50` 在该上游实现中禁用保护，不是更强保护。

消息投递只消费持久许可：`pending → dispatching → sent/unknown`。只有正向回执且非空平台消息 ID 才能证明 `sent`；当前 SDK 的 `False` 也可能出现在平台成功后的存储异常，故按未知处理。重启不会把未知发送退回 pending。它是“最多尝试一次”，不是恰好送达一次；在 claim 与实际发送之间崩溃，可能零次送达。

音色身份、训练素材权利及用户反馈的沙哑问题仍需单独评估；基础设施验收不等于音质修复。
