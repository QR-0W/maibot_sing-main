# 对话式翻唱功能分支：离线验收记录

范围：独立 `feat/conversational-singing` 工作树；仅合成音频、假引擎、假 SDK、私有临时 SQLite 与只读 Host 源码 AST。没有修改 MaiBot 核心/生产插件或配置，没有真实 QQ 发送、音乐网络下载、生产模型推理、安装依赖、合并或部署。

## 实际执行

- 全量测试（2026-09-30 工作树）：`MAIBOT_TEST_HOST_ROOT=/path/to/MaiBot PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<已有 soundfile 0.12.1 的隔离环境> /path/to/MaiBot/.venv/bin/python -m pytest -q -p no:cacheprovider tests`，**866 passed in 25.86s，退出码 0**。原第一次全量运行 865 passed / 1 failed，失败原因是旧集成测试假定装饰器 `pattern=` 是 AST 字符串字面量；将测试改为检查实际注册正则及独立原文解析后，单独测试文件 **6 passed**，全量重跑得到上述结果。没有忽略或跳过原失败。
- 渲染目标测试曾独立报告 **191 passed**：默认 full 维持 20 秒分块、<5 秒尾折叠，对照原计划 7 个时长 × 干声/伴奏 14 组；explicit excerpt 固定七阶段且只一次转换。使用合成音频、假 Demucs，不加载实际推理模型。
- 入口/Host AST/账本/出箱目标测试曾独立报告 **340 passed**；真实 Host 原消息初始化、序列化和 Command executor 的 AST 契约覆盖群聊/私聊及 slash/受限自然语法，检查引用、媒体派生文字不授予新 `--auto-reply` 许可。目标集合互有重叠，不与全量数字相加。
- 只读运行 `release-baseline/capture_baseline.py verify`，结果 `{"verified": true, "covers": 4, "protected_files": 14}`；生产安装工作树 `git status --short` 无修改。功能分支对本地已有 `soundfile` 通过既有环境路径复用，没有安装软件。
- `git -c core.whitespace=cr-at-eol diff --check` 通过；既有 `plugin.py` 为 CRLF，故使用与原格式一致的 whitespace 检查。

## 边界和未通过的验收

这只是离线/合成测试，不是实际歌曲短片段音质验证，也不是运行中 MaiBot 插件加载或真实 QQ 文件上传验收。`file` 回执若不包含明确 `sent=True` 和有效 `message_id` 则只能记录 `unknown`、不可自动重试；真实私聊/群聊文件呈现及 NapCat 路径可读性待用户另行明确授权后验收。速度 B 并不保证固定耗时，也不证明副歌识别、自然乐句或沙哑改善。

模型选型的私有报告位于插件仓库外；目前只暂留现有 Natsume Iroha 技术基线。sid0 权重结构有限数值核验不证明角色身份、素材权利或人工听感。报告、私有音频、权重、Cookie、真实配置和运行数据库均不纳入提交。独立安全审查另见 [SECURITY_REVIEW_CONVERSATIONAL.md](<SECURITY_REVIEW_CONVERSATIONAL.md>)，其结论仅可用于本地提交/进一步离线测试，不能授权生产部署。
