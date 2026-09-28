# maibot-sing

MaiBot 插件：通过音乐来源搜索与 RVC 本地处理生成歌曲翻唱。此仓库是 [xiaocutedog/maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main) 的 QR-0W fork；上游原有的说话/MiMo TTS 等能力及致谢信息见下文。当前 dev 分支的本地后端、持久化及安全约束以 [部署与数据说明](docs/DEPLOYMENT.md) 和 [配置契约](docs/CONFIG_CONTRACT.md) 为准；本说明不表示插件已在任何运行实例启用。

## 功能概览

- 请求已知歌曲并进行人声分离、RVC 转换，可选混回伴奏。
- 本地后端复用已有 Natsume Iroha RVC v1 模型/index，不重新训练或声称存在英语版 Iroha CV。
- 完成的翻唱持久保存为 MP3 与 provenance 元数据；临时下载、分离 stems 等 scratch 与成品分开管理。
- 任务具有 queued / processing / completed / failed / cancelled / interrupted 状态；请求和成品缓存遵从本地配置契约。
- 旧 sidecar 仅为兼容路径，不会成为本地后端失败后的隐式无界 fallback；以契约规定为准。

> 运行环境、隔离要求、输出目录和部署前检查见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。操作前先审阅 [config.example.toml](config.example.toml) 与当前 [CONFIG_CONTRACT.md](docs/CONFIG_CONTRACT.md)。不要把包含凭证的真实 `config.toml` 提交到版本库。

## 模型来源与限制

使用的角色模型候选是《碧蓝档案》枣伊吕波（Natsume Iroha），不是《魔法纪录》环彩羽（Tamaki Iroha）。现有公开材料倾向于日语语料，但作者没有公开训练集清单，因此训练语言与内容仍属未完全核实；“国际服”不等同英语配音，也不代表英语CV或英语训练数据存在。模型卡上的许可 tag 不等于游戏录音、声优表演或所有相关声音素材均获授权，更不构成商业使用许可。使用者须自行确认适用权利及许可。

详尽来源、哈希、许可边界及尚未核实事项见 audio-lab 内的模型研究报告（本插件仓库不包含模型文件）。性能因歌曲时长、分离、CPU、内存限制及运行环境而异；不承诺固定处理时间或“几分钟内完成”。

## 依赖与安全

插件依赖以 `requirements.txt` / manifest 为准；本说明不要求安装或改变 MaiBot 核心环境。不得把密钥、登录态、模型、歌曲音频或运行配置提交到仓库。推理和资源限制要求见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 上游说明与鸣谢

本项目基于 [xiaocutedog/maibot_sing-main](https://github.com/xiaocutedog/maibot_sing-main)，上游 README 中的原始功能介绍与致谢如下。此 fork 的后端和运行约束可能与上游说明不同；请勿据上游旧部署步骤覆盖本仓库文档。

- [ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot) — MaiBot 的 Xiaomi MiMo v2.5 音色克隆语音回复插件。
- [maibot-music](https://github.com/pan-ice/maibot-music) — MaiBot 音乐插件，支持搜索点歌、解析音乐链接、发送语音音频。
- [Retrieval-based-Voice-Conversion-WebUI (RVC)](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — 语音音色转换框架。
- [Ultimate Vocal Remover (UVR5)](https://github.com/leebufan/Ultimate-Vocal-Remover) — 人声/伴奏分离工具。

## 已完成的验证

片段和 202 秒整曲均已真实完成，峰值内存分别约 1.83 / 1.94 GiB；实际 SDK 的缓存与发送失败保留也已验证。见 [完整验证记录](docs/VALIDATION.md)。线上配置仍禁用，未向 QQ 发送消息。

```bash
python -m pytest tests -q
```

测试需要插件依赖、maibot-plugin-sdk、pytest 与 pytest-asyncio，不加载 RVC 模型或访问真实聊天。
