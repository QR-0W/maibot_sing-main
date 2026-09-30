"""Synthetic audio and fake Demucs only: never import real torch or load a model."""
from pathlib import Path
import ast
import subprocess
import sys
import importlib.util
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'runtime' / 'media_stage.py'
WORKER = ROOT / 'runtime' / 'worker.py'
RATE = 44100
FULL_CASES = [(30,2),(40,2),(41,2),(44.9,2),(45,3),(237.923,12),(300,15)]


def run(*options):
    return subprocess.run([sys.executable, str(STAGE), *options],
                          capture_output=True, text=True, timeout=5, check=False)


@pytest.mark.parametrize('render_mode', [None, 'full', 'excerpt'])
def test_durable_separation_refuses_implicit_remote_or_default_repo(render_mode):
    args = ('separate', '--scratch', '/offline/job', '--model', '/offline/model',
            '--index', '/offline/index')
    if render_mode is not None:
        args += ('--render-mode', render_mode)
    denied = run(*args)
    assert denied.returncode != 0
    assert 'separate requires explicit --demucs-repo' in denied.stderr
    # An explicit repo reaches the service guard without loading any checkpoint.
    explicit = run(*args, '--demucs-repo', '/offline/htdemucs')
    assert explicit.returncode != 0
    assert 'the following arguments are required' not in explicit.stderr
    assert '必须在 systemd user service 内运行' in explicit.stderr


@pytest.mark.parametrize('path', [WORKER, STAGE])
def test_demucs_lookup_carries_repo_argument(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    separate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'separate')
    lookups = [node for node in ast.walk(separate)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'get_model']
    assert len(lookups) == 1
    assert [(keyword.arg, keyword.value.id) for keyword in lookups[0].keywords] == [('repo', 'repo')]


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'runtime'))
    spec = importlib.util.spec_from_file_location('media_stage_test', STAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plan_module(monkeypatch):
    spec = importlib.util.spec_from_file_location('media_plan_test', ROOT/'runtime/render_plan.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('render_mode,command,handler', [
    (None, 'separate', 'separate'), (None, 'mix', 'mix_full'),
    ('full', 'separate', 'separate'), ('full', 'mix', 'mix_full'),
    ('excerpt', 'separate', 'separate'), ('excerpt', 'excerpt', 'select_excerpt'),
    ('excerpt', 'mix', 'mix_excerpt')])
@pytest.mark.parametrize('instrumental', [False, True])
def test_stage_dispatch_verifies_limits_first(stage, monkeypatch, render_mode,
                                              command, handler, instrumental):
    calls = []
    monkeypatch.setattr(stage, 'verify_limits', lambda: calls.append('limits'))
    monkeypatch.setattr(stage, handler, lambda args: calls.append(
        (args.stage, args.render_mode, args.instrumental)))
    argv = [str(STAGE), command, '--scratch', '/offline/job', '--model', '/offline/model',
            '--index', '/offline/index', '--demucs-repo', '/offline/repo']
    if render_mode is not None:
        argv += ['--render-mode', render_mode]
    if instrumental:
        argv += ['--instrumental']
    monkeypatch.setattr(sys, 'argv', argv)
    stage.main()
    assert calls == ['limits', (command, render_mode or 'full', instrumental)]


@pytest.mark.parametrize('render_mode', [None, 'full'])
def test_excerpt_action_rejected_outside_excerpt_mode(stage, monkeypatch, render_mode):
    calls = []
    monkeypatch.setattr(stage, 'verify_limits', lambda: calls.append('limits'))
    monkeypatch.setattr(stage, 'select_excerpt', lambda args: calls.append('excerpt'))
    argv = [str(STAGE), 'excerpt', '--scratch', '/offline/job',
            '--model', '/offline/model', '--index', '/offline/index']
    if render_mode is not None:
        argv += ['--render-mode', render_mode]
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as denied:
        stage.main()
    assert denied.value.code == 2
    assert calls == []


@pytest.mark.parametrize('render_mode', ['FULL', 'Excerpt', 'dry', '', ' excerpt'])
def test_cli_mode_is_strict(render_mode):
    result = run('mix', '--scratch', '/offline/job', '--model', '/offline/model',
                 '--index', '/offline/index', '--render-mode', render_mode)
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr


def test_excerpt_cli_reaches_guard_without_loading_models():
    result = run('excerpt', '--scratch', '/offline/job', '--model', '/offline/model',
                 '--index', '/offline/index', '--render-mode', 'excerpt')
    assert result.returncode != 0
    assert 'invalid choice' not in result.stderr
    assert '必须在 systemd user service 内运行' in result.stderr


