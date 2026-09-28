# QR-0W maibot-sing dev integration plan

## User objective
Use the successful Natsume Iroha RVC voice through MaiBot, using a personal fork and dev branch. Preserve completed covers on disk. Remove now-obsolete experiment media after integration, not production models/environments/sources. Route simple tasks to GPT-6-Luna, difficult implementation to GPT-6-Sol.

## Safety and boundaries
- Fork: https://github.com/QR-0W/maibot_sing-main ; upstream xiaocutedog/maibot_sing-main. Work only on dev. Lead alone commits/pushes/enables plugin/deletes external files.
- Working tree: /home/qr0w/MaiBot/plugins/qr0w.maibot-sing . The untracked config.toml currently disables plugin and legacy sidecar auto_start. Never enable until Lead review.
- Read /home/qr0w/MaiBot/AGENTS.md. No MaiBot core edits. Existing parent uv.lock changes and .kilo are unrelated; preserve them.
- Container 8GiB; local synthesis/download work must be isolated using systemd user units MemoryMax=4G, MemoryHigh=3G, MemorySwapMax=0, CPUQuota<=150%, bounded runtime/queue, and in-service flock /home/qr0w/audio-lab/inference.lock. No unsafe fallback if isolation unavailable.
- Async MaiBot loop must not block. Never kill processes based only on a port/PID supplied by an unverified foreign service. No global cache dropping.
- Do not commit audio, weights, credentials, real runtime config, personal cookies, or external checkout copies.
- Do not post test audio to a QQ group without a specified destination. Do not restart existing services without Lead coordination.

## Write scope partition
### Sol (GPT-6-Sol): hard implementation
Own plugin.py, services/**, music/** if needed, rvc_client.py/sidecar/** only if needed, runtime/**/scripts/** for local workers, tests/**, _manifest.json, requirements.txt, pyproject.toml if added, docs/CONFIG_CONTRACT.md.
Implement local bounded backend, durable output/job metadata, bounded queue/dedup, tool/command integration and targeted tests. Preserve legacy API where practical but never silently use an unbounded legacy sidecar. Publish CONFIG_CONTRACT early for Luna.

### Luna (GPT-6-Luna): simple documentation and cleanup planning
Own README.md, docs/** except CONFIG_CONTRACT.md, config.example.toml, CHANGELOG.md.
Prepare exact cleanup candidate list (NO deletion), document model provenance and output policy. Align config.example to final CONFIG_CONTRACT/schema after Sol finishes. Do not edit Python or actual config.toml.

### Lead
Own DEVELOPMENT_PLAN.md, .gitignore, untracked config.toml, integration/review fixes after child completion, Git/GitHub operations, live verification, exact cleanup execution and final report. No overlapping edits while a child is active.

## Required behavior
- MaiBot Tool/command can request a known song; preserve exact track title/artist/version when choosing results, reject previews/unavailable tracks, no third-party unlocking services.
- Reuse musicdl official-source downloader, Demucs separation and successful RVC parameters; don't retrain or invent English Iroha CV.
- Successful cover is permanently persisted BEFORE returning/sending: final MP3 + sanitized provenance, hashes, exact parameters and job status. Temp stems/download scratch have separate lifetime. No automatic deletion of final covers with existing five-day temp cache cleanup.
- Persistent defaults should use plugin-specific data_dir; explicit absolute output_dir override allowed. Sanitize names or use stable content IDs; atomic completion, no traversal or arbitrary shell text.
- Result caching/dedup keyed by source identity + model/index hashes + parameters; queue bounded, heavy processing serial. User should know queued/processing/failed status; failures not silently swallowed.
- Distinguish local backend timeout from voice-send timeout. Persist output even if chat send fails. No duplicate voice retry on unknown delivery.
- Preserve model parameters user liked: NatsumeIroha RVC v1, 40kHz, pitch0, harvest, index_rate0.5, filter_radius3, rms_mix_rate0.25, protect0.33, seed20260928. Avoid automatic pitch adjustment.
- Tests must cover persistence surviving send failure, cache key, queue/cancellation, unsafe input paths, no deletion of final covers, external command construction/resource limit, and disabled legacy auto-start.

## Reusable local resources (read-only unless Lead approves)
- RVC CLI: /home/qr0w/audio-lab/tools/rvc/rvc.sh (starts its own limited systemd service and flock; DO NOT nest another locked caller around it).
- Raw worker CLI: /home/qr0w/audio-lab/tools/rvc/rvc_infer.py (can run INSIDE an already bounded+locked worker instead). README in same folder. Existing /home/qr0w/svc-bench/.venv39 has torch CPU/fairseq/demucs/soundfile; no dependency modifications by children.
- Model: /home/qr0w/audio-lab/models/natsume-iroha/extracted/NatsumeIroha/NatsumeIroha.pth
- Index: same directory/added_IVF186_Flat_nprobe_1_v1.index
- Model SHA256: 01f2ee572103e0770b896c8a73acce9ed477fcf74d804bb441420c1d8e049319
- musicdl Python: /home/qr0w/musicdl-run/.venv/bin/python ; cloned source /home/qr0w/musicdl . Existing fetch_song.py is a one-song test hardcoded to 200-240s and MUST NOT be copied as generic production behavior.
- Full real source: /home/qr0w/music_downloads/Neutral Milk Hotel - In the Aeroplane Over the Sea.mp3
- Prior reference inputs: /home/qr0w/audio-lab/work/20260928_english_iroha/inputs/ . Do not delete until Lead completes regression/first permanent render.
- Model provenance: /home/qr0w/audio-lab/notes/natsume-iroha.md . International/global does NOT mean English CV; publisher only states openrail tag, not CV commercial authorization.
- Existing resource scripts: /home/qr0w/audio-lab/tools/publish_comparison.py ; /home/qr0w/svc-bench/real_song_preview.py (experiments, useful reference not production assumptions).

## Cleanup policy
User explicitly permits deleting old testing contents. Candidate allowlist: old sing_samples current/archive media, audio-lab/work experiment runs, svc-bench test_song/raw/results experimental audio, unused test-generated TTS audio. Preserve selected Natsume model/index/provenance, original source downloads, reusable RVC engine/environment, and any NEW permanent cover library. Lead inventories and checks no production references first, then deletes exact files/directories with a removal record outside the new cover library. No blanket rm -rf audio-lab or svc-bench.
