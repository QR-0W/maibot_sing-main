# 0.6.1 部署边界、隔离验证与数据保留

本更新统一excerpt为完整源30–900秒、中心上下文≤90秒、选段12–18秒；full仍≤配置上限且最多300秒。仅在用户明确授权后更新/重载现有插件，不改运行配置、核心或依赖，不试发QQ。

安全应用：在监听目录之外完整staging，纯目标串行验证后检查现网文件未并发变化、配置字节一致和同一磁盘文件系统，再原子交换整个插件目录；保留旧目录回滚。若目录交换不能保证内部文件事件，补一次入口文件mtime事件，让原生watcher读取完整版本。必须用Supervisor重启完成、组加载失败=0及0.6.1/source-window-v4实际加载标记核实，不以复制成功宣称加载。

当前是未发布功能分支，**不是上线指令**。不修改 MaiBot 核心、核心依赖、运行配置、全局缓存或无关服务；不在现有 profile 安装/启用，不向真实 QQ 试发。只有用户另行授权维护窗口后，才考虑生产部署。

## 安装前审阅

1. 插件是 `plugins/<plugin-name>/` 下独立仓库；[Manifest](<../_manifest.json>)、[入口](<../plugin.py>)、许可证及 [README](<../README.md>)齐备。依赖以 Manifest 为准，当前下界 Host 1.3.0 / SDK 2.8.2，不强制兼容，不复制文件进主程序。
2. Runner 管理运行配置；[示例](<../config.example.toml>)仅供审阅，不覆盖当前实例。字段变化由新配置版本表达，不擅自加迁移 Hook。具体字段见[配置契约](<CONFIG_CONTRACT.md>)。
3. 准备已审查的独立推理 Python、固定模型/index/HuBERT、wrapper 与对应 upstream、专用本地 Demucs repo。没有权重/许可证的自动下载或默认缓存回退；模型许可 tag 不证明声音素材权利。
4. 确认 cgroup v2、systemd 用户管理器、全服务 OOM 策略及共享锁可用；默认媒体 max=2048 MiB、high=1536 MiB、swap=0、CPU=150%、Tasks=64。Worker 必须核实实际限制，缺失/不符拒绝加载模型。
5. 每个有限共享祖先启动前须有完整媒体预算加默认 512 MiB reserve，锁内再检查；不能读 MemTotal 猜容量，也不能把 root 与子 cgroup 用量相加。其它服务增长仍可能导致全容器不足，不能因此调整或停止它们。
6. 永久目录与私有工作区必须持久且有足够磁盘。实际 NapCat 进程须能读取最终文件；路径可读性及 QQ file/voice 端到端未验收，不通过试发探测未知历史发送。

## 行为变化

- `/翻唱 歌名 - 艺人` 默认 full/伴奏/file；自然聊天走正常 deferred `cover_song`，默认 excerpt/干声/voice。各维度独立覆盖，曲目 title/artist 保持结构化。
- **请求翻唱就授权完成后最多一次投递，不再要求 `--auto-reply`。** 旧 flag 仅兼容，旧无许可作业不升级。
- Tool自动冻结准确过滤后的首项（含Live），首项阻塞拒绝而非跳到后项；Command保留多版本显式选择。Tool 是按 SDK/MaiCore/LLM 会话信任工作的入口，不声称原消息强锚点，不改核心、不计算聊天流 ID。`cover_song_status` 只读当前用户本会话。
- Tool 活动作业复用，终态更新时间后 60 秒重放复用，之后允许重唱；Command 原消息去重。
- 媒体发送与终态错误文本分别有 durable ledger，claim 后再 RPC，正向详细 ack 加消息 ID 才确认送达，其余 unknown 不重发。
- `/说` / `speak_voice` 及旧后处理没有当前受限实现，明确不支持；不生成/读取参考音频或回退旧 sidecar。音乐登录、二维码与固定音色列表保留；`/163cookie` 不应用聊天携带的秘密。

## 测试：先门槛，再逐个有限服务

测试使用已有依赖，不为测试修改 MaiBot 环境或安装包。所有测试必须经过独立服务；禁止裸 shell 全量 pytest、并发重型测试、提高预算凑通过。

### 纯目标测试

