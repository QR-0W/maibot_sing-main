# 旧实验产物清理计划与执行记录

此清单用于 Lead 在插件回归及首个永久成品落盘后逐项复核。**本文作者不删除、不移动任何文件。** 仅在确认文件精确存在、没有运行中任务/生产引用、保留记录和哈希需求均已处理后，Lead 才可删除所选旧测试内容；禁止对上层目录做宽泛递归删除。

## 保留，不列入清理

- 原曲：`/home/qr0w/music_downloads/Neutral Milk Hotel - In the Aeroplane Over the Sea.mp3`。
- Natsume Iroha 权重、配套索引、来源及校验记录：`/home/qr0w/audio-lab/models/natsume-iroha/`，尤其 `extracted/NatsumeIroha/NatsumeIroha.pth`、`extracted/NatsumeIroha/added_IVF186_Flat_nprobe_1_v1.index`、`source_and_loader.json`、`SHA256SUMS` 与来源 ZIP。
- 来源/许可与模型研究：`/home/qr0w/audio-lab/notes/natsume-iroha.md`。
- 可复用 RVC 引擎：`/home/qr0w/audio-lab/tools/rvc/`；已有隔离环境 `/home/qr0w/svc-bench/.venv39/` 及该环境使用的 HuBERT 文件。不要安装依赖或改动旧环境。
- musicdl 源码与现有环境：`/home/qr0w/musicdl/`、`/home/qr0w/musicdl-run/.venv/`。
- 新版永久歌曲成品库（如已创建）：插件 `data_dir/covers/` 或管理员设置的绝对 `output_dir`。成品及其元数据不能由五天语音临时缓存清理器删除。
- 当前冻结回归输入 `/home/qr0w/audio-lab/work/20260928_english_iroha/inputs/`：至少保留到 Lead 完成回归并确认首个永久渲染；若确需清理，由 Lead 单独确认并记录。

## 可评估候选（必须按文件清单审查）

### 旧 sing_samples 产物

候选旧归档目录：`/home/qr0w/sing_samples/archive/20260928_before_cleanup/`，包含旧对比、TTS、音色转换与 `real_song_preview_01/` 测试文件。`move_manifest.json` 和 README 是其来源/归档说明，删除音频前须确认是否仍需保留这些审计材料。旧文件名可能是不准确的实验标签，不能据文件名推断内容或模型身份。

候选当前对比集（**不是默认可删**）：`/home/qr0w/sing_samples/current/00_original.mp3`、`01_vctk_english.mp3`、`02_natsume_iroha.mp3`。它目前是有 README 与记录的试听对比交付物；仅在 Lead 另有保留副本、判断不再需要后才可列入实际清理。

### audio-lab 旧运行目录

候选旧实验根目录：`/home/qr0w/audio-lab/work/20260928_english_iroha/`。其内容包括冻结输入、Natsume 静态检查记录、VCTK/Natsume 原始试听输出、处理中间文件、发布副本及比较记录。这里有不可替代的研究/验证记录；应逐个子目录判定，不得整体盲删。上面的 `inputs/` 明确暂缓。引用检查发现 `/home/qr0w/audio-lab/tools/publish_comparison.py` 指向该 run 与 sing_samples/current，`/home/qr0w/audio-lab/tools/rvc/validate_audio.py` 和 run 内静态检查脚本也引用其子路径；Lead 若打算删除，应先确认这些脚本不再使用或更新引用。

### svc-bench 临时输出

已核查候选路径 `/home/qr0w/svc-bench/test_song/`，当前 glob 可见：

- `mix.wav`
- `separated/htdemucs/mix/vocals.wav`
- `separated/htdemucs/mix/no_vocals.wav`

它们是测试输入/分离结果候选，不是 `svc-bench` 引擎源代码。检查时 `/home/qr0w/svc-bench/raw/` 与 `/home/qr0w/svc-bench/results/` 不存在；不得因计划文字而假定存在或删除它们。`svc-bench/so-vits-svc/` 是引擎代码，不是清理目标。

## 执行前检查与记录

1. 等待 Sol 完成实现；由 Lead 完成配置与插件离线回归，并确认成品已持久落盘。
2. 对每个拟删路径重新检查实际存在性、文件类型、大小、引用与是否仍被任务使用；确认目标没有误包含原曲、模型/索引、HuBERT、环境、研究来源、冻结输入或永久 covers。
3. 只针对批准的精确候选文件/目录操作。保留必要清单/哈希，写明时间、精确路径、数量及删除原因；不做 `rm -rf` 上层目录或通配符式批量删除。
4. 将执行记录放在永久 covers 之外，且不提交音频、模型、密钥、运行配置或运行时产物。

## 配置与清理实现边界

`config.example.toml` 依据 `plugin.py` 中的 `SingPluginConfig` schema 对齐，并保持默认禁用且不含凭证。schema 没有 `min_duration_s`、scratch 根目录、musicdl/分离器路径等配置键。临时 worker 数据位于 `output_dir/.scratch/`，正常任务退出时在 `finally` 中清理；异常进程退出可能遗留目录，新增启动恢复：只处理记录中可验证归属且 systemd 确认已停止的 worker；没有定期 scratch 清理器。永久成品、任务记录、请求映射不得纳入临时清理。若配置契约对上述实现有不同陈述，应由配置契约所有者确认/修订，不可推测添加 schema 字段。

## Lead 已执行：2026-09-28

首个永久成品及完整歌曲通过回归后，按用户许可逐文件校验并删除 50 个旧测试音频，共 224,701,886 bytes。包括旧归档 WAV/MP3、过时比较副本、svc-bench 测试分离音轨和隔离的中断 smoke WAV。未递归删除根目录；研究、JSON、日志和冻结输入保留。上述章节是执行前的候选规划，最终范围以本机 `audio-lab/work/plugin-integration/cleanup-record.json` 为准。详见 [验证记录](VALIDATION.md)。
