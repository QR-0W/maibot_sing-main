"""MaiBot 翻唱 + 语音插件。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ActivationType, ToolParameterInfo, ToolParamType

from .music.search import MusicSearchClient, SongInfo
from .services.artifact_store import ArtifactStore
from .services.asset_inventory import AssetInventory, AssetPaths, RuntimeVersionProbe
from .services.catalogue_service import CatalogueService, CatalogueError
from .services.delivery_outbox import CustomVoiceSender, DeliveryOutbox, classify_ack
from .services.job_service import JobService, RenderRuntime, RENDER_PARAMETER_POLICY
from .services.job_store import (TRUSTED_QQ_INGRESS, TRUSTED_TOOL_INGRESS, JobConflict,
                                 JobNotFound, JobStore)
from .services.ownership import OwnershipBusy, exclusive
from .services.request_options import COVER_COMMAND_PATTERN, parse_request_text, validate_direct_host_text
from .services.resource_budget import ResourceBudget
from .services.stage_errors import MESSAGES as STAGE_MESSAGES
from .services.stage_coordinator import StageCoordinator
from .services.unit_runner import UnitRunner


from .runtime.catalogue_text import validate_catalogue_text as safe_text


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="默认禁用；管理员审查后明确启用")
    config_version: str = Field(default="0.6.0", description="配置版本")


class RVCConfig(PluginConfigBase):
    """旧 RVC/sidecar 兼容字段已退休；仅 default_model 用于固定音色显示。"""

    __ui_label__ = "历史 RVC 配置（仅 default_model 生效）"
    __ui_order__ = 1

    rvc_root: str = Field(default="", description="退休兼容字段；当前翻唱不读取或启动旧 sidecar")
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
    """退休 TTS 兼容配置；未接入受限媒体单元，不读取参考音频或合成。"""

    __ui_label__ = "历史 MiMo TTS 配置（暂不支持）"
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
    musicdl_python: str = Field(default="", description="退休兼容字段；下载只走受限 JobService 媒体阶段")
    rvc_script: str = Field(default="", description="受限 RVC 脚本绝对路径；不执行未隔离 WebUI")
    inference_lock: str = Field(default="", description="与本机其他推理共享的绝对锁文件路径，父目录须存在")
    max_queue: int = Field(default=2, ge=0, le=8)
    timeout_s: int = Field(default=900, ge=60, le=900, description="退休 LocalBackend 兼容字段；受限阶段使用各自预算")
    max_duration_s: int = Field(default=300, ge=30, le=300)
    max_download_bytes: int = Field(default=67108864, ge=1048576, le=67108864)
    local_source_allowlist: list[str] = Field(default_factory=list, description="退休 LocalBackend 兼容字段；不允许旧本地输入旁路")
    memory_max_mb: int = Field(default=2048, ge=512, le=6144,
        description="媒体服务及子进程硬内存上限（MiB）；软限为75%，超限终止并记录错误。此额度不包含 SDK Runner 入口内存，也不保证其它服务不会耗尽容器。")
    memory_headroom_mb: int = Field(default=512, ge=256, le=4096,
        description="每个有限共享祖先除完整媒体预算外须额外保留的 MiB；容量未知或不足就拒绝，不启动媒体")
    cpu_quota_percent: int = Field(default=150, ge=25, le=400, description="单个阶段服务的 CPU 配额百分比")
    media_tasks_max: int = Field(default=64, ge=8, le=512, description="单个阶段服务的最大进程/线程数")


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
    budget: ResourceBudget

    def path(self,name: str) -> Path:
        return dict(self.paths)[name]


class SingPlugin(MaiBotPlugin):
    """翻唱 + 语音插件。"""

    config_model = SingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._music: MusicSearchClient | None = None
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

    def _raise_cleanup_errors(self, errors: list[tuple[str,BaseException]]) -> None:
        if not errors:
            return
        for label,error in errors:
            try:
                self.ctx.logger.error('资源清理失败（%s）：%s',label,error,
                                      exc_info=(type(error),error,error.__traceback__))
            except BaseException:
                pass
        cancellations=[error for _,error in errors
                       if isinstance(error,asyncio.CancelledError)]
        if cancellations:
            primary=cancellations[0]
            for label,error in errors:
                if error is not primary:
                    primary.add_note(f'另一个清理步骤失败（{label}）：{error!r}')
            raise primary
        fatal=[error for _,error in errors if not isinstance(error,Exception)]
        if fatal:
            primary=fatal[0]
            for label,error in errors:
                if error is not primary:
                    primary.add_note(f'另一个清理步骤失败（{label}）：{error!r}')
            raise primary
        regular=[error for _,error in errors]
        if len(regular)==1:
            raise regular[0]
        raise ExceptionGroup('多个插件资源清理步骤失败',regular)

    async def _close_after_failure(self, original: BaseException) -> None:
        try:
            await self._close_resources()
        except BaseException as cleanup:
            try:
                self.ctx.logger.error('启动/重载失败后的资源清理也失败：%s',cleanup,
                                      exc_info=(type(cleanup),cleanup,cleanup.__traceback__))
            except BaseException:
                pass
            if (isinstance(cleanup,asyncio.CancelledError)
                    and not isinstance(original,asyncio.CancelledError)):
                cleanup.add_note(f'取消前的原始启动/重载失败：{original!r}')
                raise cleanup from original
            original.add_note(f'后续资源清理失败：{cleanup!r}')
            raise original from cleanup

    async def on_load(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._load_resources()
            except BaseException as original:
                await self._close_after_failure(original)
                raise

    async def _load_resources(self) -> None:
        self.ctx.logger.info("翻唱插件加载中...")
        # Copy every scalar needed below before the first await. A Host config
        # callback can replace self.config later, but this lifecycle owns one snapshot.
        durable=self._capture_durable_settings()
        self._music=self._build_music_client()
        await self._restore_music_logins()
        await self._start_durable_services(durable)
        self.ctx.logger.info("翻唱插件加载完成（0.6.1 source-window-v4；excerpt源≤900秒/上下文≤90秒；Tool 来源策略：first-candidate-v1）")

    async def _stop_durable_services(self) -> None:
        self._active_cover=None
        task,self._delivery_task=self._delivery_task,None
        outbox,self._outbox=self._outbox,None
        jobs,self._jobs=self._jobs,None
        owner,self._scheduler_owner=self._scheduler_owner,None
        pending=tuple(self._startup_tasks)
        errors: list[tuple[str,BaseException]]=[]
        if task is not None:
            task.cancel()
            try:
                await asyncio.gather(task,return_exceptions=True)
            except BaseException as exc:
                errors.append(('delivery_task',exc))
        # An in-flight platform send is fenced unknown on next startup. Never
        # launch a replacement send for pending/dispatching/unknown automatically.
        if outbox is not None:
            try:
                await outbox.shutdown(timeout_s=1.0)
            except BaseException as exc:
                errors.append(('outbox',exc))
        if jobs is not None:
            try:
                await jobs.close()
            except BaseException as exc:
                errors.append(('jobs',exc))
        if owner is not None:
            try:
                owner.__exit__(None,None,None)
            except BaseException as exc:
                errors.append(('scheduler_owner',exc))
        if pending:
            try:
                await asyncio.wait(pending,timeout=1.0)
            except BaseException as exc:
                errors.append(('startup_tasks',exc))
        self._raise_cleanup_errors(errors)

    async def _close_resources(self) -> None:
        login_tasks=[task for task in (self._qq_login_task,self._netease_login_task)
                     if task is not None]
        self._qq_login_task=self._netease_login_task=None
        music,self._music=self._music,None
        errors: list[tuple[str,BaseException]]=[]
        try:
            await self._stop_durable_services()
        except BaseException as exc:
            errors.append(('durable_services',exc))
        for task in login_tasks:
            task.cancel()
        if login_tasks:
            try:
                await asyncio.gather(*login_tasks,return_exceptions=True)
            except BaseException as exc:
                errors.append(('login_tasks',exc))
        if music is not None:
            try:
                await music.close()
            except BaseException as exc:
                errors.append(('music_client',exc))
        self._raise_cleanup_errors(errors)
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
            except BaseException as original:
                await self._close_after_failure(original)
                raise

    # ===== 持久化渲染与投递 =====

    def _resource_budget(self) -> ResourceBudget:
        """Build the validated per-stage budget from administrator config.

        Invalid values raise a Chinese ValueError here, so the mistake surfaces
        while loading/updating config instead of when a job is already queued.
        """
        cfg = self.config.local
        return ResourceBudget.from_config(
            memory_max_mb=cfg.memory_max_mb, cpu_quota_percent=cfg.cpu_quota_percent,
            tasks_max=cfg.media_tasks_max, headroom_mb=cfg.memory_headroom_mb)

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
            default_model=self.config.rvc.default_model.strip(),
            budget=self._resource_budget())

    async def _start_durable_services(self, settings: DurableCoverSettings) -> None:
        path=settings.path
        root,output,runtime_dir=settings.root,settings.output,settings.runtime_dir
        # The scheduler lock parent is the only pre-service directory creation.
        # Bind durable storage before recovery; generation assets are only needed
        # for admission and new media launches, never old-unit reconciliation.
        await asyncio.to_thread(root.mkdir,parents=True,exist_ok=True,mode=0o700)
        ownership_fd=await self._acquire_scheduler_owner(
            root/'.durable-scheduler.lock')
        store=await self._startup_owned_call(
            ownership_fd,JobStore,root/'jobs.sqlite3',max(1,settings.max_queue+1))
        runner=UnitRunner(runtime_dir/'stage_executor.py',path('worker_python'),
                          settings.budget)
        asset_paths=AssetPaths(
            model=path('model_path'),index=path('index_path'),hubert=path('hubert_path'),
            demucs_repo=path('demucs_repo_path'),rvc_script=path('rvc_script'),
            rvc_upstream=path('rvc_upstream_path'),media_stage=runtime_dir/'media_stage.py',
            worker=runtime_dir/'worker.py',render_plan=runtime_dir/'render_plan.py',
            stage_executor=runtime_dir/'stage_executor.py',
            excerpt_selection=runtime_dir/'excerpt_selection.py',
            context_window=runtime_dir/'context_window.py')
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
            'excerpt_selection':runtime_dir/'excerpt_selection.py',
            'context_window':runtime_dir/'context_window.py',
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
        await jobs.prepare_storage()
        if not isinstance(jobs.artifacts,ArtifactStore):
            raise RuntimeError('持久化产物存储未完成绑定')
        outbox=DeliveryOutbox(
            store,CustomVoiceSender(self.ctx.send.custom),jobs.artifacts.verify)
        self._outbox=outbox
        # Root is already validated and bound. Recovery must precede any worker
        # claim or delivery discovery.
        await outbox.recover()
        await jobs.recover_failure_notices()
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
                    for job_id,stream_id in await jobs.failure_notice_candidates(limit=8):
                        await self._dispatch_failure_notice(jobs, job_id, stream_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception('持久化投递巡检失败；不得无凭据重发')
            await asyncio.sleep(2)

    async def _dispatch_failure_notice(self, jobs: JobService, job_id: str, stream_id: str) -> None:
        """Claim before RPC; any uncertain send is fenced, never retried."""
        claim_task = asyncio.create_task(jobs.claim_failure_notice(job_id, stream_id))
        try:
            notice = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            # SQLite cannot be interrupted. Fence its eventual claim even if
            # unload cancelled the caller before the transaction returned.
            async def abandon_claim() -> None:
                claimed = await claim_task
                if claimed is not None:
                    await jobs.settle_failure_notice(job_id, claimed['token'], 'unknown')
            self._track_startup_task(asyncio.create_task(abandon_claim(), name='sing-notice-claim-close'))
            raise
        if notice is None:
            return
        message = STAGE_MESSAGES.get(notice['code'], '任务处理失败；私有日志保留了诊断。')
        text = f"翻唱任务 {job_id} 已停止（{notice['code']}）：{message} 不会自动重试或发送未完成音频。"
        try:
            ack = await asyncio.wait_for(self.ctx.send.text(
                text, stream_id, return_details=True, _rpc_timeout_ms=15_000), timeout=20)
        except BaseException:
            await asyncio.shield(jobs.settle_failure_notice(job_id, notice['token'], 'unknown'))
            raise
        receipt = classify_ack(ack)
        await asyncio.shield(jobs.settle_failure_notice(
            job_id, notice['token'], receipt.outcome, message_id=receipt.message_id))

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
        if (not isinstance(kwargs.get('text'), str) or not kwargs['text'].strip()
                or stream_id != values['stream_id'] or kwargs.get('platform') != values['platform']
                or kwargs.get('user_id') != values['user_id']
                or kwargs.get('text') != message.get('processed_plain_text')
                or message.get('is_command') is not True):
            raise ValueError('宿主消息与会话/平台/用户不匹配，拒绝自动投递')
        validate_direct_host_text(message, kwargs['text'])
        # Current trusted NapCat codec supplies self_id/message_type/target;
        # Host's registered gateway adds its independently resolved account_id.
        # WebUI virtual QQ supplies only at_bot, even with a real QQ group ID.
        extra = info.get('additional_config')
        if values['platform'] != 'qq' or not isinstance(extra, dict):
            raise ValueError('不可信平台或缺少 QQ 网关证明')
        self_id, account = extra.get('self_id'), extra.get('platform_io_account_id')
        if (not isinstance(self_id, str) or not self_id.strip()
                or len(self_id) > 256 or not isinstance(account, str)
                or not account.strip() or self_id != account):
            raise ValueError('QQ 网关账号证明不一致')
        group = info.get('group_info')
        message_type = extra.get('napcat_message_type')
        if isinstance(group, dict):
            group_id = group.get('group_id')
            if (message_type != 'group' or not isinstance(group_id, str)
                    or not group_id.strip() or len(group_id) > 256
                    or group_id.startswith('webui_virtual_group_')
                    or extra.get('platform_io_target_group_id') != group_id
                    or extra.get('platform_io_target_user_id') is not None):
                raise ValueError('QQ 群消息路由证明不一致')
        elif group is None:
            if (message_type != 'private'
                    or extra.get('platform_io_target_user_id') != values['user_id']
                    or extra.get('platform_io_target_group_id') is not None):
                raise ValueError('QQ 私聊路由证明不一致')
        else:
            raise ValueError('QQ 消息群身份无效')
        return values

    @staticmethod
    def _trusted_command_identity(stream_id: str, kwargs: dict[str, Any]) -> Optional[dict[str, str]]:
        """Fail closed before *any* send, even an error reply, or side effect."""
        try:
            return SingPlugin._command_identity(stream_id, kwargs)
        except ValueError:
            return None

    @staticmethod
    def _request_token(identity: dict[str, str]) -> str:
        raw = json.dumps([identity[key] for key in ('platform', 'stream_id', 'user_id', 'message_id')],
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(b'sing-command-v1\0' + raw).hexdigest()

    @staticmethod
    def _tool_session_identity(payload: dict[str, Any]) -> dict[str, str]:
        """Validate the QQ Tool context using the accepted Host/LLM boundary.

        The Host merges `stream_id`/`chat_id`/`platform`/`user_id` from the real
        execution context, but a model argument of the same name wins over it.
        The Host always emits both `stream_id` and `chat_id` from one value, so a
        mismatch proves one of them was overwritten; only the exact same value is
        accepted. This is a consistency check, not a signature: it does not stop
        a prompt-injected model that forges *both* consistently, which is why the
        Tool path is recorded under its own weaker ingress proof.
        """
        stream = payload.get('stream_id')
        chat = payload.get('chat_id')
        platform = payload.get('platform')
        user = payload.get('user_id')
        group = payload.get('group_id')
        if (not isinstance(stream, str) or not stream.strip() or len(stream) > 256
                or stream != chat):
            raise ValueError('工具调用缺少一致的宿主会话上下文，未入队')
        if platform != 'qq' or not isinstance(user, str) or not user.strip() or len(user) > 256:
            raise ValueError('仅支持来自真实 QQ 会话的工具调用，未入队')
        if group is not None and (not isinstance(group, str) or not group.strip() or len(group) > 256):
            raise ValueError('工具调用的群会话上下文无效，未入队')
        # WebUI virtual-QQ chats reuse platform 'qq'; their group ids are marked.
        if any(isinstance(value, str) and value.startswith('webui_virtual_group_')
               for value in (stream, group)):
            raise ValueError('WebUI 虚拟 QQ 会话不能创建生产翻唱任务，未入队')
        return {'stream_id': stream, 'platform': 'qq', 'user_id': user, 'group_id': group or ''}

    @staticmethod
    def _tool_request_token(identity: dict[str, str], request: dict[str, Any]) -> str:
        """Intent key, not a permanent request token or an original-message ID."""
        raw = json.dumps({'identity': identity, 'request': request}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(b'sing-tool-v2\0' + raw).hexdigest()

    def _require_active_cover(self) -> ActiveCoverService:
        active=self._active_cover
        if active is None:
            raise RuntimeError('持久化调度服务尚未启动或正在重载')
        return active

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

    @staticmethod
    def _source_choice_lines(job_id: str, items: list[dict[str, Any]]) -> list[str]:
        """Display the frozen ordinal/availability without substituting recordings."""
        selectable = any(item['availability'] in ('unknown', 'available') for item in items)
        header = (f'任务 {job_id}：请选择标为可选的版本，发送 /翻唱选择 {job_id} 序号：'
                  if selectable else
                  f'任务 {job_id}：候选均不可选，尚未入队；无需发送选择命令。'
                  '请处理下列原因后重新请求，不会自动换用其他版本。')
        labels = {'unknown': '可选，平台访问待取链确认', 'available': '可选，取链后确认',
                  'requires_login': '需管理员登录音乐平台（不可选）',
                  'unavailable': '平台当前不可访问（不可选）',
                  'over_limit': '超过当前来源时长上限（不可选）'}
        return [header] + [
            f"{index}. {item['title']} - {item['artist']} · {item['album']} · "
            f"ID {item['track_id']} · {labels[item['availability']]}"
            for index, item in enumerate(items, 1)]

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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        description="/翻唱 原始命令即授权完成后单次送达；默认完整伴奏文件，各选项独立覆盖",
        pattern=COVER_COMMAND_PATTERN,
        timeout_ms=45_000,  # only bounded catalogue search and ledger writes

    )
    async def handle_cover_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        identity = self._trusted_command_identity(stream_id, kwargs)
        if identity is None:
            return False, '未验证的 QQ 命令来源', True
        try:
            # No Hook/cache binding: slash requests reach this Host Command
            # after chat-disable/operator checks. Every request selector,
            # including consent, comes only from the validated original text.
            options = parse_request_text(kwargs['message']['processed_plain_text'])
            active=self._require_active_cover()
            query, model = options.query, options.model_selector
            album, source_id = options.album, options.source_id
            auto_reply = options.auto_reply
            consent_event = identity['message_id'] if auto_reply else None
            if model and model not in active.model_aliases:
                raise ValueError('只能使用管理员固定配置的音色')
            if identity['platform']!='qq':
                raise ValueError('目前仅支持 QQ 原始命令授权语音自动投递')
            jobs=active.jobs
            provider=active.provider
            request={'title':options.title,'artist':options.artist,
                     'query':query,'provider':provider,'platform':identity['platform'],
                     'user_id':identity['user_id'],
                     'entry_kind':options.entry_kind,'render_mode':options.render_mode,
                     'delivery_mode':options.delivery_mode,'instrumental':options.instrumental,
                     'album':album,'source_id':source_id,'model_selector':model,
                     'model':active.default_model,'model_path':active.model_path,
                     'auto_reply':auto_reply,'legacy_auto_reply_flag':options.legacy_auto_reply_flag,
                     'ingress_proof':TRUSTED_QQ_INGRESS}
            token = self._request_token(identity)
            # Replayed Command RPCs reuse the exact persisted token; never repeat a
            # search or start a second worker when an offer/selection already exists.
            job = await jobs.search_and_admit(
                title=options.title, artist=options.artist,
                provider=active.provider, search_limit=active.search_limit,
                stream_id=stream_id, token=token, request=request,
                album=album, source_id=source_id,
                auto_reply=auto_reply, consent_event=consent_event)
            if job.state=='needs_selection':
                offer=await jobs.choices(job.id,stream_id)
                choices=offer['items']
                lines = self._source_choice_lines(job.id, choices)
                await self.ctx.send.text('\n'.join(lines),stream_id)
                return True,f'待选择任务 {job.id}',True
            await self.ctx.send.text(f'翻唱任务 {job.id}：{self._job_status(job)}；发送 /翻唱状态 {job.id} 查询。', stream_id)
            return True, f'翻唱任务 {job.id} 已持久化', True
        except (ValueError, JobConflict, CatalogueError, RuntimeError) as exc:
            await self.ctx.send.text(f'翻唱未入队：{exc}', stream_id)
            return False, str(exc), True

    @Command(
        "说",
        description="说话功能未接入受限媒体单元，当前明确不支持",
        pattern=r"^(?P<pfx>\S)说\s+(?P<text>.+)$",
        timeout_ms=10_000,
    )
    async def handle_speak_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
        message='说话功能暂不支持：TTS 与旧后处理尚未接入受限媒体单元；不会合成、读取参考音频或发送语音。'
        await self.ctx.send.text(message,stream_id)
        return False,message,True

    @Command(
        "音色列表",
        description="列出可用 RVC 音色模型",
        pattern=r"^(?P<pfx>\S)音色列表$",
    )
    async def handle_list_models(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
        active=self._active_cover
        if active is None:
            message='持久化音色服务尚未就绪或正在重载。'
            await self.ctx.send.text(message,stream_id)
            return False,message,True
        name=Path(active.model_path).name
        message=(f'管理员固定音色模型文件：{name}。'
                 '该文件名不代表角色身份、训练来源或使用权已经验证。')
        await self.ctx.send.text(message,stream_id)
        return True,message,True

    @Command(
        "qq音乐登录",
        description="发起 QQ 音乐扫码登录，bot 发送二维码，手机 QQ 扫码确认即可（仅管理员可用）",
        pattern=r"^(?P<pfx>\S)qq音乐登录\s*$",
        permission="operator",
        timeout_ms=60_000,
    )
    async def handle_qq_music_login(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
        if self._trusted_command_identity(stream_id, kwargs) is None:
            return False, '未验证的 QQ 命令来源', True
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
            "用克隆音色翻唱一首歌（bot 亲自开口唱），用户用自然语言提出翻唱时调用本工具。"
            "典型说法：「你能唱一下朴树的生如夏花吗？」「我想听你唱XX」「请翻唱XX的YY」「用你的声音唱XX」。"
            "请把用户说到的曲目拆成 title（歌名）与 artist（歌手）两个参数，不要只填关键词、不要编造。"
            "工具会在准确歌名、艺人及明确来源条件过滤后自动绑定第一个候选，不让用户手动选择。"
            "首项不可用时返回未入队原因，不静默改唱后面的版本。请按真实结果回复："
            "只有 queued/running 等状态才表示接纳，完成后向当前会话最多发送一次；不要提前声称已经发送。"
            "注意：用户只是想听原唱/原曲时（如「放一首XX」「发一首XX」「来一首XX的歌」），"
            "不要调用本工具，应改用 search_and_play_music。"
        ),
        timeout_ms=45_000,  # normal MaiCore deferred discovery; admission only
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING,
                              description="准确歌名，例如 生如夏花；不要包含歌手名", required=True),
            ToolParameterInfo(name="artist", param_type=ToolParamType.STRING,
                              description="准确歌手/艺人名，例如 朴树；不知道时必须先问用户，不能编造", required=True),
            ToolParameterInfo(name="instrumental", param_type=ToolParamType.BOOLEAN,
                              description="是否带伴奏；不填按纯人声片段处理", required=False),
            ToolParameterInfo(name="render_mode", param_type=ToolParamType.STRING,
                              description="full=整曲，excerpt=12–18 秒片段（默认）", required=False),
            ToolParameterInfo(name="delivery_mode", param_type=ToolParamType.STRING,
                              description="voice=语音条（默认），file=音频文件", required=False),
            ToolParameterInfo(name="album", param_type=ToolParamType.STRING,
                              description="用户明确指定的专辑名，可选；不得根据时长猜测", required=False),
            ToolParameterInfo(name="source_id", param_type=ToolParamType.STRING,
                              description="用户明确指定的候选曲目ID，可选；不得编造", required=False),
        ],
    )
    async def handle_cover_tool(self, title: str = "", artist: str = "", instrumental: Any = None,
                                render_mode: Any = None, delivery_mode: Any = None,
                                album: Any = None, source_id: Any = None,
                                **kwargs: Any) -> dict[str, Any]:
        """Admit one cover request from a real QQ session context.

        Session fields come from the Host execution context; see
        `_tool_session_identity` for the consistency check and its limits.
        """
        try:
            identity = self._tool_session_identity(kwargs)
            clean_title, clean_artist = safe_text(title), safe_text(artist)
            mode = 'excerpt' if render_mode in (None, '') else render_mode
            transport = 'voice' if delivery_mode in (None, '') else delivery_mode
            backing = False if instrumental is None else instrumental
            if not isinstance(mode, str) or mode not in ('full', 'excerpt'):
                raise ValueError('render_mode 只能是 full 或 excerpt')
            if not isinstance(transport, str) or transport not in ('file', 'voice'):
                raise ValueError('delivery_mode 只能是 file 或 voice')
            if type(backing) is not bool:
                raise ValueError('instrumental 必须是布尔值')
            chosen_album = safe_text(album) if album not in (None, '') else ''
            if source_id is None or source_id == '':
                chosen_source = ''
            elif isinstance(source_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source_id):
                chosen_source = source_id
            else:
                raise ValueError('source_id 格式无效')
            query = f'{clean_title} - {clean_artist}'
            active = self._require_active_cover()
            request = {'title': clean_title, 'artist': clean_artist,
                       'query': query, 'provider': active.provider, 'platform': identity['platform'],
                       'user_id': identity['user_id'], 'entry_kind': 'tool',
                       'source_selection_policy': 'first-candidate-v1',
                       'render_mode': mode, 'delivery_mode': transport, 'instrumental': backing,
                       'album': chosen_album, 'source_id': chosen_source, 'model_selector': '',
                       'model': active.default_model, 'model_path': active.model_path,
                       'auto_reply': True, 'legacy_auto_reply_flag': False,
                       'ingress_proof': TRUSTED_TOOL_INGRESS}
            intent_key = self._tool_request_token(identity, request)
            token, job = await active.jobs.bind_tool_request(
                identity['stream_id'], intent_key, request, auto_reply=True)
            if job is None:
                job = await active.jobs.search_and_admit(
                    title=clean_title, artist=clean_artist,
                    provider=active.provider, search_limit=active.search_limit,
                    stream_id=identity['stream_id'], token=token, request=request,
                    album=chosen_album, source_id=chosen_source,
                    auto_reply=True, consent_event=token)
            return await self._cover_tool_result(active.jobs, job, identity['stream_id'])
        except (ValueError, JobConflict, CatalogueError, RuntimeError) as exc:
            if isinstance(exc, (ValueError, CatalogueError)):
                explanation = str(exc)
            elif isinstance(exc, JobConflict):
                explanation = '任务接纳冲突或队列已满；没有创建第二个任务'
            else:
                self.ctx.logger.exception('翻唱工具接纳失败，诊断不输出到聊天')
                explanation = '持久化调度服务未就绪或接纳失败，请管理员查看私有日志'
            code = 'invalid_request' if isinstance(exc, ValueError) else 'admission_failed'
            if isinstance(exc, CatalogueError) and exc.code in ('source_duration_limit', 'source_unavailable'):
                code = exc.code
            return {'success': False, 'job_id': None, 'state': 'not_admitted',
                    'code': code, 'content': f'翻唱未入队：{explanation}。'}

    async def _cover_tool_result(self, jobs: JobService, job: Any,
                                 stream_id: str) -> dict[str, Any]:
        """One public result format for admission and same-session status."""
        failed = job.state in ('failed', 'interrupted', 'cancelled')
        raw_code = job.error.get('code') if job.error else None
        code = raw_code if raw_code in STAGE_MESSAGES else ('job_failed' if failed else job.state)
        result = {'success': not failed, 'job_id': job.id, 'state': job.state,
                  'code': code, 'delivery_state': job.delivery_state}
        if job.state == 'needs_selection':
            offer = await jobs.choices(job.id, stream_id)
            lines = self._source_choice_lines(job.id, offer['items'])
            if not any(item['availability'] in ('unknown', 'available') for item in offer['items']):
                result.update(success=False, code='source_unavailable')
            result.update(content='\n'.join(lines), choices=offer['items'])
        else:
            detail = STAGE_MESSAGES.get(code, '任务已停止；不会自动重试。') if failed else ''
            delivery_hint = ('不会自动重试或发送未完成音频。' if failed else
                             '已授权的完成产物最多尝试一次投递，未知结果不重发。')
            selected_hint = ''
            if (job.request.get('source_selection_policy') == 'first-candidate-v1'
                    and job.selected_source):
                selected = job.selected_source
                result['selected_source'] = {key: selected[key] for key in
                    ('provider', 'track_id', 'title', 'artist', 'album')}
                selected_hint = (f"已自动绑定第一个匹配候选：{selected['title']} - {selected['artist']}"
                                 f"（{selected['album']}，ID {selected['track_id']}）；无需选择序号。")
            result['content'] = (selected_hint + f'翻唱任务 {job.id}：{self._job_status(job)}。{detail}'
                                 f'{delivery_hint}可用 /翻唱状态 {job.id} 查询、'
                                 f'/翻唱取消 {job.id} 取消。')
        return result

    @Tool(
        'cover_song_status',
        description='查询本 QQ 会话当前用户的翻唱任务状态；使用 cover_song 返回的 job_id，不得编造 ID。',
        timeout_ms=10_000,
        parameters=[ToolParameterInfo(name='job_id', param_type=ToolParamType.STRING,
                                      description='cover_song 返回的任务 ID', required=True)],
    )
    async def handle_cover_status_tool(self, job_id: str = '', **kwargs: Any) -> dict[str, Any]:
        try:
            identity = self._tool_session_identity(kwargs)
            if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{32}', job_id):
                raise ValueError('无效的翻唱任务 ID')
            active = self._require_active_cover()
            job = await active.jobs.get_job(job_id, identity['stream_id'])
            if (job.request.get('platform') != identity['platform']
                    or job.request.get('user_id') != identity['user_id']):
                raise JobNotFound('无权访问其他用户的任务')
            return await self._cover_tool_result(active.jobs, job, identity['stream_id'])
        except (ValueError, JobNotFound, RuntimeError):
            return {'success': False, 'job_id': None, 'state': 'unavailable',
                    'code': 'job_unavailable', 'content': '任务不存在、不可访问或调度服务尚未就绪。'}

    @Tool(
        "speak_voice",
        description="说话功能暂不支持：TTS 与旧后处理未接入受限媒体单元，不合成或发送语音。",
        activation_type=ActivationType.ALWAYS,
        timeout_ms=10_000,
        parameters=[
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="要说的文本", required=True),
        ],
    )
    async def handle_speak_tool(self, text: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del text, stream_id, kwargs
        return {'success': False, 'code': 'unsupported',
                'content': '工具没有可信原始用户消息身份，拒绝自动生成或发送语音；说话功能暂不支持，/说 也不会合成或发送语音。'}


def create_plugin() -> SingPlugin:
    return SingPlugin()
