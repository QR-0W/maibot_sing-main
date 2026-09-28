# 0.3.0-dev 验证记录

验证日期：2026-09-28。本记录是本机实测，不是所有歌曲都可下载或在相同时限内完成的保证。

## 真实音频链路

| 测试 | 输入 | Worker 耗时 | cgroup 内存峰值 | 结果 |
|---|---|---:|---:|---|
| 受控片段 | 已保留原曲的 60 秒片段 | 227.624 秒 | 1,965,965,312 bytes（1.83 GiB） | 永久 MP3 + metadata |
| 整曲下载翻唱 | Neutral Milk Hotel — In the Aeroplane Over the Sea | 774.252 秒 | 2,078,011,392 bytes（1.94 GiB） | 永久 MP3 + metadata |

整曲使用 musicdl 的 NeteaseMusicClient 官方来源，曲目 ID `17549213`，核对准确标题、艺人、专辑及目录时长 202.346 秒；没有第三方解锁或更换歌曲。原曲来自现有歌曲，不是 TTS 仿唱或合成伴奏。

- 整曲解码时长：202.346667 秒；MP3 容器时长：202.396735 秒，编码填充差异正常。
- 格式：MP3 192 kbps，44.1 kHz，双声道，4,858,191 bytes。
- 整曲 SHA-256：`f8b649bfa1b5b76d7fca0d6579d9ab7839f4dc8fa784be0bb8bd22bdde25ccbb`。
- 整曲成品 key：`94fd3f75ccca9b90f324321c04a746e0cf8f3c01934d5e957d153e3839a12224`。
- 全量流式解码：NaN/Inf 0，绝对值 >=1 的采样 0；解码采样峰值 0.933823。片段也通过同类检查。
- 两次推理实测 `MemoryMax=4294967296`、`MemoryHigh=3221225472`、`MemorySwapMax=0`、CPU 配额 150%，OOM 和 swap 均为 0。
- 成品目录严格只含 `cover.mp3` 和 `metadata.json`；`.scratch`、`.publishing` 在结束后为空。永久 MP3 不进入临时五天缓存。

模型为 Natsume Iroha（《碧蓝档案》枣伊吕波），不是旧 Tamaki Iroha。技术校验不能替代试听，也不证明角色音色完美、英语发音质量或声音素材授权。

## SDK 与回归

实际环境：MaiBot 1.3.0 / SDK 2.8.2 / Python 3.13；Demucs/RVC 位于已有隔离 Python 3.9 环境，未修改 MaiBot 核心或环境依赖。

- 对实际 SDK 导入、组件注册、配置、加载、配置重建、卸载、加载失败后的资源释放进行了测试。
- 覆盖队列满拒绝、并发请求合并、取消、库独占、遗留 worker 恢复、成品与 scratch 分离、发送失败保留、未知投递禁止重试及英文歌曲命令解析。
- SDK 的真实 `cover_song` → 本地永久缓存 → 文件 URI 路径已完成隔离验收，缓存命中约 0.18 秒，没有重复渲染。仅最终发送 transport 使用明确返回失败的桩，成品在发送失败和缓存清理后仍在。
- 测试通过 pytest 执行，详见 [后端测试](../tests/test_local_backend.py) 和 [SDK 测试](../tests/test_plugin_delivery.py)。
- 单元生命周期测试剔除 Harness 代理变量后运行；本会话的括号 IPv6 `NO_PROXY` 值会被 httpx 判为非法 URL。systemd 隔离验收的实际环境成功加载。

## 清理与保留

按用户许可，逐文件记录 SHA-256 后删除 50 个旧实验音频，共 224,701,886 bytes（约 214.3 MiB），包括旧 TTS/Tamaki 比较、旧 VCTK/Natsume 试听副本、分离中间音轨和中断测试 WAV。未递归删除实验根目录。

保留完整原曲、选定模型/index、HuBERT、RVC/musicdl 工具及虚拟环境、研究与历史 JSON/日志、冻结回归输入、当前永久片段及整曲。清理依据见 [清理计划](CLEANUP_PLAN.md)。本机逐文件清单位于 `audio-lab/work/plugin-integration/cleanup-record.json`，不随代码提交。

## 尚未执行的线上步骤

真实插件配置已对齐 0.3.0，但仍为 `enabled=false`，sidecar 自动启动关闭。没有启动 MaiBot、向 QQ 私聊/群发送测试消息或修改 NapCat。

- 管理员需先指定允许测试的聊天流，确认 NapCat 可以访问永久成品目录及文件 URI。
- MaiBot 环境缺少 manifest 声明的二维码可选功能所需 `segno`；当前无登录的本地翻唱/SDK 检查未用到它。正式启用前应按批准流程解决插件依赖，不在本次测试里擅自安装到 MaiBot 环境。
- MiMo 原生 TTS 可按自身配置使用；TTS 后 RVC 换声尚无受限路径，因此默认关闭并明确拒绝不安全调用。
- 线上启用和实际 QQ 音频投递并未验收，不能把上述 transport 桩测试称为 QQ 发送成功。
