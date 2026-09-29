"""No torch/model inference: exercise dedicated media CLI argument boundary."""
from pathlib import Path
import ast
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'runtime' / 'media_stage.py'
WORKER = ROOT / 'runtime' / 'worker.py'


def run(*options):
    return subprocess.run([sys.executable, str(STAGE), *options],
                          capture_output=True, text=True, timeout=5, check=False)


def test_durable_separation_refuses_implicit_remote_or_default_repo():
    args = ('separate', '--scratch', '/offline/job', '--model', '/offline/model',
            '--index', '/offline/index')
    denied = run(*args)
    assert denied.returncode != 0
    assert 'separate requires explicit --demucs-repo' in denied.stderr
    # With an explicit repo the CLI parses and reaches the service sandbox
    # guard; no torch is imported and no attempt is made to open a checkpoint.
    explicit = run(*args, '--demucs-repo', '/offline/htdemucs')
    assert explicit.returncode != 0
    assert 'the following arguments are required' not in explicit.stderr
    assert '必须在 systemd user service 内运行' in explicit.stderr


def test_worker_demucs_lookup_carries_repo_argument():
    tree = ast.parse(WORKER.read_text(encoding='utf-8'))
    separate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'separate')
    lookups = [node for node in ast.walk(separate)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'get_model']
    assert len(lookups) == 1
    assert [(keyword.arg, keyword.value.id) for keyword in lookups[0].keywords] == [('repo', 'repo')]
