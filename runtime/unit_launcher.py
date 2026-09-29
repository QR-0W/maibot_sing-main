"""Lightweight launch witness; never loads models or owns the ML lock.

Retain the inherited scheduler flock while systemd-run submits/waits. Persist a
write-once exit witness before closing that descriptor, even if the host died.
A missing witness is uncertainty, not permission to re-run an absent service.
"""
import argparse
import json
import os
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
    outcome={'schema':1,'completed':False,'returncode':None}
    try:
        proc=subprocess.run(command,timeout=args.timeout,check=False,close_fds=True)
        outcome.update(completed=True,returncode=proc.returncode)
    except (OSError,subprocess.TimeoutExpired):
        # Unit may have started despite submission/wait failure. Remain unknown.
        pass
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
    return 0 if outcome['completed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
