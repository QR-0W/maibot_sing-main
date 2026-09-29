# 插件与 audio-lab 的边界（离线候选修复）

**结论：已发布的 0.3.1 不是可直接跨机器运行的 Linux 翻唱包。** 其运行时把维护者用户名、模型路径、隔离 Python、RVC 实验脚本和推理锁写在插件源码；这是部署缺陷，不能以“仍在 dev”解释。原 upstream 的音乐搜索/sidecar 路线本身也不是这个受限 Linux 推理后端。

本分支已在离线测试目录将这些**机器专有路径**从运行时代码移至 `local` 管理员配置：`model_path`、`index_path`、`worker_python`、`musicdl_python`、`rvc_script`、`inference_lock`。六者留空时必须明确失败；示例配置没有私人绝对路径。模型和 FAISS 索引存放位置由管理员决定；插件的长期成品、配置和缓存留在 SDK `ctx.paths.data_dir` 的 `covers` 目录（或管理员显式设置的绝对 `output_dir`）。本地实验的输入、分轨、对照结果、服务运行日志可留在开发者的 `audio-lab/work`，**不能作为通用插件安装协议**。可读成品硬链接在 `covers/songs/`，不依赖 `audio-lab`。

受限引擎现仍为外部前置条件：RVC 推理脚本及其 HuBERT/模型支持资源、Demucs+RVC Python 依赖、musicdl 独立 Python、ffmpeg/ffprobe 和 systemd user manager。代码包没有捆绑声优训练语音、模型权重、任何账号凭据或这些引擎；这不是一键安装，也不应把他人模型和原曲重分发进 GitHub ZIP。管理员须核对每个模型及音源授权，运行对应 Python 离线自检；模型来源、训练材料和声音使用许可当前未完全查明，不能声称符合商业或公开分发授权。

当前插件本地工作路径的运行条件是 Linux cgroup v2、`systemd-run --user` 可用、内存上限 4 GiB/high 3 GiB/无 swap、150% CPU、TasksMax=64、单实例串行推理，以及该脚本对当前 RVC 权重与 HuBERT 兼容。不允许不存在依赖时自动回退到无 cgroup 的转换。还应在候选版本上线前增加脚本/环境版本校验与安全打包核验，避免管理员给出错误的 `.py` 文件路径却能通过 `is_file()` 静态检查。

**QQ/NapCat 是另一个服务生命周期**：排查证据指向之前运行在临时 dsh-subprocess scope 的两个前台 screen 树一起收到 SIGTERM；不能拿插件用户服务代替 NapCat 的长期服务，更不能默认重登。需由管理员明确批准独立服务配置和维护窗口，才能在线复测语音发送。

验证范围：离线代码单元和模拟进程测试，已经生成现有成品的人类可读目录；**未部署**本候选代码，插件仍设为 `enabled=false`，QQ 未自动重登，真实用户端到端语音发送仍待授权测试。
