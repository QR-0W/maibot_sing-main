# 部署与数据保留

## 当前阶段

插件须先由维护者完成审查与回归；本文件不是启用指令。实际运行配置由管理员控制，保持 `enabled=false`，直到 Lead 明确批准。不要为验证文档而启动 QQ、发送聊天消息或重启现有服务。不要修改 MaiBot 核心、父目录依赖锁文件或本机真实 `config.toml`。

## 持久成品与临时缓存

- 默认成品目录按配置契约使用插件 SDK 的 `ctx.paths.data_dir/covers`；管理员显式指定 `output_dir` 时，必须是绝对路径。以最终 schema/实现为准。
- 每首成功完成的歌曲先原子写入永久 MP3 与清理后的 provenance/参数元数据，再报告或尝试发送。任务记录（queued、processing、completed、failed、cancelled、interrupted）与请求到缓存结果的映射也用于状态/去重。
- 永久 covers **不属于**五天语音缓存，不得由临时缓存的定期清理任务自动删除。管理员需确认输出目录持久、空间可用，并在启用发送前确认 NapCat/发送端能访问该路径。
- 下载的歌曲、分离 stems 和转码中间文件位于 `output_dir/.scratch/<request-key>-<随机标识>/`。正常成功/失败/取消后清理；永久目录仅含 `cover.mp3` 与 `metadata.json`。Worker 的实时日志持久化到 `jobs/<request-key>.log`，元数据保留实测 cgroup 资源上限、峰值与 OOM 计数。
- 每个成品库持有独占文件锁。启动时仅对任务记录中可验证归属且 systemd 确认已停止的 worker 清理 scratch，标为 `interrupted`；仍在运行、状态未知或旧记录无 unit 身份时拒绝接收新任务，交由管理员核查。不会猜测 PID 或删除永久成品。
- 缓存命中再次校验 MP3 的 SHA-256。投递成功在内存中去重 600 秒；明确失败允许用户重新请求并复用落盘文件；结果未知保守去重 3600 秒且不自动重试。这不是跨进程重启的永久消息投递账本。
- 发送失败或结果未知不撤销本地已完成成品；发送状态与推理结果是不同状态。结果未知时不应盲目重发，具体行为以实现为准。

完整路径约束、文件身份/哈希、默认参数、时长/下载/队列限制及来源匹配规则见 [CONFIG_CONTRACT.md](CONFIG_CONTRACT.md)。

## 资源与外部组件

管理员先核对实际容器 cgroup 内存，而非主机 RAM；**容器至少应能为 4 GiB worker 与其他常驻服务留出余量**。每个推理 worker 使用独立 systemd 用户服务并验证 `MemoryMax=4G`、`MemoryHigh=3G`、`MemorySwapMax=0`、`CPUQuota≤150%` 和运行时上限；共享锁路径由 `local.inference_lock` 指定，须在 worker 内部取得。隔离不可用时不回退无约束推理。插件不会启动一个替代 QQ 进程；NapCat 应由管理员另行管理长期服务。

管理员须准备并在 `local` 配置六个实际绝对路径：模型、索引、隔离 worker Python、隔离 musicdl Python、受限 RVC 脚本和共享推理锁；不能指向实验目录的固定用户名。模型、索引、HuBERT、外部引擎和独立 Python 环境不包含在插件包内；不可因此声称一键安装，且不要为迁移而私自下载权重或训练素材。参见 [部署边界](DEPLOYMENT_BOUNDARY.md)。

## 歌曲来源与模型表达

仅使用配置/实现允许的官方来源与来源身份校验；明确标题和艺人，拒绝试听片段、不可用资源或含糊匹配，不使用第三方解锁服务。用户输入不能成为任意本地路径或 shell 命令。

Natsume Iroha 候选模型推定以日语素材为主，但训练清单不公开。国际服不是英语CV的证据；模型许可 tag 不是游戏录音、声优表演或其他素材均已授权的证明。不要称其为“英语CV模型”或暗示商用已获许可。

## 配置示例与实现边界

`config.example.toml` 按插件 `SingPluginConfig` schema 给出无凭证示例，`[plugin].enabled=false` 且 `config_version="0.4.0"`（离线修复分支待单独升级版本）。`local` 包含队列、时长、下载上限、allowlist 和六个**必须手工配置**的外部运行时路径；没有 `min_duration_s`、可配置 scratch 或通用分离器字段。模型/index 不再采用维护者机器的默认路径，缺失时拒绝启动本地后端。

历史 `[rvc].auto_start` 默认关闭；本地后端不会启动旧 sidecar。当前 `mimo.rvc_after_tts=true` 会在说话 RVC 路径明确报错，因此示例设为 `false`，可使用原生 MiMo TTS。配置契约中与 schema/实现不一致的细节需由实现负责人修正，不能靠添加不存在的示例键掩盖。真实凭证配置留在本机，不要提交。