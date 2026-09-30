import importlib.util
from pathlib import Path
import sys
import pytest

spec=importlib.util.spec_from_file_location('render_plan_test',Path(__file__).resolve().parents[1]/'runtime/render_plan.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)

RATE = 44100
FULL_CASES = [(30,2),(40,2),(41,2),(44.9,2),(45,3),(237.923,12),(300,15)]


def plan(seconds, **kwargs):
    return m.build_plan('/workspace','/python','/runtime/media_stage.py','/rvc.py','/model.pth','/index','/hubert.pt',int(seconds*RATE),demucs_repo='/private/offline/htdemucs', **kwargs)


def assert_safety_and_dependencies(steps, render_mode):
    assert all(s.unit_limit_s<900 for s in steps)
    for convert in (s for s in steps if s.name.startswith('convert_')):
        for option,value in {'--pitch':'0','--hubert':'/hubert.pt','--limit-seconds':'25',
                             '--f0-method':'harvest','--index-rate':'0.5',
                             '--filter-radius':'3','--rms-mix-rate':'0.25',
                             '--protect':'0.33','--seed':'20260928',
                             '--resample-sr':'44100'}.items():
            assert convert.argv[convert.argv.index(option)+1]==value
        assert convert.timeout_s==180
    separate=steps[1]
    assert separate.argv[separate.argv.index('--demucs-repo')+1]=='/private/offline/htdemucs'
    assert separate.timeout_s==600
    for step in steps:
        if step.name in ('separate','excerpt','mix'):
            assert step.argv[step.argv.index('--render-mode')+1]==render_mode
    # Preserve full decode and final validation argv, including their safety flags.
    assert steps[0].argv==('ffmpeg','-nostdin','-v','error','-xerror','-threads','1',
        '-i','/workspace/source.audio','-map','0:a:0','-ar','44100','-ac','2',
        '-c:a','pcm_f32le','/workspace/original.wav')
    assert steps[-1].argv==('ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
                           '/workspace/cover.mp3','-f','null','-')
    assert [s.timeout_s for s in steps[-3:]]==[60,90,60]
    assert m.total_compute_budget(steps)==sum(s.timeout_s+30 for s in steps)
    produced={'source.audio'}
    for step in steps:
        assert set(step.inputs)<=produced
        produced.update(step.outputs)


# Keep the original full-mode boundary regression; excerpt must be opt-in.
@pytest.mark.parametrize('seconds,count', FULL_CASES)
@pytest.mark.parametrize('instrumental', [False, True])
def test_finite_chunk_plan(seconds,count,instrumental):
    steps=plan(seconds,instrumental=instrumental)
    assert steps==plan(seconds,render_mode='full',instrumental=instrumental)
    chunks=tuple('vocal_%03d.wav'%n for n in range(count))
    converted=tuple('vocal_%03d_converted.wav'%n for n in range(count))
    conversion=[s for s in steps if s.name.startswith('convert_')]
    assert len(conversion)==count
    assert [s.name for s in steps]==['decode','separate',
        *('convert_%03d'%n for n in range(count)),'mix','encode','validate']
    assert steps[1].outputs==('vocals.wav','backing.wav',*chunks)
    assert steps[-3].inputs==('vocals.wav','backing.wav',*chunks,*converted)
    assert ('--instrumental' in steps[-3].argv)==instrumental
    for step,original,output in zip(conversion,chunks,converted):
        assert step.inputs==(original,)
        assert step.outputs==(output,)
        assert step.argv[step.argv.index('--input')+1]=='/workspace/'+original
        assert step.argv[step.argv.index('--output')+1]=='/workspace/'+output
    assert not any('selection.json' in s.inputs+s.outputs for s in steps)
    assert_safety_and_dependencies(steps,'full')


@pytest.mark.parametrize('seconds,count', FULL_CASES)
@pytest.mark.parametrize('instrumental', [False, True])
def test_static_single_excerpt_plan(seconds,count,instrumental):
    steps=plan(seconds,render_mode='excerpt',instrumental=instrumental)
    assert [s.name for s in steps]==['decode','separate','excerpt','convert_000','mix','encode','validate']
    conversion=[s for s in steps if s.name.startswith('convert_')]
    assert len(conversion)==1
    convert=conversion[0]
    assert convert.argv[convert.argv.index('--input')+1]=='/workspace/vocal_000.wav'
    assert convert.argv[convert.argv.index('--output')+1]=='/workspace/vocal_000_converted.wav'
    assert convert.inputs==('selection.json','vocal_000.wav')
    assert steps[1].outputs==('vocals.wav','backing.wav')
    excerpt=steps[2]
    assert excerpt.argv[2]=='excerpt'
    assert excerpt.inputs==('vocals.wav','backing.wav')
    assert excerpt.outputs==('selection.json','vocal_000.wav','excerpt_backing.wav')
    assert steps[4].inputs==('selection.json','excerpt_backing.wav','vocal_000.wav','vocal_000_converted.wav')
    assert ('--instrumental' in steps[4].argv)==instrumental
    assert not any('vocal_001' in item for step in steps for item in step.outputs)
    assert steps==plan(30,render_mode='excerpt',instrumental=instrumental)
    assert_safety_and_dependencies(steps,'excerpt')


@pytest.mark.parametrize('render_mode',['full','excerpt'])
@pytest.mark.parametrize('seconds',[0,29.9,300.1])
def test_reject_unbounded_inputs(seconds,render_mode):
    with pytest.raises(ValueError):plan(seconds,render_mode=render_mode)


@pytest.mark.parametrize('render_mode',['full','excerpt'])
def test_instrumental_only_changes_mix_flag(render_mode):
    plain=plan(30,render_mode=render_mode)
    backed=plan(30,render_mode=render_mode,instrumental=True)
    assert '--instrumental' not in plain[-3].argv
    assert backed[-3].argv==(*plain[-3].argv,'--instrumental')
    assert plain[:-3]+plain[-2:]==backed[:-3]+backed[-2:]


@pytest.mark.parametrize('render_mode',['full','excerpt'])
@pytest.mark.parametrize('options',[{'instrumental':1},{'instrumental':'false'},
                                    {'rate':48000}])
def test_reject_implicit_instrumental_or_nonstandard_rate(options,render_mode):
    with pytest.raises(ValueError):plan(30,render_mode=render_mode,**options)


@pytest.mark.parametrize('mode',[None,True,1,'','FULL','Excerpt',' full','excerpt ',
                                 'dry','backed',[],{}])
def test_render_mode_is_strict(mode):
    with pytest.raises(ValueError,match='Render mode'):
        plan(30,render_mode=mode)


@pytest.mark.parametrize('render_mode',['full','excerpt'])
@pytest.mark.parametrize('frames',[True,30*RATE+.5,30*RATE-1,300*RATE+1])
def test_frames_are_exact_bounded_integers(render_mode,frames):
    with pytest.raises(ValueError):
        m.build_plan('/workspace','/python','/runtime/media_stage.py','/rvc.py',
            '/model.pth','/index','/hubert.pt',frames,
            demucs_repo='/private/offline/htdemucs',render_mode=render_mode)


@pytest.mark.parametrize('render_mode',['full','excerpt'])
@pytest.mark.parametrize('path_index',range(8))
def test_runtime_paths_are_absolute(render_mode,path_index):
    paths=['/workspace','/python','/runtime/media_stage.py','/rvc.py',
           '/model.pth','/index','/hubert.pt','/private/offline/htdemucs']
    paths[path_index]='relative/path'
    with pytest.raises(ValueError,match='absolute'):
        m.build_plan(*paths[:7],30*RATE,demucs_repo=paths[7],render_mode=render_mode)
