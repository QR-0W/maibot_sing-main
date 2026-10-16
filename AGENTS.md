# MaiBot singing plugin project rules

## Scope and SDK

Follow the [official Vibe Coding guide](https://docs.mai-mai.org/plugin/vibe-coding). Work only in this independent plugin repository. Do not modify MaiBot core, its root ignore file, dependencies, running configuration or production plugin without explicit user permission.

- Keep `plugin.py`, `_manifest.json` (v2), `config_model`, full `on_load/on_unload/on_config_update`, and `create_plugin()`.
- Use `maibot-plugin-sdk`; new capabilities use `@Tool/@Command`, not new `@Action`. Hooks/events must have a genuine auxiliary role, not become an alternate task-admission path.
- Normal Tools enter the deferred discovery pool. Do not default `core_tool=True` or present `ActivationType.ALWAYS` as a discovery guarantee.
- Declare formatted parameters and side effects. Return readable Simplified Chinese `content` with structured admission/status fields. Never compute a real chat stream ID yourself.
- Configuration is `PluginConfigBase + Field`; Runner manages runtime configuration. The template is documentation, not an overwrite source. Version contract changes; do not add migration Hooks without permission.
- Plugin dependencies are authoritative in the Manifest, not MaiBot's requirements. Keep `/config.toml` ignored in this repository. No secrets, tokens, cookies, database files, audio, weights, private reports, machine paths or logs in commits.

## One implementation per responsibility

Command parses only `/翻唱`; natural conversation uses structured `cover_song`. Keep title/artist separate to the catalogue boundary. Both entry adapters call JobService; do not duplicate search/selection in the entry file or create an untracked scheduler.

A cover request authorizes its eventual one delivery attempt; `--auto-reply` is only redundant compatibility. Independent defaults: Command full/backing/file; Tool excerpt/dry/voice. Preserve independent overrides, explicit user version selection, durable ownership, cancellation, receipts, immutable artifacts and at-most-once outboxes.

Tool context follows the accepted SDK/MaiCore/LLM session trust boundary, not an invented original-message anchor. Active jobs reuse; terminal Tool replays reuse for 60 seconds, then new requests are allowed. Historical tasks never acquire new delivery/notification consent.

Remove obsolete reachable paths instead of piling defensive branches on them. Unsupported TTS/legacy conversion must explicitly refuse before synthesis, reference-audio reads or media execution. Preserve music login and useful unrelated behavior; avoid whole-repository formatting and unnecessary abstractions.

## Resource safety (approved task-specific policy)

The user's approved limits below supersede the parent experiment directory's historical 4 GiB command. They apply to every reachable media subprocess, including source probing, decode, separation, RVC, mixing, encoding and validation.

- One dedicated finite `systemd-run --user` service per media stage, default MemoryMax=2048 MiB, derived MemoryHigh=1536 MiB, MemorySwapMax=0, CPUQuota=150%, TasksMax=64, finite RuntimeMaxSec, KillMode=control-group and OOMPolicy=kill.
- Freeze one validated budget before startup awaits, pass it to the unit, verify actual applied cgroup limits and whole-group OOM policy. Missing fields must not fall back to 4 GiB.
- Check actual finite ancestors of the destination service plus the container, not MemTotal or the caller's unrelated service leaf. Require full budget plus default 512 MiB reserve; recheck INSIDE the service after taking the shared inference lock and before model loading.
- Acquire the administrator-configured shared inference lock inside every model/install service; never take a different lock per job. Heavy work is strictly serial. Prove the prior unit and children stopped before freeing the slot.
- Insufficient/unknown budget means zero media launch and a public failure. Confirm OOM only from actual systemd/cgroup evidence; SIGKILL alone is not OOM. Kill the complete owned service, retain verified earlier output, publish/send no partial audio, never silently retry or increase the budget.
- Do not stop unrelated processes, clear caches, alter their caps, change container limits or MaiBot dependencies. Other workloads can still exhaust the container; do not promise universal OOM prevention.
- Stream downloads/intermediates to disk with size/time bounds. No weights or large intermediates under `/tmp`. Entry/SDK Runner must remain lightweight, not import models or buffer complete audio.

## Validation discipline

Do not run bare full-suite pytest or concurrent heavy tests. Do not install dependencies to make tests pass.

1. For pure target tests, require at least 1 GiB measured headroom at every finite shared ancestor. Use a dedicated service with MemoryMax=512 MiB, MemoryHigh=384 MiB, swap=0, CPU=100%, Tasks=64 and runtime=180s. Verify limits from inside it.
2. Targeted files first, then at most one fresh process per file or bounded group. Record real results, unit wall time and charged memory peak before the successful unit disappears. Refusal or interruption is not a pass.
3. Synthetic OOM uses a small allocator in a separate 128 MiB unit, not a model. Verify whole-unit termination, surviving coordinator, slot fencing, no partial publish/send and no retry.
4. Optional real inference needs separate permission, at least 2.5 GiB headroom, the 2048/1536 MiB media budget and internal shared lock. At most one frozen comparison window. No new weights, budget increase after failure, training or QQ sending without permission.
5. Host/SDK fixtures and fake IPC do not prove actual QQ or real Runner/media behavior. Do not import Host business modules that initialize production storage. Read-only Host checkout inspection is not permission to modify core.
6. Preserve prior evidence and user audio. Freeze comparison source/stems/pitch/seed/mixing; report engine differences and rights gaps. RMS/F0/finite values/no clipping cannot certify lyrics, humming, quality or character identity. Do not confuse Blue Archive Natsume Iroha with Tamaki Iroha; global edition does not imply English voice acting.

## Review and delivery

Read existing files before editing, preserve line endings, review the final diff and coordinated write scopes. Check every command exit result. Update contracts and README to the actual implementation, and distinguish tested, historical and pending evidence. Do not deploy or send QQ regression media automatically. Deliver concise succeeded/not-succeeded/how-to-verify results with private listening links when relevant.