@pytest.mark.parametrize('render_mode', ['full', 'excerpt'])
@pytest.mark.parametrize('command', ['separate', 'mix'])
def test_limits_failure_prevents_any_handler(stage, monkeypatch, render_mode, command):
    def denied():
        raise RuntimeError('limits denied')
    def unexpected(args):
        pytest.fail('stage handler ran before the resource guard')
    monkeypatch.setattr(stage, 'verify_limits', denied)
    for handler in ('separate', 'select_excerpt', 'mix_full', 'mix_excerpt'):
        monkeypatch.setattr(stage, handler, unexpected)
    monkeypatch.setattr(sys, 'argv', [str(STAGE), command, '--scratch', '/offline/job',
        '--model', '/offline/model', '--index', '/offline/index',
        '--demucs-repo', '/offline/repo', '--render-mode', render_mode])
    with pytest.raises(RuntimeError, match='limits denied'):
        stage.main()


def test_full_mix_and_chunk_bounds_reuse_worker(stage):
    import worker
    assert stage.mix_full is worker.mix
    assert stage.chunk_bounds is worker.chunk_bounds


@pytest.mark.parametrize('render_mode', ['full', 'excerpt'])
def test_separate_rejects_short_source_before_model_import(stage, monkeypatch, tmp_path, render_mode):
    repo = tmp_path/'repo'
    repo.mkdir()
    sf.write(tmp_path/'original.wav', np.ones((100, 2)), RATE, subtype='FLOAT')
    monkeypatch.setitem(sys.modules, 'torch', None)
    with pytest.raises(ValueError, match='30–300'):
        stage.separate(SimpleNamespace(scratch=tmp_path, demucs_repo=repo,
                                       render_mode=render_mode))


@pytest.mark.parametrize('render_mode', ['full', 'excerpt'])
@pytest.mark.parametrize('bad_repo', ['relative', 'missing', 'symlink'])
def test_separate_rejects_invalid_repo_before_model_import(stage, monkeypatch, tmp_path,
                                                          render_mode, bad_repo):
    repo = tmp_path/'repo'
    if bad_repo == 'relative':
        repo = Path('relative/repo')
    elif bad_repo == 'symlink':
        actual = tmp_path/'actual'
        actual.mkdir()
        repo.symlink_to(actual, target_is_directory=True)
    monkeypatch.setitem(sys.modules, 'torch', None)
    with pytest.raises(ValueError, match='Demucs repo'):
        stage.separate(SimpleNamespace(scratch=tmp_path, demucs_repo=repo,
                                       render_mode=render_mode))


@pytest.fixture
def fake_engine(monkeypatch):
    calls = []

    class Tensor(np.ndarray):
        def numpy(self):
            return np.asarray(self)

    torch = ModuleType('torch')
    torch.from_numpy = lambda data: data.view(Tensor)
    torch.set_num_threads = lambda value: None
    torch.set_num_interop_threads = lambda value: None
    torch.inference_mode = nullcontext

    class Model:
        sources = ['vocals', 'other']
        def cpu(self):
            return self
        def eval(self):
            return self

    def get_model(name, *, repo):
        calls.append(('load-stub', name, repo))
        return Model()

    def apply_model(model, data, **options):
        calls.append(('apply-stub', data.shape, options))
        return np.stack((data*.5, data*.5), axis=1).view(Tensor)

    demucs = ModuleType('demucs')
    apply = ModuleType('demucs.apply')
    apply.apply_model = apply_model
    pretrained = ModuleType('demucs.pretrained')
    pretrained.get_model = get_model
    for name, module in [('torch', torch), ('demucs', demucs), ('demucs.apply', apply),
                         ('demucs.pretrained', pretrained)]:
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def write_synthetic_source(path, frames):
    # Bounded blocks avoid a long float64 temporary for the 300-second case.
    wave = (.1*np.sin(2*np.pi*100*np.arange(RATE)/RATE)).astype('float32')
    block = np.column_stack((wave, wave))
    with sf.SoundFile(path, 'w', samplerate=RATE, channels=2, subtype='FLOAT') as stream:
        for start in range(0, frames, RATE):
            stream.write(block[:min(RATE, frames-start)])


