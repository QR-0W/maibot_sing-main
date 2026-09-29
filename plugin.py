"""MaiBot 翻唱 + 语音插件。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ActivationType, ToolParameterInfo, ToolParamType

from .music.search import MusicSearchClient, MusicSearchError, SongInfo
from .rvc_client import RVCClient, RVCSidecarError
from .services.mimo_tts import MiMoTTSService
from .services.pipeline import Pipeline
from .services.local_backend import LocalBackend
from .services.artifact_store import ArtifactStore
from .services.asset_inventory import AssetInventory, AssetPaths, RuntimeVersionProbe
from .services.catalogue_service import CatalogueService, CatalogueError
from .services.delivery_outbox import CustomVoiceSender, DeliveryOutbox
from .services.job_service import JobService, RenderRuntime, RENDER_PARAMETER_POLICY
from .services.job_store import JobConflict, JobNotFound, JobStore
from .services.ownership import OwnershipBusy, exclusive
from .services.source_offer import normalized
from .services.stage_coordinator import StageCoordinator
from .services.unit_runner import UnitRunner
from .services.voice_sender import SingleVoiceSender


# 期望的 sidecar 代码版本；sidecar/server.py 的 SIDECAR_VERSION 递增时同步修改。
# 端口上已有服务版本不匹配（残留旧代码进程）时，插件会终止它并重新拉起
EXPECTED_SIDECAR_VERSION = "4"


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="默认禁用；管理员审查后明确启用")
    config_version: str = Field(default="0.5.0", description="配置版本")


class RVCConfig(PluginConfigBase):
    """RVC sidecar 配置（可迁移核心：改 rvc_root 即可迁移）。"""

    __ui_label__ = "RVC 声音转换"
    __ui_order__ = 1

    rvc_root: str = Field(default="D:/RVC20240604Nvidia", description="RVC 安装根目录")
    python_path: str = Field(default="", description="RVC Python 解释器路径，留空自动用 {rvc_root}/runtime/python.exe")
    port: int = Field(default=7898, description="sidecar 监听端口（避开 7897 WebUI）")
    auto_start: bool = Field(default=False, description="旧 sidecar 不自动启动；Linux 本地模式始终忽略此项")
    default_model: str = Field(default="", description="默认音色模型（assets/weights 下的 .pth 文件名，含扩展名）")
    f0_method: str = Field(default="rmvpe", description="音高提取算法: pm/harvest/crepe/rmvpe")
    f0_up_key: int = Field(default=0, description="变调（半音数，升八度 12，降八度 -12）")
    model_keys: dict[str, int] = Field(
        default_factory=dict,
        description="按模型自动变调映射：模型文件名(含 .pth) → 变调半音数。命中时覆盖 f0_up_key",
    )
    index_rate: float = Field(default=0.5, description="检索特征占比 (0~1)，过高会产生金属感/沙哑")
    filter_radius: int = Field(default=3, description="harvest 中值滤波半径（>=3 可削弱哑音）")
    resample_sr: int = Field(default=0, description="后处理重采样至最终采样率，0 为不重采样")
    rms_mix_rate: float = Field(default=0.25, description="音量包络融合比例 (0~1)")
    protect: float = Field(default=0.4, description="清辅音保护力度 (0~0.5)，过低会口齿不清")
    uvr_model: str = Field(default="HP2_all_vocals", description="UVR5 人声分离模型名（assets/uvr5_weights 下，不含 .pth）")
    uvr_agg: int = Field(default=10, description="人声提取激进程度 (0~20)")
    uvr_weights_dir: str = Field(
        default="",
        description="外部 UVR5 权重目录（如 Ultimate Vocal Remover 的 VR_Models 绝对路径），留空用 RVC 自带 uvr5_weights",
    )
    auto_key: bool = Field(
        default=False,
        description="自动变调：按示例音频与歌曲人声的音高差自动计算变调半音数（model_keys 命中时不生效）",
    )
    auto_key_offset: int = Field(
        default=0,
        description="自动变调微调（半音）：在自动计算结果上额外升/降，正=升、负=降",
    )
    auto_key_max: int = Field(
        default=5,
        ge=1,
        description="自动变调幅度上限（半音绝对值）：变调过大音色会发哑，高音哑就调低此值（如 3）",
    )
    sample_audio: str = Field(
        default="",
        description="音色示例音频路径（该音色的唱歌片段，作为自动变调的基准音高）",
    )
    voice_cache_dir: str = Field(
        default="",
        description="语音条本地缓存目录（NapCat 可见的绝对路径），留空用插件运行时目录",
    )
    voice_cache_retention_days: int = Field(
        default=5,
        ge=0,
        description="翻唱语音缓存文件保留天数（按文件修改时间计算，0=永久保留）",
    )


class MiMoConfig(PluginConfigBase):
    """MiMo TTS 配置（说话功能的基础 TTS）。"""

    __ui_label__ = "MiMo TTS"
    __ui_order__ = 2

    api_key: str = Field(default="", description="MiMo API Key")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1", description="MiMo API 地址")
    voice_mode: str = Field(default="preset", description="语音模式: 'preset'(预置音色) 或 'clone'(音色复刻)")
    preset_voice: str = Field(default="mimo_default", description="预置音色 ID（仅 preset 模式生效）")
    reference_audio: str = Field(default="", description="音色复刻参考音频文件路径（仅 clone 模式生效）")
    rvc_after_tts: bool = Field(
        default=False,
        description="说话功能：TTS 输出后再经 RVC 换音色；关闭则直接发送 TTS 原声",
    )


class MusicConfig(PluginConfigBase):
    """音乐搜索配置。"""

    __ui_label__ = "音乐搜索"
    __ui_order__ = 3

    default_platform: str = Field(default="163", description="默认音乐平台: 163(网易云) 或 qq(QQ音乐)")
    search_limit: int = Field(default=5, description="搜索结果数量")
    netease_account: str = Field(default="", description="网易云账号（手机号或邮箱），填写后自动密码登录")
    netease_password: str = Field(default="", description="网易云密码")
    netease_countrycode: str = Field(default="86", description="网易云手机号区号（手机号登录时生效）")
    netease_music_u: str = Field(default="", description="网易云 MUSIC_U 登录凭证（可选，账号密码登录的回退）")
    netease_csrf: str = Field(default="", description="网易云 __csrf 令牌（可选，与 MUSIC_U 配对）")
    qq_uin: str = Field(default="", description="QQ音乐 uin（可选，扫码登录的回退）")
    qq_key: str = Field(default="", description="QQ音乐 qqmusic_key（可选，扫码登录的回退）")


class ComponentConfig(PluginConfigBase):
    """组件开关。"""

    __ui_label__ = "组件"
    __ui_order__ = 4

    command_enabled: bool = Field(default=True, description="启用命令（/翻唱、/说、/音色列表、/qq音乐登录、/163logintest、/qqlogintest）")
    tool_enabled: bool = Field(default=True, description="启用工具（LLM 自主触发）")


class LocalConfig(PluginConfigBase):
    backend: str = Field(default="local", description="只支持受限 Linux 本地后端；legacy 禁止不受限推理")
    output_dir: str = Field(default="", description="永久输出绝对路径；留空使用插件 data_dir/covers")
    model_path: str = Field(default="", description="管理员提供的固定 RVC 模型绝对路径，未配置时禁止渲染")
    index_path: str = Field(default="", description="管理员提供的索引绝对路径，未配置时禁止渲染")
    hubert_path: str = Field(default="", description="管理员提供的 HuBERT 模型绝对路径，不使用开发者本地默认值")
    demucs_repo_path: str = Field(default="", description="固定本地 Demucs 模型 repo 绝对目录：htdemucs.yaml 与对应 .th")
    rvc_upstream_path: str = Field(default="", description="固定 RVC 上游代码文件/目录绝对路径")
    worker_python: str = Field(default="", description="安装 Demucs/RVC 的隔离 Python 绝对路径（建议 3.9）")
    musicdl_python: str = Field(default="", description="安装 musicdl 的隔离 Python 绝对路径")
    rvc_script: str = Field(default="", description="受限 RVC 脚本绝对路径；不执行未隔离 WebUI")
    inference_lock: str = Field(default="", description="与本机其他推理共享的绝对锁文件路径，父目录须存在")
    max_queue: int = Field(default=2, ge=0, le=8)
    timeout_s: int = Field(default=900, ge=60, le=900)
    max_duration_s: int = Field(default=300, ge=30, le=300)
    max_download_bytes: int = Field(default=67108864, ge=1048576, le=67108864)
    local_source_allowlist: list[str] = Field(default_factory=list, description="仅用于管理员可控回归")


class SingPluginConfig(PluginConfigBase):
    """翻唱插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    rvc: RVCConfig = Field(default_factory=RVCConfig)
    mimo: MiMoConfig = Field(default_factory=MiMoConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    components: ComponentConfig = Field(default_factory=ComponentConfig)


@dataclass(frozen=True)
class ActiveCoverService:
    jobs: JobService
    provider: str
    search_limit: int
    default_model: str
    model_path: str
    model_aliases: tuple[str,...]


@dataclass(frozen=True)
class DurableCoverSettings:
    root: Path
    output: Path
    runtime_dir: Path
    paths: tuple[tuple[str,Path],...]
    max_queue: int
    max_duration_s: int
    max_download_bytes: int
    provider: str
    search_limit: int
    default_model: str

    def path(self,name: str) -> Path:
        return dict(self.paths)[name]


class SingPlugin(MaiBotPlugin):
    """翻唱 + 语音插件。"""

    config_model = SingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._music: MusicSearchClient | None = None
        self._rvc: RVCClient | None = None
        self._mimo: MiMoTTSService | None = None
        self._pipeline: Pipeline | None = None
        self._local: LocalBackend | None = None
        self._sidecar_proc: asyncio.subprocess.Process | None = None
        # 待选歌曲状态: stream_id -> (结果列表, 平台, 时间戳)
        self._pending: dict[str, tuple[list[SongInfo], str, float]] = {}
        self._pending_lock = asyncio.Lock()
        self._voice_sender: SingleVoiceSender | None = None
        self._active_cover: ActiveCoverService | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._startup_tasks: set[asyncio.Task[Any]] = set()
        # QQ 扫码登录后台任务
        self._qq_login_task: asyncio.Task[None] | None = None
        # 网易云扫码登录后台任务
        self._netease_login_task: asyncio.Task[None] | None = None
        self._jobs: JobService | None = None
        self._outbox: DeliveryOutbox | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._scheduler_owner: Any = None
        # 翻唱语音缓存定期清理任务
        self._cache_cleanup_task: asyncio.Task[None] | None = None

    # ===== 生命周期 =====

    def _track_startup_task(self, task: asyncio.Task[Any]) -> None:
        self._startup_tasks.add(task)
        def finished(done: asyncio.Task[Any]) -> None:
            self._startup_tasks.discard(done)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException:
                    pass
        task.add_done_callback(finished)

    async def _acquire_scheduler_owner(self, lock_path: Path) -> int:
        owner=exclusive(lock_path)
        enter=asyncio.create_task(
            asyncio.to_thread(owner.__enter__),name='sing-scheduler-owner-acquire')
        try:
            fd=await asyncio.shield(enter)
        except asyncio.CancelledError:
            async def close_late_owner() -> None:
                try:
                    await enter
                except BaseException:
                    return
                owner.__exit__(None,None,None)
            cleanup=asyncio.create_task(
                close_late_owner(),name='sing-scheduler-owner-late-close')
            self._track_startup_task(cleanup)
            raise
        self._scheduler_owner=owner
        return fd

    async def _startup_owned_call(self, ownership_fd: int, function, /, *args):
        try:
            lease_fd=os.dup(ownership_fd)
        except OSError as exc:
            raise RuntimeError('持久化调度所有权已丢失') from exc
        def owned_call():
            try:
                return function(*args)
            finally:
                os.close(lease_fd)
        task=asyncio.create_task(
            asyncio.to_thread(owned_call),name='sing-owned-startup-call')
        self._track_startup_task(task)
        return await asyncio.shield(task)

    async def on_load(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._load_resources()
            except BaseException:
                await self._close_resources()
                raise

    async def _load_resources(self) -> None:
        self.ctx.logger.info("翻唱插件加载中...")
        # Copy every scalar needed below before the first await. A Host config
        # callback can replace self.config later, but this lifecycle owns one snapshot.
        port=self.config.rvc.port
        legacy=(self.config.local.backend,self.config.rvc.auto_start)
        mimo_key=self.config.mimo.api_key
        mimo_base=self.config.mimo.api_base_url
        durable=self._capture_durable_settings()
        self._cache_cleanup_task=asyncio.create_task(
            self._voice_cache_cleanup_loop(),name='sing-voice-cache-cleanup')
        self._voice_sender=SingleVoiceSender(self.ctx.send.custom)
        self._rvc=RVCClient(f"http://127.0.0.1:{port}",logger=self.ctx.logger)
        if legacy==('legacy',True):
            self.ctx.logger.warning("旧 sidecar 不具备 Linux 资源隔离，拒绝自动拉起")
        self._music=self._build_music_client()
        await self._restore_music_logins()
        self._mimo=MiMoTTSService(api_key=mimo_key,api_base_url=mimo_base,
                                  logger=self.ctx.logger)
        self._pipeline=Pipeline(self._music,self._rvc,self._mimo,self.ctx.logger)
        await self._start_durable_services(durable)
        self.ctx.logger.info("翻唱插件加载完成")

    async def _stop_durable_services(self) -> None:
        self._active_cover=None
        task,self._delivery_task=self._delivery_task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        # An in-flight platform send is fenced unknown on next startup. Never
        # launch a replacement send for pending/dispatching/unknown automatically.
        outbox,self._outbox=self._outbox,None
        if outbox is not None:
            await outbox.shutdown(timeout_s=1.0)
        jobs,self._jobs=self._jobs,None
        if jobs is not None:
            await jobs.close()
        owner,self._scheduler_owner=self._scheduler_owner,None
        if owner is not None:
            owner.__exit__(None,None,None)
        pending=tuple(self._startup_tasks)
        if pending:
            await asyncio.wait(pending,timeout=1.0)

    async def _close_resources(self) -> None:
        await self._stop_durable_services()
        if self._local is not None:
            await self._local.close()
            self._local=None
        current_tasks=[task for task in (self._qq_login_task,self._netease_login_task,
                                         self._cache_cleanup_task) if task is not None]
        for task in current_tasks:
            task.cancel()
        if current_tasks:
            await asyncio.gather(*current_tasks,return_exceptions=True)
        self._qq_login_task=self._netease_login_task=self._cache_cleanup_task=None
        sender,self._voice_sender=self._voice_sender,None
        if sender is not None:
            await sender.shutdown(timeout_s=1.0)
        if self._music is not None:
            await self._music.close()
            self._music=None
        if self._mimo is not None:
            await self._mimo.close()
            self._mimo=None
        if self._rvc is not None:
            await self._rvc.close()
            self._rvc=None
        await self._stop_sidecar()
        self._pending.clear()
        self._pipeline=None
        self.ctx.logger.info("翻唱插件已卸载")

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            await self._close_resources()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data,version
        if scope!=CONFIG_RELOAD_SCOPE_SELF:
            return
        async with self._lifecycle_lock:
            self.ctx.logger.info("翻唱插件配置已更新，重建客户端")
            await self._close_resources()
            try:
                await self._load_resources()
            except BaseException:
                await self._close_resources()
                raise

    # ===== 持久化渲染与投递 =====

    def _render_paths(self) -> dict[str, Path]:
        cfg = self.config.local
        if cfg.backend != 'local':
            raise RuntimeError('旧 sidecar 不支持持久化受限翻唱')
        names = ('model_path', 'index_path', 'hubert_path', 'demucs_repo_path',
                 'rvc_upstream_path', 'worker_python', 'rvc_script', 'inference_lock')
        paths = {}
        for name in names:
            raw = getattr(cfg, name).strip()
            if not raw or not Path(raw).is_absolute():
                raise ValueError(f'local.{name} 必须显式配置为绝对路径')
            paths[name] = Path(raw)
        # The restricted wrapper imports its own fixed upstream sibling; a
        # separate administrator path must not claim unrelated hashed code.
        if paths['rvc_upstream_path'] != paths['rvc_script'].parent / 'upstream':
            raise ValueError('local.rvc_upstream_path 必须等于 rvc_script 所在目录下的 upstream')
        return paths

    def _capture_durable_settings(self) -> DurableCoverSettings:
        paths=self._render_paths()
        root=Path(self.ctx.paths.data_dir).resolve()
        runtime_dir=Path(__file__).resolve().parent/'runtime'
        configured=self.config.local.output_dir.strip()
        raw_output=Path(configured) if configured else root/'covers'
        if (not raw_output.is_absolute()
                or any(path.is_symlink() for path in (raw_output,*raw_output.parents))):
            raise ValueError('持久化产物目录必须是非符号链接绝对路径')
        output=raw_output.resolve()
        return DurableCoverSettings(
            root=root,output=output,runtime_dir=runtime_dir,
            paths=tuple(sorted(paths.items())),
            max_queue=int(self.config.local.max_queue),
            max_duration_s=int(self.config.local.max_duration_s),
            max_download_bytes=int(self.config.local.max_download_bytes),
            provider=self._resolve_platform(self.config.music.default_platform),
            search_limit=min(10,max(1,int(self.config.music.search_limit))),
            default_model=self.config.rvc.default_model.strip())

    async def _start_durable_services(self, settings: DurableCoverSettings) -> None:
        path=settings.path
        root,output,runtime_dir=settings.root,settings.output,settings.runtime_dir
        # The scheduler lock parent is the only pre-service directory creation.
        # All durable binding, artifact validation, and hashing belong to jobs.prepare.
        await asyncio.to_thread(root.mkdir,parents=True,exist_ok=True,mode=0o700)
        ownership_fd=await self._acquire_scheduler_owner(
            root/'.durable-scheduler.lock')
        store=await self._startup_owned_call(
            ownership_fd,JobStore,root/'jobs.sqlite3',max(1,settings.max_queue+1))
        runner=UnitRunner(runtime_dir/'stage_executor.py',path('worker_python'))
        asset_paths=AssetPaths(
            model=path('model_path'),index=path('index_path'),hubert=path('hubert_path'),
            demucs_repo=path('demucs_repo_path'),rvc_script=path('rvc_script'),
            rvc_upstream=path('rvc_upstream_path'),media_stage=runtime_dir/'media_stage.py',
            worker=runtime_dir/'worker.py',render_plan=runtime_dir/'render_plan.py',
            stage_executor=runtime_dir/'stage_executor.py')
        inventory=AssetInventory(asset_paths,RuntimeVersionProbe(path('worker_python')))
        runtime=RenderRuntime(
            work_root=root/'jobs',worker_python=path('worker_python'),
            worker_script=runtime_dir/'media_stage.py',rvc_script=path('rvc_script'),
            model=path('model_path'),index=path('index_path'),hubert=path('hubert_path'),
            demucs_repo=path('demucs_repo_path'),inference_lock=path('inference_lock'),
            max_download_bytes=settings.max_download_bytes,
            max_duration_s=settings.max_duration_s)
        execution_paths={
            'model':path('model_path'),'index':path('index_path'),
            'hubert':path('hubert_path'),'demucs_repo':path('demucs_repo_path'),
            'rvc_script':path('rvc_script'),'rvc_upstream':path('rvc_upstream_path'),
            'media_stage':runtime_dir/'media_stage.py','worker':runtime_dir/'worker.py',
            'render_plan':runtime_dir/'render_plan.py',
            'stage_executor':runtime_dir/'stage_executor.py',
            'worker_python':path('worker_python'),'worker_script':runtime_dir/'media_stage.py',
            'inference_lock':path('inference_lock')}
        runtime_context={
            'execution_paths':execution_paths,'artifact_root':output,
            'limits':{'max_duration_s':settings.max_duration_s,
                      'max_download_bytes':settings.max_download_bytes},
            'parameter_policy':RENDER_PARAMETER_POLICY}
        if self._music is None:
            raise RuntimeError('音乐目录客户端尚未初始化')
        jobs=JobService(
            store,CatalogueService(self._music,max_duration_s=settings.max_duration_s),
            StageCoordinator(store,runner),output,inventory,runtime,
            runtime_context=runtime_context,ownership_fd=ownership_fd)
        self._jobs=jobs
        await jobs.prepare()
        if not isinstance(jobs.artifacts,ArtifactStore):
            raise RuntimeError('持久化产物存储未完成绑定')
        outbox=DeliveryOutbox(
            store,CustomVoiceSender(self.ctx.send.custom),jobs.artifacts.verify)
        self._outbox=outbox
        # Root is already validated and bound. Recovery must precede any worker
        # claim or delivery discovery.
        await outbox.recover()
        await jobs.start()
        aliases=tuple(dict.fromkeys(value for value in (
            settings.default_model,str(path('model_path')),path('model_path').name) if value))
        self._active_cover=ActiveCoverService(
            jobs=jobs,provider=settings.provider,search_limit=settings.search_limit,
            default_model=settings.default_model,model_path=str(path('model_path')),
            model_aliases=aliases)
        self._delivery_task=asyncio.create_task(
            self._delivery_loop(),name='sing-delivery-outbox')

    async def _delivery_loop(self) -> None:
        while True:
            try:
                jobs,outbox=self._jobs,self._outbox
                if jobs is not None and outbox is not None:
                    for job_id,stream_id in await jobs.delivery_candidates(limit=8):
                        await outbox.dispatch(job_id,stream_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception('持久化投递巡检失败；不得无凭据重发')
            await asyncio.sleep(2)

    # ===== 本地受限后端（仅保留兼容查询；不再用于翻唱入口） =====

    def _make_local_backend(self) -> LocalBackend:
        cfg = self.config.local
        if cfg.backend != 'local':
            raise RuntimeError('旧 sidecar 未隔离，禁止在本机启用')
        if cfg.output_dir.strip():
            output = Path(cfg.output_dir)
            if not output.is_absolute():
                raise ValueError('local.output_dir 必须是绝对路径')
        else:
            output = Path(self.ctx.paths.data_dir).resolve() / 'covers'
        path_fields = ('model_path', 'index_path', 'worker_python', 'musicdl_python',
                       'rvc_script', 'inference_lock')
        paths = {field: Path(getattr(cfg, field)) for field in path_fields if getattr(cfg, field).strip()}
        if len(paths) != len(path_fields) or any(not path.is_absolute() for path in paths.values()):
            raise ValueError('local 的模型、索引、隔离 Python、RVC 脚本、推理锁须全部配置为绝对路径')
        return LocalBackend(output, model=paths['model_path'], index=paths['index_path'],
                            worker_python=paths['worker_python'], musicdl_python=paths['musicdl_python'],
                            rvc_script=paths['rvc_script'], inference_lock=paths['inference_lock'],
                            max_queue=cfg.max_queue, timeout_s=cfg.timeout_s,
                            max_duration_s=cfg.max_duration_s,
                            max_download_bytes=cfg.max_download_bytes,
                            allowlist=tuple(Path(path) for path in cfg.local_source_allowlist))

    # ===== sidecar 管理 =====

    def _resolve_python_path(self) -> str:
        """解析 RVC Python 解释器路径。"""
        rvc_root = self.config.rvc.rvc_root
        configured = self.config.rvc.python_path.strip()
        if configured:
            return configured
        return str(Path(rvc_root) / "runtime" / "python.exe")

    def _sidecar_script_path(self) -> str:
        return str(Path(__file__).parent / "sidecar" / "server.py")

    async def _start_sidecar(self) -> None:
        """历史 Windows sidecar；Linux 本地部署禁止启动无界推理。"""
        if os.name == "posix":
            raise RuntimeError("Linux 禁止无资源隔离的旧 sidecar")
        rvc = self._rvc
        if rvc is None:
            return
        port = self.config.rvc.port

        # 先探测端口是否已有服务
        try:
            health = await rvc.health()
        except Exception:
            health = None

        if health and health.get("status") == "ready":
            version = str(health.get("version", "") or "")
            if version == EXPECTED_SIDECAR_VERSION:
                self.ctx.logger.info("复用已运行的 sidecar: %s", port)
                return
            # 版本不匹配：端口上是旧代码的残留进程，终止后重新拉起
            self.ctx.logger.warning(
                "端口 %s 上的 sidecar 代码版本过旧（%s != %s），终止后重新拉起",
                port, version or "未知", EXPECTED_SIDECAR_VERSION,
            )
            self.ctx.logger.error('端口已有不同版本服务；拒绝关闭非本插件拥有的进程')
            return

        rvc_root = self.config.rvc.rvc_root
        python_exe = self._resolve_python_path()
        script = self._sidecar_script_path()

        if not Path(rvc_root).exists():
            self.ctx.logger.error("RVC 根目录不存在: %s", rvc_root)
            return
        if not Path(python_exe).exists():
            self.ctx.logger.error("RVC Python 解释器不存在: %s（请检查 rvc_root 或 python_path）", python_exe)
            return

        self.ctx.logger.info("拉起 sidecar: %s %s --rvc-root %s --port %s", python_exe, script, rvc_root, port)
        try:
            self._sidecar_proc = await asyncio.create_subprocess_exec(
                python_exe,
                script,
                "--rvc-root",
                rvc_root,
                "--port",
                str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self.ctx.logger.error("拉起 sidecar 失败: %s", exc)
            return

        # 等待 sidecar 就绪（模型加载可能较慢，但服务本身几秒内可响应 /health）
        for _ in range(60):
            if self._sidecar_proc is not None and self._sidecar_proc.returncode is not None:
                self.ctx.logger.error("sidecar 进程提前退出，退出码: %s", self._sidecar_proc.returncode)
                return
            try:
                health = await rvc.health()
                if health.get("status") == "ready":
                    self.ctx.logger.info("sidecar 已就绪: %s", port)
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        self.ctx.logger.warning("等待 sidecar 就绪超时（60s），可能仍在加载模型")

    async def _terminate_stale_sidecar(self, health: dict[str, Any]) -> None:
        """终止版本过旧的 sidecar 进程并等待端口释放。"""
        proc = self._sidecar_proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
        else:
            self.ctx.logger.warning('拒绝终止其他服务健康检查返回的 PID')
            return
        self._sidecar_proc = None
        # 等旧进程释放端口
        for _ in range(10):
            try:
                await self._rvc.health()
                await asyncio.sleep(0.5)
            except Exception:
                return

    async def _ensure_sidecar_ready(self) -> None:
        """发起转换前确保 sidecar 存活；崩溃或被结束后自动重新拉起。"""
        if self._rvc is None:
            return
        try:
            health = await self._rvc.health()
            if health.get("status") == "ready":
                return
        except Exception:
            pass
        self.ctx.logger.warning("sidecar 未就绪，自动重新拉起")
        await self._start_sidecar()

    async def _stop_sidecar(self) -> None:
        proc = self._sidecar_proc
        self._sidecar_proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

    def _build_music_client(self) -> MusicSearchClient:
        netease_cookie: dict[str, str] = {}
        if self.config.music.netease_music_u:
            netease_cookie["MUSIC_U"] = self.config.music.netease_music_u
        if self.config.music.netease_csrf:
            netease_cookie["__csrf"] = self.config.music.netease_csrf
        qq_cookie: dict[str, str] = {}
        if self.config.music.qq_uin:
            qq_cookie["uin"] = self.config.music.qq_uin
        if self.config.music.qq_key:
            qq_cookie["qqmusic_key"] = self.config.music.qq_key
        return MusicSearchClient(netease_cookie=netease_cookie, qq_cookie=qq_cookie)

    # ===== 音乐平台登录 =====

    def _login_cache_path(self) -> Path:
        return Path(self.ctx.paths.runtime_dir) / "music_login_cache.json"

    def _load_login_cache(self) -> dict[str, Any]:
        """读取平台登录态缓存文件（避免每次重启都重新登录）。"""
        try:
            with open(self._login_cache_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_login_cache(self, section: str, value: dict[str, Any]) -> None:
        try:
            data = self._load_login_cache()
            data[section] = value
            path = self._login_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.logger.warning("保存音乐登录缓存失败: %s", exc)

    async def _restore_music_logins(self) -> None:
        """插件加载/重建客户端后恢复网易云与 QQ 的登录态。"""
        if self._music is None:
            return
        cfg = self.config.music
        cache = self._load_login_cache()

        # QQ：优先用扫码登录缓存，其次退回配置里的 uin/key（已在客户端初始化）
        qq_cache = cache.get("qq") if isinstance(cache.get("qq"), dict) else {}
        if (
            not cfg.qq_uin.strip()
            and not cfg.qq_key.strip()
            and qq_cache.get("uin")
            and qq_cache.get("qqmusic_key")
        ):
            self._music.apply_qq_cookies({k: str(v) for k, v in qq_cache.items()})
            self.ctx.logger.info("已恢复 QQ 音乐缓存登录态: uin=%s", qq_cache["uin"])

        # 网易云：恢复持久化的设备 ID 与匿名 token（扫码接口需要，避免重复注册被限频）
        device = cache.get("netease_device") if isinstance(cache.get("netease_device"), dict) else {}
        if device.get("device_id"):
            self._music.set_netease_device(str(device["device_id"]), str(device.get("anon_token") or ""))

        # 网易云：配置了账号密码时自动登录（缓存有效则复用，失效自动重登）
        try:
            result = await self._ensure_netease_login()
            if result:
                self.ctx.logger.info("%s", result)
        except Exception as exc:
            self.ctx.logger.warning("网易云自动登录失败（将退回 cookie/未登录模式）: %s", exc)

    async def _ensure_netease_login(self, force: bool = False) -> str:
        """确保网易云处于登录态，返回结果说明。

        优先复用缓存登录态（含扫码登录的，先经接口校验），失效或 force 时：
        配置了账号密码则密码登录，否则提示重新扫码。
        """
        if self._music is None:
            return ""
        cfg = self.config.music
        account, password = cfg.netease_account.strip(), cfg.netease_password.strip()
        cache = self._load_login_cache().get("netease", {})
        cache_cookies = (cache.get("cookies") or {}).get("MUSIC_U")
        # 缓存登录态可复用：toml 账号匹配，或来自扫码登录（account 标记 qr）
        cache_matches = bool(cache_cookies) and (
            str(cache.get("account", "")) == account
            or str(cache.get("account", "")).startswith("qr")
            or str(cache.get("account", "")) == "cookie"
        )
        if not force and cache_matches:
            cookies = {k: str(v) for k, v in cache["cookies"].items()}
            self._music.apply_netease_cookies(cookies)
            try:
                profile = await self._music.get_netease_profile()
                self.ctx.logger.info("复用网易云缓存登录态: %s", profile["nickname"])
                return f"网易云登录正常：{profile['nickname']}"
            except Exception:
                self.ctx.logger.warning("网易云缓存登录态已失效，尝试重新登录")
        if not account or not password:
            if cache_cookies:
                return "网易云缓存登录态已失效，请发 /网易云音乐登录 重新扫码"
            return ""
        cookies = await self._music.login_netease(account, password, cfg.netease_countrycode)
        self._save_login_cache("netease", {"account": account, "cookies": cookies})
        profile = await self._music.get_netease_profile()
        self.ctx.logger.info("网易云自动登录成功: %s", account)
        return f"网易云登录成功：{profile['nickname']}"

    # ===== 工具方法 =====

    def _convert_kwargs(self, sid: str = "") -> dict[str, Any]:
        cfg = self.config.rvc
        f0_up_key, _ = self._resolve_f0_up_key(sid)
        return {
            "f0_up_key": f0_up_key,
            "f0_method": cfg.f0_method,
            "index_rate": cfg.index_rate,
            "filter_radius": cfg.filter_radius,
            "resample_sr": cfg.resample_sr,
            "rms_mix_rate": cfg.rms_mix_rate,
            "protect": cfg.protect,
        }

    def _resolve_f0_up_key(self, sid: str) -> tuple[int, bool]:
        """解析变调半音数，返回 (半音数, 是否命中 model_keys 手动映射)。

        每个 RVC 模型都有其训练时最合适的音域，翻唱时按模型自动适配变调。
        ``rvc.model_keys`` 键为模型文件名（含 .pth），值为该模型的变调半音数；
        手动映射优先于自动变调。
        """
        model = (sid or "").strip()
        if model:
            key = self.config.rvc.model_keys.get(model)
            if key is not None:
                return int(key), True
        return int(self.config.rvc.f0_up_key), False

    def _resolve_model(self, sid: str) -> str:
        """解析音色模型名，未指定时用配置默认值。"""
        model = sid.strip() if sid else self.config.rvc.default_model.strip()
        if not model and self._local is not None:
            model = self._local.model.name
        if not model:
            raise RuntimeError("未指定音色模型，请配置 rvc.default_model")
        return model

    def _resolve_platform(self, platform: str) -> str:
        p = platform.strip().lower()
        if p in ("163", "qq"):
            return p
        if p in ("网易", "netease", "网易云音乐"):
            return "163"
        if p in ("qq音乐", "qqmusic"):
            return "qq"
        default = self.config.music.default_platform.strip().lower()
        return default if default in ("163", "qq") else "163"

    def _voice_cache_dir(self) -> Path:
        """翻唱语音缓存目录：配置了 voice_cache_dir 用之，否则用运行时目录。"""
        raw = self.config.rvc.voice_cache_dir.strip()
        return Path(raw).expanduser() if raw else Path(self.ctx.paths.runtime_dir)

    def _cleanup_voice_cache(self) -> int:
        """清理超过保留期的翻唱语音缓存文件，返回删除数量。"""
        retention_days = self.config.rvc.voice_cache_retention_days
        if retention_days <= 0:
            return 0
        cache_dir = self._voice_cache_dir()
        if not cache_dir.is_dir():
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        for path in cache_dir.glob("sing_*.wav"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue  # 文件可能正被发送流程占用，跳过下次再清
        if removed:
            self.ctx.logger.info("已清理 %d 个超过 %d 天的翻唱语音缓存", removed, retention_days)
        return removed

    async def _voice_cache_cleanup_loop(self) -> None:
        """启动时清理一次，之后每 24 小时巡检一次。"""
        while True:
            try:
                await asyncio.to_thread(self._cleanup_voice_cache)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("清理翻唱语音缓存失败")
            await asyncio.sleep(24 * 3600)

    async def _send_voice(self, audio: bytes | Path, stream_id: str,
                          sender: SingleVoiceSender | None = None):
        """Make exactly one detailed ``voiceurl`` attempt and preserve ambiguity."""
        sender=sender or self._voice_sender
        if sender is None:
            raise RuntimeError('语音发送服务尚未启动或正在重载')
        if isinstance(audio,Path):
            cache_path=audio
        else:
            cache_dir=self._voice_cache_dir()
            await asyncio.to_thread(cache_dir.mkdir,parents=True,exist_ok=True)
            cache_path=cache_dir/f"sing_{uuid.uuid4().hex}.wav"
            await asyncio.to_thread(cache_path.write_bytes,audio)
        absolute=await asyncio.to_thread(cache_path.resolve)
        return await sender.send_file(absolute,stream_id)

    def _find_stream_id(self, stream_id: str, kwargs: dict[str, Any]) -> str:
        sid = str(stream_id or "").strip() or str(kwargs.get("stream_id", "") or "")
        if sid:
            return sid
        msg = kwargs.get("message", {})
        if isinstance(msg, dict):
            sid = str(msg.get("stream_id", "") or "")
            if sid:
                return sid
        return ""

    # ===== 命令 =====

    @staticmethod
    def _command_identity(stream_id: str, kwargs: dict[str, Any]) -> dict[str, str]:
        message = kwargs.get('message')
        info = message.get('message_info') if isinstance(message, dict) else None
        user = info.get('user_info') if isinstance(info, dict) else None
        if not isinstance(user, dict):
            raise ValueError('缺少宿主原始用户消息，不能授予自动回复许可')
        values = {'stream_id': message.get('session_id'), 'platform': message.get('platform'),
                  'user_id': user.get('user_id'), 'message_id': message.get('message_id')}
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256
               for value in values.values()):
            raise ValueError('宿主消息身份不完整，拒绝自动投递')
        if (stream_id != values['stream_id'] or kwargs.get('platform') != values['platform']
                or kwargs.get('user_id') != values['user_id']
                or kwargs.get('text') != message.get('processed_plain_text')
                or message.get('is_command') is not True):
            raise ValueError('宿主消息与会话/平台/用户不匹配，拒绝自动投递')
        return values

    @staticmethod
    def _request_token(identity: dict[str, str]) -> str:
        raw = json.dumps([identity[key] for key in ('platform', 'stream_id', 'user_id', 'message_id')],
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(b'sing-command-v1\0' + raw).hexdigest()

    def _require_active_cover(self) -> ActiveCoverService:
        active=self._active_cover
        if active is None:
            raise RuntimeError('持久化调度服务尚未启动或正在重载')
        return active

    def _require_jobs(self) -> JobService:
        return self._require_active_cover().jobs

    @staticmethod
    def _job_status(job: Any) -> str:
        if job.delivery_state == 'unknown':
            delivery = '平台发送结果未知，可能已送达；严禁自动重发'
        elif job.delivery_state == 'sent':
            delivery = f'平台已确认送达（消息 {job.message_id}）'
        elif job.delivery_state == 'failed':
            delivery = '平台明确拒绝投递；不会自动重发'
        elif job.delivery_state == 'dispatching':
            delivery = '正在投递；不可重复发送'
        else:
            delivery = '自动投递待处理' if job.delivery_state == 'pending' else '未授权/已取消投递'
        failure = f"，阶段原因 {job.error.get('code')}" if job.error else ''
        return f'{job.state}/{job.stage}，进度 {job.chunk_done}/{job.chunk_total}{failure}，{delivery}'

    async def _owned_job(self, active: ActiveCoverService, stream_id: str,
                         kwargs: dict[str,Any], job_id: str):
        identity=self._command_identity(stream_id,kwargs)
        job=await active.jobs.get_job(job_id,stream_id)
        if (job.request.get('platform')!=identity['platform']
                or job.request.get('user_id')!=identity['user_id']):
            raise JobNotFound('无权访问此消息流中的其他用户任务')
        return job

    @Command('翻唱选择', description='选择已显示的准确歌曲版本',
             pattern=r'^(?P<pfx>\S)翻唱选择\s+(?P<job_id>[0-9a-f]{32})\s+(?P<number>\d{1,2})$')
    async def handle_cover_select(self, stream_id: str = '', **kwargs: Any) -> tuple[bool, str, bool]:
        try:
            active=self._require_active_cover()
            groups=kwargs.get('matched_groups') or {}
            job=await self._owned_job(active,stream_id,kwargs,groups.get('job_id',''))
            offer=await active.jobs.choices(job.id,stream_id)
            job=await active.jobs.select(
                job.id,stream_id,offer['offer_id'],int(groups.get('number',0)))
            await self.ctx.send.text(f'已选择曲目，任务 {job.id} 已入队。', stream_id)
            return True, job.id, True
        except (ValueError, JobConflict, JobNotFound, RuntimeError) as exc:
            await self.ctx.send.text(f'选择失败：{exc}', stream_id)
            return False, str(exc), True

    @Command('翻唱状态', description='查询持久化翻唱任务',
             pattern=r'^(?P<pfx>\S)翻唱状态\s+(?P<job_id>[0-9a-f]{32})$')
    async def handle_cover_status(self, stream_id: str = '', **kwargs: Any) -> tuple[bool, str, bool]:
        try:
            active=self._require_active_cover()
            job=await self._owned_job(
                active,stream_id,kwargs,(kwargs.get('matched_groups') or {}).get('job_id',''))
            fault=await active.jobs.coordinator_error(job.id,stream_id)
            detail=(f"；协调器 {fault['code']}：{fault['message']}，约 {fault['retry_after_s']} 秒后重试"
                    if fault is not None else '')
            await self.ctx.send.text(
                f'翻唱任务 {job.id}：{self._job_status(job)}{detail}',stream_id)
            return True, job.id, True
        except (ValueError, JobNotFound, RuntimeError) as exc:
            await self.ctx.send.text(f'查询失败：{exc}', stream_id)
            return False, str(exc), True

    @Command('翻唱取消', description='取消任务及尚未开始的自动投递',
             pattern=r'^(?P<pfx>\S)翻唱取消\s+(?P<job_id>[0-9a-f]{32})$')
    async def handle_cover_cancel(self, stream_id: str = '', **kwargs: Any) -> tuple[bool, str, bool]:
        try:
            active=self._require_active_cover()
            job=await self._owned_job(
                active,stream_id,kwargs,(kwargs.get('matched_groups') or {}).get('job_id',''))
            job=await active.jobs.cancel(job.id,stream_id)
            await self.ctx.send.text(f'取消请求已持久化：{job.id}，{self._job_status(job)}', stream_id)
            return True, job.id, True
        except (ValueError, JobNotFound, JobConflict, RuntimeError) as exc:
            await self.ctx.send.text(f'取消失败：{exc}', stream_id)
            return False, str(exc), True

    @Command(
        "翻唱",
        description="用克隆音色翻唱歌曲（搜歌 → 人声分离 → 换音色）",
        pattern=r"^(?P<pfx>\S)翻唱\s+(?P<query>.+?)(?:\s+--album\s+(?P<album>.+?))?(?:\s+--source-id\s+(?P<source_id>[A-Za-z0-9_-]+))?(?:\s+-v\s+(?P<model>\S+))?(?:\s+(?P<instrumental>--with-instrumental))?(?:\s+(?P<auto_reply>--auto-reply))?$",
        timeout_ms=45_000,  # only bounded catalogue search and ledger writes

    )
    async def handle_cover_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        try:
            identity=self._command_identity(stream_id,kwargs)
            active=self._require_active_cover()
            # Tool/kwargs groups cannot grant consent: parse the Host's original
            # Command text rather than trusting a separately supplied group.
            command_match = re.fullmatch(
                r'\S翻唱\s+(?P<query>.+?)(?:\s+--album\s+(?P<album>.+?))?'
                r'(?:\s+--source-id\s+(?P<source_id>[A-Za-z0-9_-]+))?'
                r'(?:\s+-v\s+(?P<model>\S+))?'
                r'(?:\s+(?P<instrumental>--with-instrumental))?'
                r'(?:\s+(?P<auto_reply>--auto-reply))?', kwargs['text'])
            if command_match is None:
                raise ValueError('翻唱命令格式无效；--auto-reply 只能放在最后')
            matched = command_match.groupdict()
            query = str(matched.get('query') or '').strip()
            model = str(matched.get('model') or '').strip()
            album = str(matched.get('album') or '').strip()
            source_id = str(matched.get('source_id') or '').strip()
            auto_reply = matched.get('auto_reply') == '--auto-reply'
            consent_event = identity['message_id'] if auto_reply else None
            if re.search(r'\s(?:--[A-Za-z-]+|-v)(?=\s|$)', query):
                raise ValueError('命令参数顺序无效；--auto-reply 只能放在最后')
            if not query or ' - ' not in query or not query.rsplit(' - ', 1)[-1].strip():
                raise ValueError('请用 /翻唱 准确歌名 - 艺人名；来源需在候选列表明确选定')
            if model and model not in active.model_aliases:
                raise ValueError('只能使用管理员固定配置的音色')
            if identity['platform']!='qq':
                raise ValueError('目前仅支持 QQ 原始命令授权语音自动投递')
            jobs=active.jobs
            provider=active.provider
            request={'query':query,'provider':provider,'platform':identity['platform'],
                     'user_id':identity['user_id'],
                     'instrumental':matched.get('instrumental')=='--with-instrumental',
                     'album':album,'source_id':source_id,'model_selector':model,
                     'model':active.default_model,'model_path':active.model_path,
                     'auto_reply':auto_reply}
            token = self._request_token(identity)
            # Replayed Command RPCs reuse the exact persisted token; never repeat a
            # search or start a second worker when an offer/selection already exists.
            existing=await jobs.find_request(
                stream_id,token,request,auto_reply=auto_reply,consent_event=consent_event)
            if existing is not None:
                job=existing
            else:
                choices=await jobs.catalogue.search(
                    query,provider,limit=active.search_limit)
                title, artist = [part.strip() for part in query.rsplit(' - ', 1)]
                if not title:
                    raise ValueError('缺少准确歌名')
                choices = [item for item in choices if normalized(item.artist) == normalized(artist)
                           and (normalized(item.title) == normalized(title)
                                or normalized(item.title).startswith(normalized(title) + ' '))]
                if album:
                    choices = [item for item in choices if item.album == album]
                if source_id:
                    choices = [item for item in choices if item.track_id == source_id]
                if not choices:
                    raise ValueError('未找到符合准确曲目/艺人/专辑/来源 ID 的候选；没有启动翻唱')
                job,_=await jobs.admit_offer(
                    stream_id,token,request,choices,auto_reply=auto_reply,
                    consent_event=consent_event,select_single=True)
            if job.state=='needs_selection':
                offer=await jobs.choices(job.id,stream_id)
                choices=offer['items']
                lines=[f'任务 {job.id}：请选择明确版本，发送 /翻唱选择 {job.id} 序号：']
                lines += [f"{i}. {item['title']} - {item['artist']} · {item['album']} · ID {item['track_id']} · {item['availability']}"
                          for i,item in enumerate(choices,1)]
                await self.ctx.send.text('\n'.join(lines),stream_id)
                return True,f'待选择任务 {job.id}',True
            await self.ctx.send.text(f'翻唱任务 {job.id}：{self._job_status(job)}；发送 /翻唱状态 {job.id} 查询。', stream_id)
            return True, f'翻唱任务 {job.id} 已持久化', True
        except (ValueError, JobConflict, CatalogueError, RuntimeError) as exc:
            await self.ctx.send.text(f'翻唱未入队：{exc}', stream_id)
            return False, str(exc), True

    @Command(
        "说",
        description="用克隆音色说话（MiMo TTS → 换音色）",
        pattern=r"^(?P<pfx>\S)说\s+(?P<text>.+)$",
        timeout_ms=600_000,
    )
    async def handle_speak_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        matched = kwargs.get("matched_groups")
        if not isinstance(matched, dict):
            matched = {}
        text = str(matched.get("text", "") or "").strip()
        if not text:
            await self.ctx.send.text("用法：/说 <文本>", stream_id)
            return False, "缺少文本", True

        try:
            sender=self._voice_sender
            if sender is None:
                raise RuntimeError('语音发送服务尚未启动或正在重载')
            model = self._resolve_model("")
            audio = await self._run_speak(text, model, stream_id)
        except Exception as exc:
            self.ctx.logger.exception("说话失败")
            await self.ctx.send.text(f"说话失败：{exc}", stream_id)
            return False, str(exc), True

        try:
            receipt=await self._send_voice(audio,stream_id,sender)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.exception("语音发送失败")
            await self.ctx.send.text(f"语音发送失败：{exc}",stream_id)
            return False,str(exc),True
        if receipt.outcome=='sent' and receipt.message_id:
            return True,f"说: {text}（消息 {receipt.message_id}）",True
        message='语音发送结果未知，可能已送达；不会自动重发。'
        await self.ctx.send.text(message,stream_id)
        return True,message,True

    @Command(
        "音色列表",
        description="列出可用 RVC 音色模型",
        pattern=r"^(?P<pfx>\S)音色列表$",
    )
    async def handle_list_models(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._local is not None:
            await self.ctx.send.text(f"本地固定音色：{self._local.model.name}（Natsume Iroha）", stream_id)
            return True, "本地音色列表", True
        if self._rvc is None:
            await self.ctx.send.text("RVC 客户端未初始化", stream_id)
            return False, "RVC 客户端未初始化", True
        try:
            models = await self._rvc.list_models()
        except Exception as exc:
            self.ctx.logger.exception("获取音色列表失败")
            await self.ctx.send.text(f"获取音色列表失败：{exc}", stream_id)
            return False, str(exc), True
        if not models:
            await self.ctx.send.text("未找到音色模型（请检查 rvc_root 下的 assets/weights）", stream_id)
            return False, "无模型", True
        lines = ["可用音色模型："] + [f"  {name}" for name in models]
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"列出 {len(models)} 个模型", True

    @Command(
        "qq音乐登录",
        description="发起 QQ 音乐扫码登录，bot 发送二维码，手机 QQ 扫码确认即可（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)qq音乐登录\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_qq_music_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        self._start_qq_qrcode_login(stream_id)
        return True, "QQ 扫码登录已发起", True

    @Command(
        "网易云音乐登录",
        description="发起网易云扫码登录，bot 发送二维码，网易云音乐 App 扫码确认即可（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)网易云音乐登录\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_music_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        self._start_netease_qrcode_login(stream_id)
        return True, "网易云扫码登录已发起", True

    @Command(
        "163cookie",
        description="已停用聊天携带 Cookie；请使用安全配置或扫码登录",
        pattern=r"^(?P<pfx>\S)163cookie(?:\s+(?P<cookie>.+))?\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_cookie_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        # Never parse, log, persist, or echo a secret pasted into group chat.
        await self.ctx.send.text('聊天 Cookie 登录已停用。请使用管理员安全配置或 /网易云音乐登录 扫码；若已发送凭据请立即撤回并轮换。', stream_id)
        return False, '聊天 Cookie 登录已停用', True

    @Command(
        "163logintest",
        description="测试网易云登录态，成功时显示账号昵称（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)163logintest\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_netease_login_test(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._music is None:
            await self.ctx.send.text("音乐客户端未初始化", stream_id)
            return False, "未初始化", True
        try:
            profile = await self._music.get_netease_profile()
        except Exception as exc:
            await self.ctx.send.text(f"❌ 网易云登录态异常：{exc}", stream_id)
            return False, str(exc), True
        nickname = profile["nickname"] or "（昵称未知，但登录态有效）"
        await self.ctx.send.text(f"✅ 网易云登录正常：{nickname}", stream_id)
        return True, f"网易云登录正常: {nickname}", True

    @Command(
        "qqlogintest",
        description="测试 QQ 音乐登录态，成功时显示账号昵称（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)qqlogintest\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_qq_login_test(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._music is None:
            await self.ctx.send.text("音乐客户端未初始化", stream_id)
            return False, "未初始化", True
        try:
            profile = await self._music.get_qq_profile()
        except Exception as exc:
            await self.ctx.send.text(f"❌ QQ 音乐登录态异常：{exc}", stream_id)
            return False, str(exc), True
        nickname = profile["nickname"] or "（昵称未知，但登录态有效）"
        await self.ctx.send.text(f"✅ QQ 音乐登录正常：{nickname}", stream_id)
        return True, f"QQ 音乐登录正常: {nickname}", True

    # ===== QQ 扫码登录流程 =====

    def _start_qq_qrcode_login(self, stream_id: str) -> None:
        if self._music is None:
            asyncio.create_task(self.ctx.send.text("音乐客户端未初始化", stream_id))
            return
        if self._qq_login_task is not None and not self._qq_login_task.done():
            self._qq_login_task.cancel()
        self._qq_login_task = asyncio.create_task(self._run_qq_qrcode_login(stream_id))

    async def _run_qq_qrcode_login(self, stream_id: str) -> None:
        """发起 QQ 扫码登录：发二维码 → 轮询状态 → 确认后换票并缓存登录态。"""
        music = self._music
        if music is None:
            return
        try:
            png, qrsig = await music.qq_qrcode_start()
        except Exception as exc:
            await self.ctx.send.text(f"获取 QQ 登录二维码失败：{exc}", stream_id)
            return
        try:
            await self.ctx.send.image(base64.b64encode(png).decode("ascii"), stream_id)
        except Exception as exc:
            self.ctx.logger.warning("二维码图片发送失败: %s", exc)
            await self.ctx.send.text(f"二维码图片发送失败：{exc}", stream_id)
            return
        await self.ctx.send.text("请用手机 QQ 扫描二维码，并在手机上点击确认登录（3 分钟内有效）", stream_id)

        scanned_notified = False
        waited = 0.0
        interval = 2.5
        while waited < 180.0:
            await asyncio.sleep(interval)
            waited += interval
            try:
                state, extra = await music.qq_qrcode_poll(qrsig)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.ctx.send.text(f"查询扫码状态失败：{exc}", stream_id)
                return
            if state == "scanned" and not scanned_notified:
                scanned_notified = True
                await self.ctx.send.text("已扫码，请在手机上确认登录", stream_id)
            elif state == "expired":
                await self.ctx.send.text("二维码已过期，请重新发送 /音乐登录 qq", stream_id)
                return
            elif state == "success":
                uin, sigx = extra.split("|", 1)
                try:
                    cookies = await music.qq_qrcode_finish(uin, sigx)
                except Exception as exc:
                    await self.ctx.send.text(f"QQ 登录失败：{exc}", stream_id)
                    return
                self._save_login_cache("qq", cookies)
                await self.ctx.send.text(f"QQ 音乐登录成功（uin={cookies['uin']}），登录态已保存", stream_id)
                return
        await self.ctx.send.text("等待扫码超时，请重新发送 /qq音乐登录", stream_id)

    # ===== 网易云扫码登录流程 =====

    def _start_netease_qrcode_login(self, stream_id: str) -> None:
        if self._music is None:
            asyncio.create_task(self.ctx.send.text("音乐客户端未初始化", stream_id))
            return
        if self._netease_login_task is not None and not self._netease_login_task.done():
            self._netease_login_task.cancel()
        self._netease_login_task = asyncio.create_task(self._run_netease_qrcode_login(stream_id))

    async def _run_netease_qrcode_login(self, stream_id: str) -> None:
        """发起网易云扫码登录：发二维码 → 轮询状态 → 确认后保存登录态。"""
        music = self._music
        if music is None:
            return
        try:
            png, unikey = await music.netease_qrcode_start()
        except Exception as exc:
            await self.ctx.send.text(f"获取网易云登录二维码失败：{exc}", stream_id)
            return
        # 持久化设备 ID 与匿名 token（下次启动复用，避免匿名注册限频）
        device = music.get_netease_device()
        if device.get("anon_token"):
            self._save_login_cache("netease_device", device)
        try:
            await self.ctx.send.image(base64.b64encode(png).decode("ascii"), stream_id)
        except Exception as exc:
            self.ctx.logger.warning("二维码图片发送失败: %s", exc)
            await self.ctx.send.text(f"二维码图片发送失败：{exc}", stream_id)
            return
        token_note = (
            "" if music.get_netease_device().get("anon_token")
            else "（注意：本次二维码缺少环境令牌，确认时若提示环境异常，请使用管理员安全配置重新登录）"
        )
        await self.ctx.send.text(
            "请打开网易云音乐 App，用 App 内的「扫一扫」扫描此二维码（不要用 QQ/微信扫一扫或相机），"
            "扫描后在 App 弹出的页面点击【确认登录】。二维码 5 分钟内有效" + token_note, stream_id,
        )

        scanned_notified = False
        scanned_reminded = False
        waited = 0.0
        interval = 2.0
        # 轮询直到服务器判定二维码过期（800）或授权成功（803）。
        # unikey 的真实有效期由网易云服务器控制（实测 > 3 分钟），
        # 固定提前退出会导致"手机已确认但 bot 已停止监听"的错位
        while waited < 600.0:
            await asyncio.sleep(interval)
            waited += interval
            try:
                state = await music.netease_qrcode_poll(unikey)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.ctx.send.text(f"查询扫码状态失败：{exc}", stream_id)
                return
            if state == "scanned" and not scanned_notified:
                scanned_notified = True
                await self.ctx.send.text("已扫码，请在手机上点击【确认登录】完成授权", stream_id)
            if scanned_notified and waited > 60 and not scanned_reminded:
                scanned_reminded = True
                await self.ctx.send.text(
                    "仍未收到确认。请确认手机上已点击【确认登录】按钮；"
                    "若扫码后打开的是登录页而不是确认页，请改用网易云音乐 App 内的「扫一扫」重新扫描", stream_id)
            if state == "expired":
                await self.ctx.send.text(
                    "二维码已过期，请重新发送 /网易云音乐登录，扫码后请尽快在手机上点击【确认登录】", stream_id)
                return
            if state == "success":
                self._save_login_cache("netease", {"account": "qr", "cookies": music.get_netease_cookies()})
                nickname = ""
                try:
                    nickname = (await music.get_netease_profile())["nickname"]
                except Exception:
                    pass
                msg = "网易云扫码登录成功"
                if nickname:
                    msg += f"：{nickname}"
                await self.ctx.send.text(msg + "，登录态已保存", stream_id)
                return
        await self.ctx.send.text("等待扫码超时（10 分钟），请重新发送 /网易云音乐登录", stream_id)

    # ===== Tool =====

    @Tool(
        "cover_song",
        description=(
            "用克隆音色翻唱一首歌（bot 亲自开口唱）。仅当用户想让 bot 自己唱时调用，"
            "典型说法：「我想听你唱XX」「你唱一首XX」「翻唱XX」「用你的声音唱XX」。"
            "传参 query 必须为『准确歌名 - 艺人名』，例如 In the Aeroplane Over the Sea - Neutral Milk Hotel；"
            "不能只填歌名。本 Tool 不拥有可信原始用户 message_id，仅提供 /翻唱 命令授权指引；不会入队、渲染或自动发送。"
            "注意：用户只是想听这首歌的原唱/原曲时（如「放一首XX」「发一首XX」「来一首XX的歌」"
            "「放XX听听」），不要调用本工具，应改用 search_and_play_music。"
            "请提示用户亲自发送 /翻唱 准确歌名 - 艺人名：缺省只保存并允许查询状态，不自动发送；仅用户明确要求完成后自动回复时在命令最后加 --auto-reply。涉及歌曲选择须用 /翻唱选择。"
            "任何 stream_id、source_id 或其它工具参数均不可作为自动投递授权。"
        ),
        activation_type=ActivationType.ALWAYS,
        timeout_ms=10_000,  # no rendering, sending or queue writes via untrusted Tool

        parameters=[
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="准确歌名 - 艺人名；必须包含艺人，不能仅用关键词", required=True),
            ToolParameterInfo(name="with_instrumental", param_type=ToolParamType.BOOLEAN, description="是否混入伴奏（默认 false）", required=False),
            ToolParameterInfo(name="album", param_type=ToolParamType.STRING, description="用户明确选择的专辑，可选；不得根据时长猜测", required=False),
            ToolParameterInfo(name="source_id", param_type=ToolParamType.STRING, description="用户明确选择的返回候选曲目ID，可选；不得编造", required=False),
        ],
    )
    async def handle_cover_tool(self, query: str = "", with_instrumental: bool = False, stream_id: str = "", album: str | None = None, source_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        # Tool arguments may contain a fabricated stream_id and do not include an
        # authenticated original message_id. Never convert them into consent.
        del stream_id, album, source_id, with_instrumental, kwargs
        title = query.strip() if isinstance(query, str) else ''
        if not title:
            return {'content': '请向用户询问准确歌名 - 艺人名。Tool 无权发起自动投递。'}
        return {'content': f'请用户本人发送 /翻唱 {title} 发起任务（缺省只保存、可查状态，不自动发语音）；若本人明确希望完成后自动回复，请在命令最后加 --auto-reply。本次仅提供说明，未搜索、未入队、未发送语音。'}

    @Tool(
        "speak_voice",
        description=(
            "用克隆音色说话。当用户要求语音回复、发送了语音消息、或适合语音回复时调用。"
            "本工具会用基础 TTS 合成原声，再用克隆音色替换，发送语音条。"
        ),
        activation_type=ActivationType.ALWAYS,
        timeout_ms=600_000,
        parameters=[
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="要说的文本", required=True),
        ],
    )
    async def handle_speak_tool(self, text: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, str]:
        del text, stream_id, kwargs
        return {'content': '工具没有可信原始用户消息身份，拒绝自动生成或发送语音；请用户亲自使用 /说 命令。'}

    # ===== 编排调用 =====

    async def _run_speak(self, text: str, sid: str, stream_id: str) -> bytes:
        if self._pipeline is None:
            raise RuntimeError("插件未初始化完成")
        cfg = self.config.mimo
        if cfg.rvc_after_tts:
            raise RuntimeError('说话 RVC 尚无受限本地转换路径；关闭 mimo.rvc_after_tts 可使用原生 MiMo TTS')
        reference_b64 = ""
        if cfg.voice_mode == "clone":
            ref_path = cfg.reference_audio.strip()
            if not ref_path:
                raise RuntimeError("clone 模式需配置 mimo.reference_audio 参考音频路径")
            if not Path(ref_path).exists():
                raise RuntimeError(f"参考音频不存在: {ref_path}")
            reference_b64 = base64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
        return await self._pipeline.speak(
            text,
            sid,
            voice_id=cfg.preset_voice,
            reference_audio_base64=reference_b64,
            convert=cfg.rvc_after_tts,
            convert_kwargs=self._convert_kwargs(sid),
        )


def create_plugin() -> SingPlugin:
    return SingPlugin()
