"""Budget validation, cgroup parsing and pre-launch refusal stay exact and cheap."""
from pathlib import Path
import importlib
import importlib.util
import os
import sys

import pytest

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('budget_test_pkg',root/'__init__.py',submodule_search_locations=[str(root)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
module=importlib.import_module('budget_test_pkg.services.resource_budget')

MIB=module.BYTES_PER_MIB
GIB=module.BYTES_PER_GIB
MAXIMUM=6*GIB
DEFAULT_BUDGET={'memory_max_bytes':4*GIB,'memory_high_bytes':3*GIB,'swap_max_bytes':0,
                'cpu_quota_percent':150,'tasks_max':64,'headroom_bytes':512*MIB,
                'runtime_max_seconds':1200}


def budget(**overrides):
    return module.ResourceBudget(**{**DEFAULT_BUDGET,**overrides})


def fake_cgroup(tmp_path,*,name='cgroup',maximum='8589934592',current='1048576',
                high='6442450944',swap='0',peak='2097152'):
    """Write a synthetic cgroup v2 memory tree; None omits a file entirely."""
    folder=tmp_path/name;folder.mkdir(parents=True,exist_ok=True)
    for name,value in (('memory.max',maximum),('memory.current',current),('memory.high',high),
                       ('memory.swap.max',swap),('memory.peak',peak)):
        if value is None:
            continue
        (folder/name).write_text(value+'\n' if value else value,encoding='utf-8')
    return folder


def test_valid_budget_round_trips_every_field_exactly():
    value=budget()
    assert (value.memory_max_bytes,value.memory_high_bytes,value.swap_max_bytes)==(4*GIB,3*GIB,0)
    assert (value.cpu_quota_percent,value.tasks_max)==(150,64)
    assert (value.headroom_bytes,value.runtime_max_seconds)==(512*MIB,1200)
    assert module.field_names()==('memory_max_bytes','memory_high_bytes','cpu_quota_percent',
        'tasks_max','headroom_bytes','runtime_max_seconds','swap_max_bytes')
    with pytest.raises(ValueError, match='swap_max_bytes'):
        budget(swap_max_bytes=1*GIB)
    assert module.ResourceBudget(memory_max_bytes=512*MIB,memory_high_bytes=256*MIB,
        cpu_quota_percent=25,tasks_max=8,headroom_bytes=256*MIB,
        runtime_max_seconds=30).swap_max_bytes==0
    assert module.ResourceBudget(memory_max_bytes=MAXIMUM,memory_high_bytes=MAXIMUM-1,
        cpu_quota_percent=400,tasks_max=512,headroom_bytes=MAXIMUM,
        runtime_max_seconds=7200).memory_max_bytes==MAXIMUM


def test_budget_is_immutable_and_slots_has_no_extra_state():
    value=budget()
    with pytest.raises(Exception):
        value.memory_max_bytes=8*GIB
    assert not hasattr(value,'__dict__')
    assert value==budget() and value!=budget(tasks_max=32)
    assert hash(value)==hash(budget())


@pytest.mark.parametrize('field_name,value',[
    ('memory_max_bytes',True),('memory_max_bytes',False),('memory_max_bytes',4294967296.0),
    ('memory_max_bytes','4294967296'),('memory_max_bytes',None),('memory_max_bytes',[]),
    ('memory_high_bytes',True),('memory_high_bytes',3221225472.0),('memory_high_bytes',None),
    ('swap_max_bytes',True),('swap_max_bytes',0.0),('swap_max_bytes',None),('swap_max_bytes','0'),
    ('cpu_quota_percent',True),('cpu_quota_percent',150.0),('cpu_quota_percent',None),
    ('tasks_max',True),('tasks_max',64.0),('tasks_max',None),
    ('headroom_bytes',True),('headroom_bytes',536870912.0),('headroom_bytes',None),
    ('runtime_max_seconds',True),('runtime_max_seconds',1200.0),('runtime_max_seconds',None),
])
def test_every_non_int_field_value_is_refused(field_name,value):
    with pytest.raises(ValueError,match=field_name):
        budget(**{field_name:value})


@pytest.mark.parametrize('field_name,value',[
    ('memory_max_bytes',268435455),('memory_max_bytes',MAXIMUM+1),('memory_max_bytes',8*GIB),
    ('memory_max_bytes',-4294967296),('memory_max_bytes',0),
    ('memory_high_bytes',268435455),('memory_high_bytes',MAXIMUM+1),('memory_high_bytes',-1),
    ('swap_max_bytes',-1),('swap_max_bytes',MAXIMUM+1),
    ('cpu_quota_percent',24),('cpu_quota_percent',401),('cpu_quota_percent',0),
    ('tasks_max',7),('tasks_max',513),('tasks_max',-1),
    ('headroom_bytes',256*MIB-1),('headroom_bytes',-1),('headroom_bytes',MAXIMUM+1),
    ('runtime_max_seconds',29),('runtime_max_seconds',7201),('runtime_max_seconds',-1),
])
def test_every_out_of_range_field_value_is_refused(field_name,value):
    with pytest.raises(ValueError,match=field_name):
        budget(**{field_name:value})


@pytest.mark.parametrize('high',[4*GIB,4*GIB+1,MAXIMUM])
def test_memory_high_must_stay_below_memory_max(high):
    with pytest.raises(ValueError,match='memory_high_bytes 必须小于 memory_max_bytes'):
        budget(memory_high_bytes=high)


def test_capacity_is_measured_not_assumed_eight_gib(tmp_path):
    value = module.ResourceBudget.from_config()
    small = module.cgroup_limits(fake_cgroup(tmp_path, maximum=str(2*GIB), current='0'))
    with pytest.raises(module.InsufficientMemoryError):
        module.check_headroom(value, small)
    large = module.cgroup_limits(fake_cgroup(tmp_path, name='large',
                                             maximum=str(16*GIB), current='0'))
    assert module.check_headroom(value, large)['available_bytes'] == 16*GIB
    with pytest.raises(ValueError, match='memory_max_bytes'):
        budget(memory_max_bytes=MAXIMUM+1)


def test_error_messages_are_readable_chinese_with_numbers():
    with pytest.raises(ValueError) as failure:
        budget(cpu_quota_percent=401)
    text=str(failure.value)
    assert 'cpu_quota_percent' in text and '401' in text and '400' in text
    assert any('\u4e00'<=character<='\u9fff' for character in text)
    with pytest.raises(ValueError) as failure:
        budget(memory_max_bytes=True)
    assert 'int' in str(failure.value) and 'bool' in str(failure.value)


def test_cgroup_limits_parses_exact_ints_and_max_keyword(tmp_path):
    folder=fake_cgroup(tmp_path,high='3221225472',swap='max',peak='3221225472')
    limits=module.cgroup_limits(str(folder))
    assert limits['memory_max_bytes']==8589934592
    assert limits['memory_current_bytes']==1048576
    assert limits['memory_high_bytes']==3221225472
    assert limits['memory_swap_max_bytes'] is None
    assert limits['memory_peak_bytes']==3221225472
    assert limits['path']==str(folder)
    assert all(type(limits[key]) is int for key in
               ('memory_max_bytes','memory_current_bytes','memory_high_bytes','memory_peak_bytes'))
    unbounded=module.cgroup_limits(fake_cgroup(tmp_path,maximum='max',high='max'))
    assert unbounded['memory_max_bytes'] is None and unbounded['memory_high_bytes'] is None
    assert module.cgroup_limits(fake_cgroup(tmp_path,maximum='0',current='0'))['memory_max_bytes']==0
    assert module.cgroup_limits(str(fake_cgroup(tmp_path,maximum=' 8589934592 ')))['memory_max_bytes']==8589934592
    assert module.cgroup_limits(fake_cgroup(tmp_path,maximum='08589934592'))['memory_max_bytes']==8589934592


@pytest.mark.parametrize('maximum',['','\n','maxx','infinity','-1','-0','1.5','8589934592 0',
                                    '0x20','8G','8589934592 bytes','8589934592\n0','+8589934592'])
def test_invalid_cgroup_content_is_refused_not_guessed(tmp_path,maximum):
    with pytest.raises(module.CgroupUnavailable) as failure:
        module.cgroup_limits(fake_cgroup(tmp_path,maximum=maximum))
    assert 'memory.max' in str(failure.value)
    assert failure.value.code=='cgroup_unavailable'


def test_missing_cgroup_file_or_directory_is_refused(tmp_path):
    with pytest.raises(module.CgroupUnavailable) as failure:
        module.cgroup_limits(str(fake_cgroup(tmp_path,name='without-current',current=None)))
    assert 'memory.current' in str(failure.value) and failure.value.code=='cgroup_unavailable'
    with pytest.raises(module.CgroupUnavailable) as failure:
        module.cgroup_limits(tmp_path/'absent')
    assert 'absent' in str(failure.value)
    with pytest.raises(module.CgroupUnavailable) as failure:
        module.cgroup_limits(str(fake_cgroup(tmp_path,name='without-high',high=None)))
    assert 'memory.high' in str(failure.value)
    with pytest.raises(module.CgroupUnavailable) as failure:
        module.cgroup_limits(str(fake_cgroup(tmp_path,name='without-peak',peak=None)))
    assert 'memory.peak' in str(failure.value)
    with pytest.raises(ValueError,match='cgroup 路径'):
        module.cgroup_limits(8589934592)
    with pytest.raises(ValueError,match='cgroup 路径'):
        module.cgroup_limits('')
    with pytest.raises(module.CgroupUnavailable,match='符号链接'):
        real=fake_cgroup(tmp_path,name='symlink-source')
        linked=tmp_path/'linked';linked.mkdir()
        for name in ('memory.max','memory.current','memory.high','memory.swap.max','memory.peak'):
            os.symlink(real/name,linked/name)
        module.cgroup_limits(linked)


def test_check_headroom_reports_diagnostics_when_sufficient(tmp_path):
    limits=module.cgroup_limits(fake_cgroup(tmp_path,current=str(3*GIB)))
    report=module.check_headroom(budget(),limits)
    assert report['available_bytes']==8589934592-3*GIB
    assert report['required_bytes']==4*GIB+512*MIB
    assert report['sufficient'] is True and report['headroom_bytes']==512*MIB
    assert report['budget_memory_max_bytes']==4*GIB and report['memory_current_bytes']==3*GIB
    assert report['path']==str(tmp_path/'cgroup')
    assert '内存余量充足' in report['message'] and str(report['available_bytes']) in report['message']


def test_check_headroom_refuses_insufficient_memory_with_numbers(tmp_path):
    limits=module.cgroup_limits(fake_cgroup(tmp_path,current=str(8589934592-(4*GIB+512*MIB-1))))
    with pytest.raises(module.InsufficientMemoryError) as failure:
        module.check_headroom(budget(),limits)
    error=failure.value
    assert error.code=='insufficient_memory'
    assert error.required_bytes==4*GIB+512*MIB
    assert error.available_bytes==4*GIB+512*MIB-1
    message=str(error)
    assert str(error.required_bytes) in message and str(error.available_bytes) in message
    assert '不自动降级' in message
    assert budget().memory_max_bytes==4*GIB  # the budget itself is never shrunk


def test_check_headroom_refuses_unbounded_or_unknown_host_memory(tmp_path):
    limits=module.cgroup_limits(fake_cgroup(tmp_path,maximum='max'))
    with pytest.raises(module.InsufficientMemoryError) as failure:
        module.check_headroom(budget(),limits)
    error=failure.value
    assert error.code=='insufficient_memory'
    assert error.required_bytes==4*GIB+512*MIB
    assert error.available_bytes==0  # unbounded cgroup: no usable number to report
    assert '无法证明' in str(error)
    assert '拒绝以未验证的内存余量启动阶段' in str(error)
    assert str(error.required_bytes) in str(error)
    with pytest.raises(ValueError,match='limits'):
        module.check_headroom(budget(),None)
    with pytest.raises(ValueError,match='budget'):
        module.check_headroom(DEFAULT_BUDGET,limits)


def test_systemd_properties_order_and_exact_values():
    value=budget()
    assert value.systemd_properties()==[
        '-p','MemoryMax=4294967296','-p','MemoryHigh=3221225472','-p','MemorySwapMax=0',
        '-p','CPUQuota=150%','-p','TasksMax=64','-p','RuntimeMaxSec=1200']
    assert module.systemd_properties(value)==value.systemd_properties()
    other=module.ResourceBudget(memory_max_bytes=MAXIMUM,memory_high_bytes=MAXIMUM-1,
        swap_max_bytes=0,cpu_quota_percent=250,tasks_max=128,headroom_bytes=1024*MIB,
        runtime_max_seconds=90)
    assert other.systemd_properties()==[
        '-p','MemoryMax=6442450944','-p','MemoryHigh=6442450943','-p','MemorySwapMax=0',
        '-p','CPUQuota=250%','-p','TasksMax=128','-p','RuntimeMaxSec=90']
    assert len(other.systemd_properties())==12
    assert all(other.systemd_properties()[index]=='-p' for index in range(0,12,2))
    with pytest.raises(ValueError,match='budget'):
        module.systemd_properties(DEFAULT_BUDGET)


def test_from_config_maps_mib_and_applies_identical_validation():
    value=module.ResourceBudget.from_config(memory_max_mb=3072,cpu_quota_percent=100,
                                            tasks_max=48,runtime_max_seconds=600,headroom_mb=768)
    assert value.memory_max_bytes==3072*MIB and value.memory_high_bytes==2304*MIB
    assert value.headroom_bytes==768*MIB and value.swap_max_bytes==0
    assert (value.cpu_quota_percent,value.tasks_max,value.runtime_max_seconds)==(100,48,600)
    default=module.ResourceBudget.from_config()
    assert (default.memory_max_bytes,default.memory_high_bytes)==(2*GIB,1536*MIB)
    assert default.systemd_properties()==budget(
        memory_max_bytes=2*GIB, memory_high_bytes=1536*MIB).systemd_properties()
    assert module.ResourceBudget.from_config(memory_max_mb=512,headroom_mb=256,
        cpu_quota_percent=25,tasks_max=8,runtime_max_seconds=30).memory_high_bytes==384*MIB
    assert module.ResourceBudget.from_config(memory_max_mb=6144,headroom_mb=6144,
        cpu_quota_percent=400,tasks_max=512,runtime_max_seconds=7200).memory_max_bytes==MAXIMUM
    for keyword,value_in in (('memory_max_mb',6145),('memory_max_mb',511),('memory_max_mb',4096.0),
                             ('memory_max_mb',True),('memory_max_mb',None),('headroom_mb',255),
                             ('headroom_mb',6145),('headroom_mb',False),('cpu_quota_percent',24),
                             ('cpu_quota_percent',401),('cpu_quota_percent',150.0),
                             ('tasks_max',7),('tasks_max',513),('tasks_max',64.0),
                             ('runtime_max_seconds',29),('runtime_max_seconds',7201),
                             ('runtime_max_seconds',1200.0)):
        with pytest.raises(ValueError,match=keyword):
            module.ResourceBudget.from_config(**{keyword:value_in})


def test_describe_is_chinese_and_consistent_with_the_budget():
    text=module.describe(budget())
    assert '4 GiB' in text and '4294967296' in text and '3 GiB' in text
    assert '150%' in text and '64' in text and '1200' in text and '512 MiB' in text
    assert 'swap' in text and '余量' in text
    assert any('\u4e00'<=character<='\u9fff' for character in text)
    assert text==budget().describe() and '资源预算' in text
    assert module.describe(module.ResourceBudget.from_config(memory_max_mb=4096))==text
    assert module.describe(module.ResourceBudget.from_config())!=text
    with pytest.raises(ValueError,match='budget'):
        module.describe(None)


def test_module_stays_stdlib_only_and_run_never_imports_heavy_libraries():
    # Importing the module must not pull in heavy libraries. Other test modules
    # in a full-suite run may already have imported numpy/torch, so compare the
    # module set before and after a fresh import instead of assuming a clean run.
    before=set(sys.modules)
    fresh_spec=importlib.util.spec_from_file_location(
        'resource_budget_fresh',root/'services'/'resource_budget.py')
    fresh=importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    assert not {name for name in set(sys.modules)-before
                if name.split('.')[0] in ('torch','numpy','systemd')}
    source=(root/'services'/'resource_budget.py').read_text(encoding='utf-8')
    imports=[line.strip() for line in source.splitlines()
             if line.startswith(('import ','from '))]
    assert sorted(imports)==['from dataclasses import dataclass, fields',
                             'from pathlib import Path','from typing import Optional','import re']
    for forbidden in ('torch','numpy','systemd','subprocess','socket','urllib','requests'):
        assert not any(forbidden in line for line in imports)
    assert module.MIN_MEMORY_MAX_BYTES==512*MIB and module.MIN_HEADROOM_BYTES==256*MIB
    assert module.MIN_CPU_QUOTA_PERCENT==25 and module.MAX_CPU_QUOTA_PERCENT==400
    assert module.MIN_TASKS_MAX==8 and module.MAX_TASKS_MAX==512
    assert (module.MIN_RUNTIME_MAX_SECONDS,module.MAX_RUNTIME_MAX_SECONDS)==(30,7200)


def test_effective_ancestors_include_finite_root_and_skip_unlimited_slice(tmp_path):
    root_cg = fake_cgroup(tmp_path, name='mount', current=str(1*GIB))
    user = fake_cgroup(root_cg, name='user.slice', maximum='max', current=str(1*GIB))
    target = fake_cgroup(user, name='app.slice', maximum='max', current='0', peak=None)
    reports = module.check_ancestors(module.ResourceBudget.from_config(), target, root=root_cg)
    assert len(reports) == 1 and reports[0]['path'] == str(root_cg)


def test_effective_ancestor_refuses_even_when_container_has_room(tmp_path):
    root_cg = fake_cgroup(tmp_path, name='mount', current='0')
    target = fake_cgroup(root_cg, name='app.slice', maximum=str(3*GIB), current=str(1*GIB))
    with pytest.raises(module.InsufficientMemoryError) as exc:
        module.check_ancestors(module.ResourceBudget.from_config(), target, root=root_cg)
    assert exc.value.available_bytes == 2*GIB


def test_effective_ancestors_do_not_require_optional_peak(tmp_path):
    root_cg = fake_cgroup(tmp_path, name='mount', current='0', peak=None)
    assert module.check_ancestors(module.ResourceBudget.from_config(), root_cg, root=root_cg)


def test_effective_ancestors_refuse_unknown_capacity_or_path_escape(tmp_path):
    root_cg = fake_cgroup(tmp_path, name='mount', maximum='max')
    with pytest.raises(module.CgroupUnavailable, match='有限'):
        module.check_ancestors(module.ResourceBudget.from_config(), root_cg, root=root_cg)
    with pytest.raises(module.CgroupUnavailable):
        module.check_ancestors(module.ResourceBudget.from_config(), tmp_path, root=root_cg)
