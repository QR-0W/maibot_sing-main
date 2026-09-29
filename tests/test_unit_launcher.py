"""Launch witness captures systemd state before resetting a unique failed unit."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import types


MODULE=Path(__file__).resolve().parents[1]/'runtime/unit_launcher.py'
spec=importlib.util.spec_from_file_location('launcher_test',MODULE)
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


def test_failed_service_result_is_fsynced_before_reset(tmp_path,monkeypatch):
    lock=os.open(tmp_path/'lock',os.O_CREAT|os.O_RDWR,0o600)
    result=tmp_path/'result.json'
    unit='maibot-sing-'+('a'*32)+'-convert_000'
    calls=[]
    def run(argv,**kwargs):
        calls.append(tuple(argv))
        if argv[0]=='systemd-run':
            assert '--collect' not in argv
            return types.SimpleNamespace(returncode=1,stdout=b'')
        if argv[0:3]==['systemctl','--user','show']:
            raw=(b'LoadState=loaded\nActiveState=failed\nResult=oom-kill\n'
                 b'ExecMainCode=killed\nExecMainStatus=9\n')
            return types.SimpleNamespace(returncode=0,stdout=raw)
        assert argv[0:3]==['systemctl','--user','reset-failed']
        assert json.loads(result.read_text())['service']['Result']=='oom-kill'
        return types.SimpleNamespace(returncode=0,stdout=b'')
    monkeypatch.setattr(launcher.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['unit_launcher.py','--lock-fd',str(lock),
        '--result',str(result),'--timeout','40','--','systemd-run','--user','--wait',
        '--unit',unit,'/bin/false'])
    try:
        assert launcher.main()==0
    finally:
        os.close(lock)
    witness=json.loads(result.read_text())
    assert witness=={'schema':2,'completed':True,'returncode':1,'service':{
        'LoadState':'loaded','ActiveState':'failed','Result':'oom-kill',
        'ExecMainCode':'killed','ExecMainStatus':'9'}}
    assert [call[0] for call in calls]==['systemd-run','systemctl','systemctl']


def test_failed_diagnosis_query_preserves_unit_for_reconciliation(tmp_path,monkeypatch):
    lock=os.open(tmp_path/'lock',os.O_CREAT|os.O_RDWR,0o600)
    result=tmp_path/'result.json';unit='maibot-sing-'+('c'*32)+'-encode'
    calls=[]
    def run(argv,**kwargs):
        calls.append(tuple(argv))
        if argv[0]=='systemd-run':
            return types.SimpleNamespace(returncode=1,stdout=b'')
        if argv[0:3]==['systemctl','--user','show']:
            return types.SimpleNamespace(returncode=1,stdout=b'')
        raise AssertionError('A failed diagnosis must not be erased by reset-failed')
    monkeypatch.setattr(launcher.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['unit_launcher.py','--lock-fd',str(lock),
        '--result',str(result),'--timeout','40','--','systemd-run','--user','--wait',
        '--unit',unit,'/bin/false'])
    try:
        assert launcher.main()==0
    finally:
        os.close(lock)
    assert json.loads(result.read_text())=={
        'schema':2,'completed':True,'returncode':1,'service':{}}
    assert [call[0] for call in calls]==['systemd-run','systemctl']


def test_unknown_launch_never_invents_a_service_result(tmp_path,monkeypatch):
    lock=os.open(tmp_path/'lock',os.O_CREAT|os.O_RDWR,0o600)
    result=tmp_path/'result.json';unit='maibot-sing-'+('b'*32)+'-encode'
    def run(argv,**kwargs):
        if argv[0]=='systemd-run':raise launcher.subprocess.TimeoutExpired(argv,40)
        raise AssertionError('Unknown launch must not query or reset a service')
    monkeypatch.setattr(launcher.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['unit_launcher.py','--lock-fd',str(lock),
        '--result',str(result),'--timeout','40','--','systemd-run','--user','--wait',
        '--unit',unit,'/bin/sleep','100'])
    try:
        assert launcher.main()==1
    finally:
        os.close(lock)
    assert json.loads(result.read_text())=={
        'schema':2,'completed':False,'returncode':None,'service':{}}
