# 会话式翻唱：独立安全审查

审查者：release-auditor，未实现本轮业务补丁。审查对象为 `conversational-singing-20260930/plugin` 的当前 feature 工作树；开始时 HEAD 为 `a2771c87eb96fa23ca9291da6706bd7bb3889624`，存在实现者的未提交改动。**本文不是对这些改动已经提交或部署的证明。**

## 最终结论：本地提交 / 离线阶段 GO

**仅本地提交及离线测试阶段可放行：CS-01 派生授权与 CS-02 可选字段兼容两个 P1 均已独立复核关闭；在本轮检查范围内，没有剩余已确认的 P0/P1。**

这不是部署 GO，不授权真实 QQ 消息、模型运行或生产配置变更；不构成音质、角色身份、录音/模型权益声明。此前 NO-GO 的原因和复现保留于下文，已被本节最终结论取代。Lead 的全量测试和本地提交单独负责，本报告不假称自己执行了全量套件。

### 最终独立复核证据

- [直接文本 gate:30–54](<../services/request_options.py#L30-L54>)现以 `message.get('reply_to') is not None` 判断引用；缺省或 None 接受，存在且非 None 拒绝，raw reply/派生组件拒绝不变。
- 独立重跑原真实 serializer AST 复现（不是只读实现者结果），普通直接 NL 文本的 Host 字典确实省略 reply_to；现在接受并持久化 `napcat-direct-text-v2` 与显式许可。原 reply-only payload 仍在 active lookup 前拒绝，零发送：

```text
REAL_HOST_SERIALIZER reply_to_present=False DIRECT_ACCEPTED=True PROOF= napcat-direct-text-v2
ORIGINAL_REPLY_ONLY_REPRO_NOW_REJECTED_ZERO_LOOKUP_SEND=PASS
```

- [真实 Host 合约测试:28–119](<../tests/test_host_raw_contract.py#L28-L119>)执行实际 MaiMessage initializer、四个 serializer 方法及 Command executor 的 AST：private/group × slash/NL 共 4 个 direct 正例，12 个 reply_to/reply-component/derived-text 负例；[optional key:122–139](<../tests/test_host_raw_contract.py#L122-L139>)覆盖 7 种省略/None/非 None 情形。本审查者在 Host import/网络/非测试 SQLite fence 下亲跑通过。
- [旧 v1 投递测试:462–488](<../tests/test_delivery_outbox.py#L462-L488>)在 file/voice 两种模式验证旧 pending 被拒、零 artifact/send 调用、token 仍 None；已 sent 的历史行完全不变。本轮亲跑通过，不追认旧授权。
- [v3 验证:149–158](<../runtime/recipe_identity.py#L149-L158>)要求 separate/excerpt/mix 各自恰有一个 `--render-mode`，且与 recipe mode 一致；[反例测试:129–145](<../tests/test_recipe_identity.py#L129-L145>)覆盖 missing/duplicate/wrong/truncated，已亲跑通过。

最终目标套件为 request_options、request_binding、host_raw_contract、delivery_outbox、job_ledger、recipe_identity、artifact_store、render_plan 八个测试文件，显式设置 `MAIBOT_TEST_HOST_ROOT="$HOST_ROOT"`，结果：

```text
432 passed in 2.31s
NL_FINAL_TEST_EXIT=0
```

两次最终核对确认下列已测试文件字节未变；当时 HEAD 仍为上述父提交，故以下哈希用于精确标识未提交实现，而不是把父提交误称为新功能提交：

```text
plugin.py                    0ef57da0ea4fe99e53b4c2e8953e8db5a8d6bfa50f8a8718ff59b4dafacc5f30
services/request_options.py  6b93624eb863527272086997d72fa779ba420315c328beb12240cd317af38e51
services/job_store.py        b45e81514de3d34610f2d1b551974f508dee43c187d5f5568f037e3ebe23663f
services/job_service.py      c64be93b67ac4ffd14cd7984904a1d12debf910359464c10f3ab01cd92882f29
services/delivery_outbox.py  87461b55c9d5edecee250d291d84b3aa9b3f8495c23a12fe15e723077adafd5b
runtime/recipe_identity.py   3657b80e8ca3b202a7b43e1410d61b988db86018a25807c94273fb02f3ffe9e0
runtime/render_plan.py       fe2122553b3ee452dcf79416e8a78e470120c7c38966859dd18c2bac23273d0d
```

Lead 另报全量 866 passed/25.86s，本审查不将其列作自己的亲跑证据。所有本审查 background job 已收集；未进行真实 QQ、听辨、模型或部署验收。

## 已确认的 P1 与复现

### CS-01：派生 reply 文本被当作直接授权（已关闭）

初次工作树的[入口 identity](<../plugin.py#L980-L997>)只比较 kwargs.text 与 processed_plain_text；[翻唱处理](<../plugin.py#L1134-L1161>)直接解析后者，没有 raw component 验证。

`MaiBot/src/chat/message_receive/message.py:422–428` 对已有 target_message_content 直接返回内容；`MaiBot/src/chat/message_receive/message.py:154–172`把组件处理结果拼接为 processed_plain_text。因此“Host 原始消息对象”不等于“用户新写的直接文本”。

独立复现仅 AST 提取这个 Host reply 方法，不 import Host；合成可信 QQ metadata 的消息，其 raw_message **只有 reply，无 text**，引用内容是 `唱一下《Song - Artist》 --auto-reply`。私有 SQLite/fake catalogue 下结果：

```text
REPLY_ONLY_RAW_COMPONENTS=1 DIRECT_TEXT_COMPONENTS=0 accepted= True auto_reply= True delivery_state= pending catalogue_searches= 1 sends= 1
DERIVED_REPLY_AUTHORIZATION_P1_REPRO_CONFIRMED
```

direct-text 补丁后，复用原 reply-only payload，将 `_require_active_cover` 替换为一旦调用就抛异常的 trap：

```text
ORIGINAL_REPLY_ONLY_REPRO_NOW_REJECTED_ZERO_LOOKUP_SEND=PASS
```

[validate_direct_host_text](<../services/request_options.py#L30-L55>)现在仅接受有界 text 组件列表，按 Host 的单空格 join 必须等于 processed_plain_text 与 kwargs.text；拒绝 reply/forward/voice/image/dict/at、通知、已引用以及不一致/过大/畸形输入。调用在任何 active-service lookup、搜索、ledger mutation、发送之前。

当前 proof 升级为 `napcat-direct-text-v2`；旧 v1 许可不应被追认。这是固定版本的受信任 Host/NapCat 入口标记，不是加密签名。

### CS-02：可选 reply_to 被当作必填（已关闭；保留历史复现）

实际`MaiBot/src/plugin_runtime/host/message_utils.py:444–448`仅在 reply_to 非 None 时输出该字段。普通未引用的直接 TextComponent 消息，序列化字典里**没有 reply_to key**。

修复前的 [request_options:37–38](<../services/request_options.py#L37-L38>)的 `'reply_to' not in message` 将这种正常消息拒绝；测试 helper 人工塞入 None 掩盖兼容问题。

独立测试 AST 提取真实 Host `_component_to_dict`（Text 分支）、`_message_sequence_to_dict`、`_message_info_to_dict`、`_session_message_to_dict`，没有用手工 serializer stub：

```text
REAL_HOST_SERIALIZER reply_to_present=False DIRECT_IDENTITY_ACCEPTED= False
DIRECT_SERIALIZED_REQUEST_ACCEPTED= False sends= 0
```

当时建议只拒绝存在且非 None 的 reply_to，即把缺省视为未引用；不能通过给生产 message 人工补字段来掩盖错误契约。最终实现已按此修复，并保留 raw reply component 拒绝与非空 reply_to 拒绝；真实 serializer AST 的直接文字正例及 quoted/derived 负例全部通过，因此 CS-02 已关闭。修复前该缺陷阻断全部正常 Command 功能，被定为 P1，而不是可延期文案问题。

## 其余实现核对

### 同名 Command / 严格语法 / 权限 / 幂等

- [request_options:5–8](<../services/request_options.py#L5-L8>)为整个 slash/NL alternation 使用 `\A…\Z`，不是仅锚定其中一支。
- [同一个翻唱 Command](<../plugin.py#L1127-L1133>)承载两种语法，未新增 Hook 或替代名称绕过会话禁用。Host regex matcher 使用 search 而非 fullmatch；完整锚定必要。
- `MaiBot/src/chat/message_receive/bot.py:264–294`先判同名禁用与既有 operator permission，再设置 is_command。NL 不需要自行伪造 is_command。
- handler 从验证过的文本独立解析，不让 matched_groups、Tool 参数或顶层伪造 selectors 替换 query/mode/consent。[绑定测试](<../tests/test_request_binding.py#L25-L69>)验证同消息重放得到同一 job，catalogue search 只一次；[Tool 测试](<../tests/test_request_binding.py#L154-L167>)验证即使携带完整伪 Host payload 也不产生副作用。
- 显式 `--auto-reply` 仍必须最后且唯一；自然语法本身**不会默认授予投递许可**。slash 默认 full/伴奏/file；NL 默认 excerpt/清唱/voice，三个维度独立可覆盖，最终选项进入持久 request，不由未来配置补算。
- 全部会发送的 Command 继续共用 identity gate，来源无效时连拒绝文本也不向该 stream 发送。

### File / voice 类型与 durable gate

- [request_modes:26–39](<../services/job_store.py#L26-L39>)限定 delivery_mode=file|voice、render_mode=full|excerpt，instrumental 必须严格 bool；拒绝列表/字典等畸形类型。
- [claim_delivery](<../services/job_store.py#L561-L578>)在事务内验证 root、模式、proof 和显式 consent，才生成 delivery token；delivery scan 仍按相同 proof 版本过滤旧许可。
- [CustomVoiceSender:58–100](<../services/delivery_outbox.py#L58-L100>)从持久 request 选择唯一 transport：voiceurl 使用 `{url}`，file 使用 `{url,name}`。没有按第一次失败改另一种消息类型。
- [ack 分类:42–55](<../services/delivery_outbox.py#L42-L55>)仍要求明确 sent=True 且有效 message_id，False/缺 ID/异常等保持 unknown。模式扩展没有被当作自动重试许可。
- 没有真实 QQ 文件/语音发送验收；可读源码/模拟回执不能证明平台实际呈现、文件名、上传大小上限或接收成功。

### Render / recipe v3 / legacy v2

- 当前[build_plan:24–74](<../runtime/render_plan.py#L24-L74>)明确 default render_mode=full。full 使用完整帧长的 20 秒分块并折叠小于 5 秒尾部；excerpt 有独立 selection stage、一个转换块、同步 backing 与 selection 收据。没有把此前开发暂态误报为当前 full 被截短。
- [media_stage:61–82](<../runtime/media_stage.py#L61-L82>) full 写全部 chunks，并使用 full mixer；excerpt 走 selection/excerpt mixer。没有运行实际 Demucs/RVC，质量不在本轮结论内。
- [recipe schema:11–18](<../runtime/recipe_identity.py#L11-L18>)新生成 v3，新增 excerpt-selection code hash，legacy v2 hash 列表独立保留；mode 及实际 argv/steps 参与 identity，delivery mode 不改变音频 recipe 是合理分层。
- [ArtifactStore:71–79](<../services/artifact_store.py#L71-L79>)接受可读 schema；[publish:121–128](<../services/artifact_store.py#L121-L128>)拒绝新发布 legacy recipe，既有成品不覆盖。[JobService:727–754](<../services/job_service.py#L727-L754>)拒绝把 legacy v2 当新可执行 frozen plan。这是 oldv2 成品只读，不是承诺旧运行中计划可无缝迁移。
- [artifact_manifest](<../runtime/artifact_manifest.py#L10-L76>)验证 recipe identity、source/model/mix、selection canonical bytes 与 excerpt receipt、最终 decode receipt；没有把 duration 或摘要当听辨/录音身份依据。
- 最终 v3 render_mode 与 argv 一致性约束及反例测试已完成，并在本报告顶部记录的 432 项目标测试中独立通过。

## 本轮亲跑证据

安全栅栏：在 import pytest/插件前拒绝 `src.*` 业务 import、socket.connect、测试临时目录外 SQLite；AST 只提取已读的函数，不执行 Host 顶层代码。使用指定 MaiBot venv，不以 Host 根目录作为测试工作目录；合成 bytes 与私有 SQLite 结束清理。

目标文件：request_options、request_binding、delivery_outbox、job_ledger、recipe_identity、artifact_store、render_plan。

```text
369 passed, 6 skipped in 1.65s
NL_SCOPE_TEST_EXIT=0
```

6 个 skip 为未显式设置 Host checkout 的 AST gate cases；随后设置 `MAIBOT_TEST_HOST_ROOT="$HOST_ROOT"`，仅重跑这些 case：

```text
6 passed, 44 deselected in 0.39s
NL_HOST_AST_EXIT=0
```

它们确认 slash/NL 的 allowed、disabled、operator_denied 分支；但这些既有测试使用 serializer stub，因此**不能反驳 CS-02**。CS-02 使用真实 serializer AST 的独立复现另列于前。所有 bash 结果均已收集；复现退出 0 是断言/观察完成，不等于被测缺陷已修复。

## P2 / 非代码边界

1. 当前仅接受直接 text 列表，不接受 @+text、语音、引用等。这是安全收紧后的兼容/交互边界；不应在文档中笼统承诺“任意自然聊天皆可触发”。
2. Host 先前 Hook 可改写文本及 metadata；直接 text 检查仍信任已安装 Host/NapCat/Hook 插件，不抵御持有网关/Hook 权限的恶意插件。proof 不是签名。
3. 旧 v1 pending 将拒绝自动投递但不自动变成成功/撤回/重授权；需要运维对账。already sent/unknown 不因此再发。
4. 真实 QQ 文件/语音、片段连贯性、清唱与伴奏听感、音色身份/许可、内存峰值与部署回滚仍需独立受限验收。未进行这些验收，就不能把静态/合成测试写成 live 证明。

## 最终放行边界

CS-01、CS-02 与最终 v3 一致性检查均已通过独立复核。本报告仅支持本地提交和后续离线阶段；Lead 的全量测试及本地提交单独负责。部署、真实 QQ、模型与听辨/权益验收未获本报告授权。