- 起前测目标用户服务 cgroup 的每个有限共享祖先；**至少 1 GiB 实测余量**，没有则记录未运行。
- 服务固定 `MemoryMax=512M MemoryHigh=384M MemorySwapMax=0 CPUQuota=100% TasksMax=64 RuntimeMaxSec=180 KillMode=control-group OOMPolicy=kill`。
- 在服务内部读取自己的cgroup并核实max/high/swap/CPU/Tasks/OOMPolicy/KillMode及有限时限；逐文件/有界用例运行，每次全新进程。TMPDIR/TEMP/TMP、pytest basetemp、缓存与pycache显式落在经stat核实的磁盘数据目录，禁用pytest缓存或指定磁盘路径；不使用/tmp或/run/user存放测试文件。
- stdout 应记录实际 charged peak、退出原因与时间；成功 unit 已被回收后再次 `systemctl show` 可能返回默认 infinity，不能把它当该次应用限额或 peak。
- 如某测试缺依赖或触及上限，说明范围与未通过原因，不追加安装或扩大额度。

泛化示例（**管理员须先完成祖先余量检查**，并将路径替换为已有隔离环境；不是一键执行指令）：

```bash
systemd-run --user --wait --pipe --unit=<唯一测试unit> \
  -p MemoryMax=512M -p MemoryHigh=384M -p MemorySwapMax=0 \
  -p CPUQuota=100% -p TasksMax=64 -p RuntimeMaxSec=180 \
  -p KillMode=control-group -p OOMPolicy=kill \
  --working-directory=/path/to/plugin \
  /path/to/isolated-python /path/to/private-test-wrapper.py
```

wrapper 先核实实际 cgroup，再用 `pytest -p no:cacheprovider <一个目标文件或小组>`，最后读取 still-live cgroup 的 peak。若指定 `MAIBOT_TEST_HOST_ROOT`，只读实际 Host checkout，不导入会初始化生产数据库的业务模块；测试 IPC/发送必须是假体。

### 合成 OOM

仅在独立 128 MiB 服务运行小分配器，验证 systemd `oom-kill`、整组子进程退出、外部 coordinator 存活、槽在停止证据后释放，以及无 publish/send/auto-retry。不得用真实模型制造 OOM，也不得把任意 SIGKILL 判为 OOM。此项尚待执行。

### 可选真实媒体

另需用户许可；预算 max=2048 MiB/high=1536 MiB/swap=0/CPU=150%/Tasks=64，有限 runtime，起前每个有限祖先至少 **2.5 GiB 余量**。服务内部取得共享 inference.lock，严格串行；最多一个冻结分离窗口 A/B，不下载权重，不在失败后扩大预算，不更改原模型/音频或向 QQ 发送。已有 Aris 旧峰值略超默认上限，不能保证该试验能完成；无需为验收强行运行。

安装也必须是独立有限服务，服务内持有同一共享锁；本次没有安装授权。具体实验规则以用户更具体的批准预算为准，而不是历史 4 GiB 命令。

## 数据与恢复

- 迁移前私下记录旧 commit、配置版本、健康、永久成品 SHA/inode/权限，备份登录态及账本；不提交它们。
- 旧成品保持原位置；新库根首次绑定后不能靠热更搬迁。早期单-worker 作业不自动导入或补许可，运行/未知 unit 必须先对账归属，不删日志释放槽。
- 私有 `jobs.sqlite3`、`jobs/<job-id>/`、source `.part`、未封存阶段输出及 unit 日志是恢复证据，不作自动清理。
- 永久 MP3、metadata、songs 硬链接和浏览索引分别保存；重新建索引不能修改 MP3。来源探测也必须通过同一阶段 coordinator/UnitRunner，不能在 SDK Runner 裸启动媒体子进程。
- 正常卸载关闭本进程调度与平台观察；已有有限 stage 可能独立完成，重载沿同 job/unit 对账。未知/活跃子进程未证明停止前不能放槽或起替代 unit。
- 音频与文本通知的 dispatching 在恢复时记 unknown，不重试；历史许可不升级。失败不回滚已有已校验字节。
- 回滚先停新接纳、等待/对账自有 unit，保留新版账本与全部未知记录；只在另行授权的维护窗口恢复旧代码/配置。禁止 `git clean`、删库、清空产物或杀无关进程来“修复”。

## 当前验收范围

当前新预算模块 80 项定向测试通过（systemd 报告 peak 34.7M）；随后协调器组因不足 1 GiB 余量拒绝启动，未运行。来源探测、入口清理与新文本通知还在整合；当前全量、真实 Runner、合成 OOM、媒体及 QQ 端到端均未通过最终验收。历史测试/音频实验不能替代本次结果。

角色身份、训练录音权利与用户报告的音质/哼唱损失需独立人工复核，不能由有限值或无削波替代。
