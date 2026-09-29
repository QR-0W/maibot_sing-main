"""Dedicated stage entry point, called only inside the bounded stage supervisor.

Unlike legacy worker CLI this needs no musicdl runtime or source-search inputs.
The supervisor owns the inference lock for the entire child process lifetime.
"""
import argparse
from pathlib import Path
from worker import verify_limits, separate, mix


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',choices=['separate','mix'])
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--demucs-repo',type=Path)
    parser.add_argument('--instrumental',action='store_true')
    args=parser.parse_args()
    if args.stage=='separate' and args.demucs_repo is None:
        parser.error('separate requires explicit --demucs-repo; default cache is forbidden')
    verify_limits()
    {'separate':separate,'mix':mix}[args.stage](args)


if __name__=='__main__':
    main()
