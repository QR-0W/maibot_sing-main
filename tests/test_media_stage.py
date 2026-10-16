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
# 前置护栏的两条拒绝文案：被测进程不在 systemd 单元内，或单元缺少冻结预算字段。
# 验证本身就跑在受限 systemd 单元里，因此只会命中后者；两者都必须在加载任何
# 模型或读取媒体之前发生。
GUARD_MESSAGES = ('必须在 systemd user service 内运行', '媒体单元缺少有效预算字段')


def run(*options):
    if '--source-frames' not in options:
        options += ('--source-frames',str(30*RATE),'--context-start-frame','0',
                    '--context-end-frame',str(30*RATE))
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
    assert any(message in explicit.stderr for message in GUARD_MESSAGES)


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
    validate=stage.validate_timeline
    def timeline(args):
        calls.append('timeline')
        return validate(args)
    monkeypatch.setattr(stage,'validate_timeline',timeline)
    monkeypatch.setattr(stage, handler, lambda args: calls.append(
        (args.stage, args.render_mode, args.instrumental)))
    argv = [str(STAGE), command, '--scratch', '/offline/job', '--model', '/offline/model',
            '--index', '/offline/index', '--demucs-repo', '/offline/repo',
            '--source-frames', str(30*RATE), '--context-start-frame', '0',
            '--context-end-frame', str(30*RATE)]
    if render_mode is not None:
        argv += ['--render-mode', render_mode]
    if instrumental:
        argv += ['--instrumental']
    monkeypatch.setattr(sys, 'argv', argv)
    stage.main()
    assert calls == ['limits', 'timeline', (command, render_mode or 'full', instrumental)]


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
    assert any(message in result.stderr for message in GUARD_MESSAGES)


@pytest.mark.parametrize('render_mode', ['full', 'excerpt'])
@pytest.mark.parametrize('command', ['separate', 'mix'])
def test_limits_failure_prevents_any_handler(stage, monkeypatch, render_mode, command):
    def denied():
        raise RuntimeError('limits denied')
    def unexpected(args):
        pytest.fail('stage handler ran before the resource guard')
    monkeypatch.setattr(stage, 'verify_limits', denied)
    monkeypatch.setattr(stage,'validate_timeline',unexpected)
    for handler in ('separate', 'select_excerpt', 'mix_full', 'mix_excerpt'):
        monkeypatch.setattr(stage, handler, unexpected)
    monkeypatch.setattr(sys, 'argv', [str(STAGE), command, '--scratch', '/offline/job',
        '--model', '/offline/model', '--index', '/offline/index',
        '--demucs-repo', '/offline/repo', '--render-mode', render_mode,
        '--source-frames', str(30*RATE), '--context-start-frame', '0',
        '--context-end-frame', str(30*RATE)])
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
    with pytest.raises(ValueError, match='冻结上下文'):
        stage.separate(SimpleNamespace(scratch=tmp_path, demucs_repo=repo,
            render_mode=render_mode,source_frames=30*RATE,context_start_frame=0,
            context_end_frame=30*RATE))


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
            render_mode=render_mode,source_frames=30*RATE,context_start_frame=0,
            context_end_frame=30*RATE))


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


def timeline_args(tmp_path,mode='excerpt',source_frames=900*RATE):
    from context_window import freeze_context, full_context
    window=(freeze_context if mode=='excerpt' else full_context)(source_frames)
    return SimpleNamespace(scratch=tmp_path,render_mode=mode,source_frames=source_frames,
        context_start_frame=window['start_frame'],context_end_frame=window['end_frame'],
        demucs_repo=tmp_path/'repo',instrumental=False)


@pytest.mark.parametrize('mode,frames',[
    ('full',30*RATE),('full',300*RATE),('excerpt',30*RATE),
    ('excerpt',90*RATE+1),('excerpt',900*RATE-1),('excerpt',900*RATE)])
def test_validate_timeline_accepts_only_the_exact_policy(stage,tmp_path,mode,frames):
    args=timeline_args(tmp_path,mode,frames)
    window=stage.validate_timeline(args)
    assert window==dict(start_frame=args.context_start_frame,end_frame=args.context_end_frame)
    assert window['end_frame']-window['start_frame']==(frames if mode=='full' else min(frames,90*RATE))


@pytest.mark.parametrize('mode,frames',[
    ('full',300*RATE+1),('full',900*RATE),('excerpt',900*RATE+1),
    ('full',True),('excerpt',True),('full',30*RATE-1),('excerpt',30*RATE-1),
    ('full',30*RATE+.5),('excerpt',30*RATE+.5)])
