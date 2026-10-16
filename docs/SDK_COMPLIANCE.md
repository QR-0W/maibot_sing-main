# SDK 合规与验证证据（0.6.1）

有界上下文更新：excerpt源30–900秒、唯一中心上下文≤90秒、选择12–18秒；recipe-v4/probe-v2/selection-v2。旧v2/v3完成成品与v1 selection保持只读，不以新代码续跑旧计划。用户授权的生产重载与真实媒体/QQ验收分开记录，历史测试数字不代表当前字节。

本文件区分当前源码契约、历史验证和本次待验收范围。不把旧版本通过数字、fake IPC 或 serializer 契约当生产送达；仅按用户明确授权应用/重载，不试发、不改MaiBot核心。

## 接口基线

以 [MaiBot Vibe Coding 指南](https://docs.mai-mai.org/plugin/vibe-coding)为开发基线；实际最低 Host 1.3.0 / SDK 2.8.2，Manifest v2。依赖与版本以 [Manifest](<../_manifest.json>)为准，不更改核心依赖来补契约。

| 项目 | 当前实现 / 限制 |
|---|---|
| 入口 | [plugin.py](<../plugin.py>)包含 config_model、on_load/on_unload/on_config_update 和 create_plugin；不新增 Action、迁移 Hook 或副作用入口。 |
| 配置 | PluginConfigBase + Field，Runner 生成/补齐配置；[模板](<../config.example.toml>)只供审阅。源码契约变化升为 0.6.0，不修改现网配置。 |
| Command | 严格 slash `/翻唱`，原始直接文字/QQ账号路由验证；自然文本不拦截，不使用模型匹配组替换原文。 |
| Tool | 正常 deferred cover_song，明确 title/artist、默认 excerpt/dry/voice 与独立覆写；不默认 core_tool=True，不声称 ALWAYS 保证发现。 |
| 接纳与状态 | 两入口共享 [JobService](<../services/job_service.py>)；Tool 返回结构化 job_id/state/code/content/choices，cover_song_status 只读本会话当前用户。 |
| Tool 信任 | SDK/MaiCore/LLM 一致 stream/chat/QQ用户与非虚拟检查，按用户接受的上下文边界；不是原始消息强锚点，不计算真实流 ID，不改核心。 |
| 幂等 | Command 原消息 ID；Tool 活动复用、终态更新时间后60秒重放复用、此后允许新请求。不是永久去重或原消息 exactly-once。 |
| 明确许可 | 请求翻唱即许可最多一次最终投递，无需 --auto-reply；历史无许可/旧proof不升级。Tool准确过滤后的首项自动冻结且不跳过阻塞项；Command保留本人版本选择。 |
| 预算 | 默认2048MiB、高水位1536MiB、swap0、CPU150%、Tasks64；冻结快照，实际cgroup核实，目标祖先启动前及共享锁内检查，整unit OOM终止。 |
| 来源探测 | 也通过原StageCoordinator/UnitRunner，冻结全recipe前有独立source_probe receipt/plan；Host只读<=64KiB report，无裸ffprobe/ffmpeg回退。 |
| 生命周期 | 独占调度所有权，未确认unit/children停止不放槽；卸载收敛后台task/客户端，已有有限unit按同identity恢复，不删除证据。 |
| 媒体投递 | 独立outbox claim-before-RPC，一次send.custom(return_details=True)；voiceurl或file，正向sent加有效message_id才sent，否则unknown不重发。 |
| 失败文本 | 独立(job_id,kind)ledger，新明确许可任务才有资格；send.text(return_details=True)，RPC15s/总20s，restartdispatching→unknown，不污染media字段。 |
| 退休路径 | 旧LocalBackend/sidecar/Pipeline/TTS/voicecache实例从入口删除。/说和speak_voice明确不支持，无合成、参考读取或fallback；音乐登录/二维码保留。 |
| 音频身份 | sing-render-v4，真实source/engine/helper/selector/mode/完整argv，完整源帧及冻结上下文；已完成legacy v2/v3只读，不升级运行中plan，旧claim先核对。 |

上述是源码核对，不是当前接口已经通过自动化或真实 Runner 的证明。

## 当前已经执行的范围

- 新预算模块定向 **80 passed**；独立服务请求 max512MiB/high384MiB/swap0/CPU100/Tasks64/runtime180s，systemd 报告 peak 34.7M、swap0。
- 随后 primitive 协调器组因真实root余量不足1GiB，门槛拒绝，**没有运行**。
- 新入口清理、Tool绑定/status、failure ledger及source_probe测试仍待隔离执行；diff whitespace检查通过，不等于编译/导入/测试通过。
- 当前没有最终全量、合成OOM、真实Runner、真实媒体或QQ端到端验收。不将旧191/340/432/866/1011等历史或目标集合相加为当前结果。

## 历史基线（不能套到当前工作树）

历史0.5.0修订81203d7曾记录359项及真实Runner/PluginLoader配置、加载、热更、卸载等隔离契约；IPC、平台与搜索为假体，不是生产QQ。更早迁移/整曲/恢复、旧自然Command审查亦属当时字节。当前structured Tool与source_probe改变了边界，旧审查不构成新GO。旧安全发现及原始证据见[历史安全审查](<SECURITY_REVIEW_CONVERSATIONAL.md>)顶部范围警告。

## 后续验收

依[项目规则](<../AGENTS.md>)与[部署验收](<DEPLOYMENT.md>)先检查实际有限祖先余量，再用有限服务逐文件/小组新进程测试；禁止裸全suite、并发重型测试、安装依赖或涨预算。

1. 先接口/账本/源码预算测试，再阶段/锁内复核/整组终止与恢复。
2. 专用128MiB小分配器合成OOM，验证外部coordinator存活、槽位fence、无publish/send/retry，不能用真实模型制造OOM。
3. 真实Runner只在私有隔离环境验证，不连接生产数据库/网关；真实工具发现需正常聊天流程证据，不能用直接handler调用冒充LLM发现。
4. 真实媒体另需许可与至少2.5GiB余量；不保证Aris旧峰值能进入默认2048MiB。
5. QQ端到端仅用户另行明确授权；file/voice路径可读性、详细回执、实际呈现与音质需单独验收。

私有研究、模型hash日志、真实路径、audio、credentials和运行数据库不提交。RMS/F0/finite/no-clipping不能代替旋律/哼唱/质量/角色及权利判断。
