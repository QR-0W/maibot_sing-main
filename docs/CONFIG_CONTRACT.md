# 0.4.0 配置契约（本地 Linux 后端）

- `[plugin] enabled=false` 的真实运行配置由管理员控制；不自动修改。`config_version="0.4.0"` 与 manifest 版本一致。
- `[local] backend="local"` 默认本地；`backend="legacy"` 仅显式选择且当前 Linux 限制下拒绝无隔离推理。`output_dir=""` 默认 SDK `ctx.paths.data_dir/covers`，显式覆盖必须是绝对路径；永久结果 `<key>/cover.mp3`、`metadata.json`，`jobs/<request-key>.json` 显示 queued/processing/completed/failed/cancelled/interrupted，`requests/<request-key>.json` 指向以官方源曲目 ID 为身份的真实缓存 key；不归五天缓存清理管辖。
- `[local] model_path`、`index_path`、`worker_python`、`musicdl_python`、`rvc_script`、`inference_lock` 必须由管理员提供实际绝对路径，默认全空并拒绝运行；不依赖维护者机器上的 `audio-lab`。模型/index SHA256 是缓存身份；不接受聊天输入中的文件路径。`local_source_allowlist` 只用于受控回归。
- `[local] max_queue=2`（不含运行中的 1 个）、`max_duration_s=300`、`max_download_bytes=67108864`、`timeout_s=900`（固定最短30秒，无独立配置字段）；超限拒绝而不退到旧 sidecar。输入歌曲必须明确匹配标题与艺人；同名多条歧义要求用户明确给出专辑或曲目 ID，短片段拒绝，解码时长只检验与来源元数据是否一致、不证明录音室身份。musicdl 在受限 worker 中仅使用官方源，不用第三方解锁。
- 资源隔离：每个 worker 使用 `systemd-run --user` 服务 `MemoryMax=4G MemoryHigh=3G MemorySwapMax=0 CPUQuota=150% TasksMax=64 TimeoutStopSec=15 RuntimeMaxSec=900`；worker 自检 cgroup 后在服务**内部**取得配置的 `local.inference_lock`；不同 worker/实验必须共享同一锁，不能在外层叠套另一个自动上锁脚本。分离与转换在不同 Python 子进程运行。
- 固定 RVC v1/40k：`pitch=0 f0_method=harvest index_rate=0.5 filter_radius=3 rms_mix_rate=0.25 protect=0.33 seed=20260928`。自动变调禁用。说话默认不调用 legacy sidecar；未隔离时直接报错，不影响原生 MiMo TTS (`rvc_after_tts=false`)。
- `[rvc] auto_start=false`，任何版本不匹配的端口服务都不得根据健康检查中的 PID 结束。保留配置字段仅为历史兼容。发送成功/失败不改变已完成成品；未知投递不重试。
- 不提交 `config.toml`、登录态、永久音频、模型和中间文件。管理员须确认 NapCat 能访问永久输出目录方可发送文件 URI。
