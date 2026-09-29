"""Lightweight launch witness; never loads models or owns the ML lock.

Retain the inherited scheduler flock while systemd-run submits/waits. Persist a
write-once exit witness before closing that descriptor, even if the host died.
A missing witness is uncertainty, not permission to re-run an absent service.
"""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import uuid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--lock-fd',type=int,required=True)
    parser.add_argument('--result',required=True)
    parser.add_argument('--timeout',type=int,required=True)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command or command[0]!='systemd-run' or not 30<=args.timeout<=700:
        parser.error('Expected bounded systemd-run invocation')
    os.fstat(args.lock_fd)  # inherited descriptor must stay open until process exit
    result=Path(args.result)
    if not result.is_absolute() or any(p.is_symlink() for p in (result,*result.parents)):
        parser.error('Unsafe launch witness path')
    try:
        unit=command[command.index('--unit')+1]
    except (ValueError,IndexError):
        parser.error('Missing owned unit identity')
    if not re.fullmatch('maibot-sing-[0-9a-f]{32}-[a-z][a-z0-9_-]{0,63}',unit):
        parser.error('Invalid owned unit identity')
    outcome={'schema':2,'completed':False,'returncode':None,'service':{}}
    try:
        proc=subprocess.run(command,timeout=args.timeout,check=False,close_fds=True)
        outcome.update(completed=True,returncode=proc.returncode)
    except (OSError,subprocess.TimeoutExpired):
        # Unit may have started despite submission/wait failure. Remain unknown.
        pass
    if outcome['completed']:
        fields=('LoadState','ActiveState','Result','ExecMainCode','ExecMainStatus')
        try:
            query=subprocess.run(['systemctl','--user','show',unit+'.service','--no-pager',
                                  *[part for name in fields for part in ('-p',name)]],
                                 timeout=10,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False)
            if query.returncode==0 and len(query.stdout)<=4096:
                parsed=dict(line.split('=',1) for line in query.stdout.decode('utf-8','replace').splitlines() if '=' in line)
                outcome['service']={name:parsed[name] for name in fields if name in parsed
                                    and re.fullmatch('[a-zA-Z0-9_-]{0,80}',parsed[name])}
        except (OSError,subprocess.TimeoutExpired):
            pass  # Unknown result is retained as unknown, never invented OOM.
    temporary=result.with_name(result.name+'.'+uuid.uuid4().hex+'.part')
    with temporary.open('xb') as output:
        os.chmod(temporary,0o600)
        output.write(json.dumps(outcome,sort_keys=True).encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    os.link(temporary,result)
    temporary.unlink()
    fd=os.open(result.parent,os.O_DIRECTORY|os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # Failed transient units remain queryable until their diagnosis is durable.
    # If the query failed, preserve the unit for later forensic reconciliation.
    if outcome['completed'] and outcome['service'].get('Result'):
        try:
            subprocess.run(['systemctl','--user','reset-failed',unit+'.service'],timeout=10,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        except (OSError,subprocess.TimeoutExpired):
            pass
    return 0 if outcome['completed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
