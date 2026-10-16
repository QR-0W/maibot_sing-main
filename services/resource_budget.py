"""One immutable media-unit budget and exact cgroup admission checks.

The maximum is an administrator policy, not a guess at host RAM. Admission uses
real cgroup capacity and usage (including charged file cache); unknown capacity
refuses launch. The budget never shrinks silently, and swap is always disabled.
This module uses only the standard library and never launches a process.
"""
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional
import re

BYTES_PER_MIB = 1024 * 1024
BYTES_PER_GIB = 1024 * 1024 * 1024
MIN_MEMORY_MAX_BYTES = 512 * BYTES_PER_MIB
MAX_MEMORY_MAX_BYTES = 6 * BYTES_PER_GIB
MIN_CPU_QUOTA_PERCENT = 25
MAX_CPU_QUOTA_PERCENT = 400
MIN_TASKS_MAX = 8
MAX_TASKS_MAX = 512
MIN_RUNTIME_MAX_SECONDS = 30
MAX_RUNTIME_MAX_SECONDS = 7200
MIN_HEADROOM_BYTES = 256 * BYTES_PER_MIB
_CGROUP_INT = re.compile(r'[0-9]+\Z')


class ResourceBudgetError(RuntimeError):
    code = 'resource_budget_error'

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class CgroupUnavailable(ResourceBudgetError):
    code = 'cgroup_unavailable'


class InsufficientMemoryError(ResourceBudgetError):
    code = 'insufficient_memory'

    def __init__(self, message: str, *, required_bytes: int, available_bytes: int):
        super().__init__(message)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


def _int_field(value, name, low, high):
    if type(value) is not int:
        raise ValueError('%s 必须是 int，实际为 %s' % (name, type(value).__name__))
    if not low <= value <= high:
        raise ValueError('%s 必须在 [%d, %d] 内，实际为 %d' % (name, low, high, value))
    return value