@pytest.mark.parametrize('seconds,count', FULL_CASES)
@pytest.mark.parametrize('render_mode', [None, 'full', 'excerpt'])
def test_fake_separation_conversion_and_mix_preserve_mode_timeline(
        stage, plan_module, fake_engine, monkeypatch, tmp_path, seconds, count, render_mode):
    frames = int(seconds*RATE)
    repo = tmp_path/'repo'
    repo.mkdir()
    write_synthetic_source(tmp_path/'original.wav', frames)
    options = {} if render_mode is None else {'render_mode': render_mode}
    steps = plan_module.build_plan(tmp_path, Path(sys.executable), STAGE, '/offline/rvc.py',
        '/offline/model', '/offline/index', '/offline/hubert', frames,
        demucs_repo=repo, **options)
    mode = render_mode or 'full'
    guarded = []
    monkeypatch.setattr(stage, 'verify_limits', lambda: guarded.append('limits'))

    def execute(step):
        before = {p.name for p in tmp_path.iterdir() if p.is_file()}
        monkeypatch.setattr(sys, 'argv', list(step.argv[1:]))
        stage.main()
        after = {p.name for p in tmp_path.iterdir() if p.is_file()}
        # Receipts must declare every new output, especially full RVC chunks.
        assert after-before == set(step.outputs)-before

    execute(steps[1])
    assert fake_engine[0] == ('load-stub', 'htdemucs', repo)
    assert fake_engine[1][0:2] == ('apply-stub', (1, 2, frames))
    assert fake_engine[1][2] == dict(device='cpu', shifts=0, split=True, segment=5,
                                    overlap=.25, num_workers=0, progress=False)
    assert len(fake_engine) == 2
    assert sf.info(tmp_path/'vocals.wav').frames == sf.info(tmp_path/'backing.wav').frames == frames
    assert sf.info(tmp_path/'vocals.wav').channels == 1
    assert sf.info(tmp_path/'backing.wav').channels == 2

    if mode == 'excerpt':
        assert {p.name for p in tmp_path.glob('*.wav')} == {'original.wav', 'vocals.wav', 'backing.wav'}
        assert not list(tmp_path.glob('vocal_???.wav'))
        execute(steps[2])
        import json
        selection = json.loads((tmp_path/'selection.json').read_text())
        assert selection['source_frames'] == frames
        output_frames = selection['output_frames']
        assert 12*RATE <= output_frames <= 18*RATE
        assert len(steps) == 7
        count = 1
    else:
        assert not (tmp_path/'selection.json').exists()
        assert not (tmp_path/'excerpt_backing.wav').exists()
        output_frames = frames
        bounds = stage.chunk_bounds(frames, RATE)
        assert len(bounds) == count
        assert bounds[0][0] == 0 and bounds[-1][1] == frames
        assert all(left[1] == right[0] for left, right in zip(bounds, bounds[1:]))
        assert sum(end-start for start, end in bounds) == frames
        assert all(5*RATE <= end-start < 25*RATE for start, end in bounds)
        assert all(end-start == 20*RATE for start, end in bounds[:-1])
        for number, (start, end) in enumerate(bounds):
            original, sr = sf.read(tmp_path/f'vocal_{number:03d}.wav', dtype='float32')
            reference, rr = sf.read(tmp_path/'vocals.wav', start=start, stop=end, dtype='float32')
            assert sr == rr == RATE
            assert len(original) == end-start
            np.testing.assert_array_equal(original, reference)
        assert len(steps) == count+5

    chunks = sorted(tmp_path.glob('vocal_???.wav'))
    conversion = [step for step in steps if step.name.startswith('convert_')]
    assert len(chunks) == len(conversion) == count
    assert sum(sf.info(chunk).frames for chunk in chunks) == output_frames
    for chunk, step in zip(chunks, conversion):
        assert step.inputs[-1] == chunk.name
        source, sr = sf.read(chunk, dtype='float32')
        # Fake conversion mimics RVC's 20 ms short output; the real mixers must
        # recover exact frames instead of losing 882 frames at every boundary.
        sf.write(tmp_path/step.outputs[0], source[:-882]*.8, sr, subtype='FLOAT')
    assert len(list(tmp_path.glob('vocal_???_converted.wav'))) == count

    for instrumental in (False, True):
        mix = plan_module.build_plan(tmp_path, Path(sys.executable), STAGE, '/offline/rvc.py',
            '/offline/model', '/offline/index', '/offline/hubert', frames,
            demucs_repo=repo, instrumental=instrumental, **options)[-3]
        execute(mix)
        info = sf.info(tmp_path/'mixed.wav')
        assert info.frames == output_frames
        assert info.samplerate == RATE
        assert info.channels == (2 if instrumental else 1)
        assert info.subtype == 'PCM_24'
        mixed, _ = sf.read(tmp_path/'mixed.wav', dtype='float32', always_2d=True)
        assert np.isfinite(mixed).all()
        assert 0 < float(np.max(np.abs(mixed))) <= .950001
        if not instrumental:
            dry = mixed
        else:
            assert not np.allclose(mixed[:, :1], dry)
            if mode == 'full':
                backing, _ = sf.read(tmp_path/'backing.wav', dtype='float32', always_2d=True)
                np.testing.assert_allclose(mixed-dry, backing, atol=3e-7, rtol=0)
    assert len(guarded) == (4 if mode == 'excerpt' else 3)