def test_invalid_timeline_never_reaches_audio_or_models(stage,monkeypatch,tmp_path,mode,frames):
    args=timeline_args(tmp_path,mode,30*RATE)
    args.source_frames=frames;args.context_end_frame=frames
    monkeypatch.setattr(sf,'info',lambda *a,**k:pytest.fail('invalid timeline reached media metadata'))
    monkeypatch.setitem(sys.modules,'torch',None)
    with pytest.raises(ValueError):stage.validate_timeline(args)
    with pytest.raises(ValueError):stage.separate(args)


@pytest.mark.parametrize('field,value',[
    ('context_start_frame',True),('context_start_frame',0),
    ('context_end_frame',900*RATE),('context_end_frame',float(495*RATE)),
    ('render_mode',None),('render_mode','FULL')])
def test_rejects_nonfrozen_or_wrong_type_window(stage,tmp_path,field,value):
    args=timeline_args(tmp_path);setattr(args,field,value)
    with pytest.raises(ValueError):stage.validate_timeline(args)


@pytest.mark.parametrize('command',['separate','excerpt','mix'])
def test_main_checks_timeline_after_limits_before_dispatch(stage,monkeypatch,command):
    calls=[]
    monkeypatch.setattr(stage,'verify_limits',lambda:calls.append('limits'))
    for name in ('separate','select_excerpt','mix_full','mix_excerpt'):
        monkeypatch.setattr(stage,name,lambda args:pytest.fail('forged timeline reached handler'))
    monkeypatch.setattr(sys,'argv',[str(STAGE),command,'--scratch','/offline/job',
        '--model','/offline/model','--index','/offline/index','--demucs-repo','/offline/repo',
        '--render-mode','excerpt','--source-frames',str(900*RATE),
        '--context-start-frame','0','--context-end-frame',str(90*RATE)])
    with pytest.raises(ValueError):stage.main()
    assert calls==['limits']


@pytest.mark.parametrize('mode,source_frames,actual_frames',[
    ('full',30*RATE,30*RATE+1),('full',300*RATE,300*RATE-1),
    ('excerpt',900*RATE,90*RATE+1),('excerpt',900*RATE-1,90*RATE-1)])
def test_actual_metadata_must_match_frozen_window_before_pcm_read(
        stage,monkeypatch,tmp_path,mode,source_frames,actual_frames):
    args=timeline_args(tmp_path,mode,source_frames);args.demucs_repo.mkdir()
    monkeypatch.setattr(sf,'info',lambda *a,**k:SimpleNamespace(
        frames=actual_frames,samplerate=RATE,channels=2))
    monkeypatch.setattr(sf,'read',lambda *a,**k:pytest.fail('mismatch must fail before PCM allocation'))
    monkeypatch.setitem(sys.modules,'torch',None)
    with pytest.raises(ValueError,match='冻结上下文'):stage.separate(args)


@pytest.mark.parametrize('source_frames',[300*RATE+1,900*RATE-1,900*RATE])
def test_fake_long_source_only_separates_one_ninety_second_context(
        stage,fake_engine,tmp_path,source_frames):
    args=timeline_args(tmp_path,source_frames=source_frames);args.demucs_repo.mkdir()
    write_synthetic_source(tmp_path/'original.wav',90*RATE)
    stage.separate(args)
    assert fake_engine[1][0:2]==('apply-stub',(1,2,90*RATE))
    assert sf.info(tmp_path/'vocals.wav').frames==90*RATE
    assert sf.info(tmp_path/'backing.wav').frames==90*RATE
    assert not list(tmp_path.glob('vocal_???.wav'))


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
    mode = render_mode or 'full'
    context_frames = frames if mode=='full' else min(frames,90*RATE)
    write_synthetic_source(tmp_path/'original.wav', context_frames)
    options = {} if render_mode is None else {'render_mode': render_mode}
    steps = plan_module.build_plan(tmp_path, Path(sys.executable), STAGE, '/offline/rvc.py',
        '/offline/model', '/offline/index', '/offline/hubert', frames,
        demucs_repo=repo, **options)
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
    assert fake_engine[1][0:2] == ('apply-stub', (1, 2, context_frames))
    assert fake_engine[1][2] == dict(device='cpu', shifts=0, split=True, segment=5,
                                    overlap=.25, num_workers=0, progress=False)
    assert len(fake_engine) == 2
    assert sf.info(tmp_path/'vocals.wav').frames == sf.info(tmp_path/'backing.wav').frames == context_frames
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
