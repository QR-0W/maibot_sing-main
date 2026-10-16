# 插件、引擎与私有数据边界

本文描述 0.6.0 候选源码的运行和交付契约，不是现网部署或媒体验收证明。项目目录及迁移规则见 [PROJECT_LAYOUT](<PROJECT_LAYOUT.md>)。

## 发布内容

插件仓库交付 Manifest v2、SDK 入口、自有 services/runtime 模块、[RVC 适配器](<../engines/rvc/rvc_infer.py>)、测试和公开文档。适配器复用 runtime worker 的冻结预算校验；不是独立实验启动器，不应套用外部 rvc.sh 或 run_limited.sh。

第三方 RVC upstream、独立 Python 环境、RVC 权重、FAISS 索引、SHA 固定的 HuBERT 和离线 Demucs 资源由管理员准备，不自动下载。现有资产契约要求 `rvc_upstream_path` 等于 `rvc_script` 的同级 `upstream/`。使用仓库适配器时对应 `engines/rvc/upstream/`，该目录不跟踪、不进入插件发布包，管理员必须保留第三方 LICENSE 和 notices。适配器的 reference commit 是来源参考；实际源树字节由 AssetInventory 绑定，不能把常量当作已核验的 checkout。

完整的引擎环境锁、传递依赖许可证审查和新适配器的真实兼容性验收仍待完成。本项目目前不是一键安装包，也不保证任意管理员脚本可兼容。模型卡的代码许可证不自动授予录音、声音、歌曲或训练数据权利。

## 两个 Python 环境

1. SDK Runner：Host/SDK 版本遵守 Manifest；插件轻依赖为 aiohttp、httpx、cryptography、segno。不得修改 MaiBot 核心依赖来安装媒体库。
2. 管理员指定的 `worker_python`：隔离 Demucs/RVC 环境，需要 torch、torchaudio、numpy、demucs、soundfile、librosa、pyworld、FAISS、fairseq、scipy、praat-parselmouth、torchcrepe、OmegaConf、numba及实际传递依赖。历史 Python 3.9/旧 torch 组合记录不等于新版锁定、ABI 或安全验收。

`musicdl_python` 是退休兼容字段，不是当前生产链的第三个必备环境。RuntimeVersionProbe 读取包/编码器元数据，不导入模型；版本存在不等于推理可用。受限反序列化策略也不能代替依赖漏洞审查。

系统必须提供 Linux cgroup v2、systemd user manager、memory/cpu/pids 控制器、ffmpeg/ffprobe及 libmp3lame 编码器。共享锁父目录和持久化目录须有正确权限与足够磁盘空间。

## 预算与恢复

每个可达媒体阶段，包括来源探测、解码、分离、转换、混音、编码和校验，都运行在独立有限服务中。默认 MemoryMax=2048 MiB、MemoryHigh=1536 MiB、swap=0、CPUQuota=150%、TasksMax=64、有限 RuntimeMaxSec、KillMode=control-group、OOMPolicy=kill及实际 memory.oom.group=1。参数由一个验证后的冻结预算传递，不回退 4 GiB，不通过涨额度掩盖失败。

启动前检查目标服务的有限共享祖先，必须有完整预算加默认 512 MiB 余量；服务内拿到管理员配置的共享 inference lock 后、加载模型前再检查。所有重工作串行，旧 unit/子进程未证明停止前不释放槽。未知或不足就拒绝，不启动媒体；仅真实 systemd/cgroup 证据可以确认 OOM。

存储绑定与实时资产代际分离。启动、旧 unit 对账、失败通知和已完成任务的不可变发布不要求今天的模型/解释器/执行器仍存在；新接纳、新阶段必须验证实时资产及版本。新 launch 前验证实际执行文件。旧 claimed unit 必须先对账，再验证 probe 实际证据，避免丢失文件挡住取消或终止结算。发布仍验证冻结来源、收据、配方及实际成品字节，不发送半成品、不自动重试。

## 持久化和服务生命周期

数据库、工作区根及永久 covers 根采用 SDK 数据目录或管理员显式配置的合法绝对目录。首次绑定后不能用热更偷偷搬迁。浏览目录、索引与成品不依赖私有实验目录，也不随源码升级删除。

QQ/NapCat 独立于插件生命周期。部署应先核对 SDK Supervisor 的反向依赖，使用受信管理入口重载；不假设存在 HTTP reload 端点，不杀整组 Runner 或 QQ 来模拟单插件更新。旧任务不会被追加新发送许可，音频和失败通知保持各自的持久化单次尝试语义。

## 当前证据范围

预算模块定向 80 项通过是已有执行证据；随后新增入口、probe、发布、恢复、适配器回归未执行，不能引用历史总数当作当前全量通过。本轮尚未把 0.6.0 应用到现网；现网观察为 0.5.0 且已启用。用户已授权后续应用更新，但授权不等于完成安全验收和重载。

纯测试须在 512 MiB/high384 MiB 隔离服务中逐目标/小组运行，有限共享祖先至少有 1 GiB 余量。真实模型需要单独许可、至少 2.5 GiB 余量及 2048/1536 MiB 媒体预算。实际 QQ 验收另需许可，不自动发送、重登或重启 QQ。
