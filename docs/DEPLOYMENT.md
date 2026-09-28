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

本机受限于 8 GiB 容器内存。任何安装、模型加载或推理均必须由维护者在专用 systemd 用户服务中运行，并满足：`MemoryMax=4G`、`MemoryHigh=3G`、`MemorySwapMax=0`、`CPUQuota` 不超过 150%、明确的运行时上限；共享互斥锁 `/home/qr0w/audio-lab/inference.lock` 必须在服务内部取得。无法建立并验证约束时不得继续，也不得改用不受限执行。使用已有 `/home/qr0w/audio-lab/tools/rvc/rvc.sh` 时不要在外层重复套用其锁定调用；资源执行细节须由维护者核验。

不要修改既有 `svc-bench/.venv39`、musicdl 环境或 MaiBot 依赖；不要把大文件放在 `/tmp`。模型、索引、HubERT 和引擎不是插件包内容。不要下载/训练新模型以满足本部署说明。

## 歌曲来源与模型表达

仅使用配置/实现允许的官方来源与来源身份校验；明确标题和艺人，拒绝试听片段、不可用资源或含糊匹配，不使用第三方解锁服务。用户输入不能成为任意本地路径或 shell 命令。

Natsume Iroha 候选模型推定以日语素材为主，但训练清单不公开。国际服不是英语CV的证据；模型许可 tag 不是游戏录音、声优表演或其他素材均已授权的证明。不要称其为“英语CV模型”或暗示商用已获许可。

## 配置示例与实现边界

`config.example.toml` 按插件 `SingPluginConfig` schema 给出无凭证示例，`[plugin].enabled=false` 且 `config_version="0.3.1"`。本地配置包括 schema 实际声明的队列、时长、下载上限和 allowlist；没有 `min_duration_s` 字段，也没有可配置 scratch、分离器或 musicdl 路径。固定模型/index 默认路径来自后端常量。

历史 `[rvc].auto_start` 默认关闭；本地后端不会启动旧 sidecar。当前 `mimo.rvc_after_tts=true` 会在说话 RVC 路径明确报错，因此示例设为 `false`，可使用原生 MiMo TTS。配置契约中与 schema/实现不一致的细节需由实现负责人修正，不能靠添加不存在的示例键掩盖。真实凭证配置留在本机，不要提交。