def _human_bytes(value):
    if value % BYTES_PER_GIB == 0:
        return '%d GiB' % (value // BYTES_PER_GIB)
    if value % BYTES_PER_MIB == 0:
        return '%d MiB' % (value // BYTES_PER_MIB)
    return '%d 字节' % value


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    memory_max_bytes: int
    memory_high_bytes: int
    cpu_quota_percent: int
    tasks_max: int
    headroom_bytes: int
    runtime_max_seconds: int
    swap_max_bytes: int = 0

    def __post_init__(self):
        _int_field(self.memory_max_bytes, 'memory_max_bytes',
                   MIN_MEMORY_MAX_BYTES, MAX_MEMORY_MAX_BYTES)
        _int_field(self.memory_high_bytes, 'memory_high_bytes',
                   MIN_HEADROOM_BYTES, MAX_MEMORY_MAX_BYTES)
        if self.memory_high_bytes >= self.memory_max_bytes:
            raise ValueError('memory_high_bytes 必须小于 memory_max_bytes')
        _int_field(self.swap_max_bytes, 'swap_max_bytes', 0, 0)
        _int_field(self.cpu_quota_percent, 'cpu_quota_percent',
                   MIN_CPU_QUOTA_PERCENT, MAX_CPU_QUOTA_PERCENT)
        _int_field(self.tasks_max, 'tasks_max', MIN_TASKS_MAX, MAX_TASKS_MAX)
        _int_field(self.headroom_bytes, 'headroom_bytes',
                   MIN_HEADROOM_BYTES, MAX_MEMORY_MAX_BYTES)
        _int_field(self.runtime_max_seconds, 'runtime_max_seconds',
                   MIN_RUNTIME_MAX_SECONDS, MAX_RUNTIME_MAX_SECONDS)

    @classmethod
    def from_config(cls, memory_max_mb=2048, cpu_quota_percent=150, tasks_max=64,
                    runtime_max_seconds=1200, headroom_mb=512):
        maximum = _int_field(memory_max_mb, 'memory_max_mb', 512, 6144) * BYTES_PER_MIB
        headroom = _int_field(headroom_mb, 'headroom_mb', 256, 6144) * BYTES_PER_MIB
        return cls(maximum, maximum * 3 // 4, cpu_quota_percent, tasks_max,
                   headroom, runtime_max_seconds)

    def cgroup_limits(self, path='/sys/fs/cgroup'):
        return cgroup_limits(path)

    def check_headroom(self, limits):
        return check_headroom(self, limits)

    def systemd_properties(self):
        return systemd_properties(self)

    def describe(self):
        return describe(self)


def _read_counter(base, name, *, allow_max=False):
    location = base / name
    if location.is_symlink():
        raise CgroupUnavailable('cgroup 文件 %s 是符号链接，拒绝读取' % name)
    try:
        raw = location.read_text(encoding='utf-8')
    except OSError as exc:
        raise CgroupUnavailable('无法读取 cgroup 文件 %s：%s' % (name, exc.strerror)) from exc
    value = raw.strip()
    if allow_max and value == 'max':
        return None
    if not _CGROUP_INT.fullmatch(value):
        raise CgroupUnavailable('cgroup 文件 %s 内容无效：%r' % (name, raw))
    return int(value)


def cgroup_limits(path='/sys/fs/cgroup'):
    if not isinstance(path, (str, Path)) or not str(path):
        raise ValueError('cgroup 路径必须是非空 str 或 Path')
    base = Path(path)
    if not base.is_dir() or base.is_symlink():
        raise CgroupUnavailable('cgroup 路径 %s 不存在或不安全' % base)
    return {
        'path': str(base),
        'memory_max_bytes': _read_counter(base, 'memory.max', allow_max=True),
        'memory_current_bytes': _read_counter(base, 'memory.current'),
        'memory_high_bytes': _read_counter(base, 'memory.high', allow_max=True),
        'memory_swap_max_bytes': _read_counter(base, 'memory.swap.max', allow_max=True),
        'memory_peak_bytes': _read_counter(base, 'memory.peak'),
    }


def check_headroom(budget, limits):
    if not isinstance(budget, ResourceBudget):
        raise ValueError('budget 必须是 ResourceBudget')
    if not isinstance(limits, dict):
        raise ValueError('limits 必须是 cgroup_limits() 返回的字典')
    required = budget.memory_max_bytes + budget.headroom_bytes
    maximum, current = limits.get('memory_max_bytes'), limits.get('memory_current_bytes')
    if type(maximum) is not int or type(current) is not int or current < 0:
        raise InsufficientMemoryError(
            'cgroup 未报告有限容量，无法证明内存余量；拒绝以未验证的内存余量启动阶段（需要 %d 字节）' % required,
            required_bytes=required, available_bytes=0)
    available = maximum - current
    message = ('宿主内存余量不足：可用 %d 字节，需要 %d 字节（阶段上限 %d 字节 + 余量 %d 字节）；'
               '不自动降级，请先释放内存或调低预算' %
               (available, required, budget.memory_max_bytes, budget.headroom_bytes))
    if available < required:
        raise InsufficientMemoryError(message, required_bytes=required, available_bytes=available)
    return {**limits, 'available_bytes': available, 'required_bytes': required,
            'headroom_bytes': budget.headroom_bytes,
            'budget_memory_max_bytes': budget.memory_max_bytes, 'sufficient': True,
            'message': '宿主内存余量充足：可用 %d 字节，需要 %d 字节。' % (available, required)}


def check_ancestors(budget, target, *, root=Path('/sys/fs/cgroup')):
    """Check every finite ancestor of the destination user-service slice.

    Do not include the caller's MaiBot service leaf: a transient media service
    is a sibling, not its child. Unlimited ancestors may be skipped only when
    another ancestor provides a finite, measured capacity.
    """
    target, root = Path(target), Path(root)
    if not target.is_absolute() or not root.is_absolute() or not target.is_relative_to(root):
        raise CgroupUnavailable('目标 cgroup 必须在指定挂载点内')
    reports = []
    current = target
    while True:
        # Only memory.max/current are needed for admission. Optional telemetry
        # (memory.peak) is not available on every supported kernel.
        if not current.is_dir() or current.is_symlink():
            raise CgroupUnavailable('目标 cgroup 祖先不存在或不安全：%s' % current)
        limits = {'path': str(current),
                  'memory_max_bytes': _read_counter(current, 'memory.max', allow_max=True),
                  'memory_current_bytes': _read_counter(current, 'memory.current')}
        if limits['memory_max_bytes'] is not None:
            reports.append(check_headroom(budget, limits))
        if current == root:
            break
        current = current.parent
    if not reports:
        raise CgroupUnavailable('未找到可验证的有限祖先 cgroup 容量')
    return reports


def systemd_properties(budget):
    if not isinstance(budget, ResourceBudget):
        raise ValueError('budget 必须是 ResourceBudget')
    properties = [('MemoryMax', budget.memory_max_bytes),
                  ('MemoryHigh', budget.memory_high_bytes), ('MemorySwapMax', 0),
                  ('CPUQuota', '%d%%' % budget.cpu_quota_percent),
                  ('TasksMax', budget.tasks_max), ('RuntimeMaxSec', budget.runtime_max_seconds)]
    return [part for name, value in properties for part in ('-p', '%s=%s' % (name, value))]


def describe(budget):
    if not isinstance(budget, ResourceBudget):
        raise ValueError('budget 必须是 ResourceBudget')
    return ('资源预算：阶段内存上限 %s（MemoryMax=%d 字节），软限 %s（MemoryHigh=%d 字节），'
            'swap 上限 0，CPU 配额 %d%%，任务数上限 %d，宿主剩余内存余量要求 %s，'
            '单服务运行上限 %d 秒' %
            (_human_bytes(budget.memory_max_bytes), budget.memory_max_bytes,
             _human_bytes(budget.memory_high_bytes), budget.memory_high_bytes,
             budget.cpu_quota_percent, budget.tasks_max, _human_bytes(budget.headroom_bytes),
             budget.runtime_max_seconds))


def field_names():
    return tuple(field.name for field in fields(ResourceBudget))
