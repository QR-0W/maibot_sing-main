"""Finite per-stage execution plan; no whole-song 900-second worker.

Commands are argv arrays, never shell strings. The supervisor MUST enforce
resource limits, in-service lock, checkpoints and total job deadline separately.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple
    timeout_s: int
    inputs: tuple
    outputs: tuple

    @property
    def unit_limit_s(self):
        # Grace for child teardown and durable error/receipt writing.
        return self.timeout_s + 30


def build_plan(workspace, worker_python, worker_script, rvc_script, model, index,
               hubert, frames, *, render_mode='full', demucs_repo, rate=44100,
               instrumental=False):
    paths = [Path(p) for p in (workspace,worker_python,worker_script,rvc_script,model,index,hubert,demucs_repo)]
    if any(not p.is_absolute() for p in paths):
        raise ValueError('Runtime paths must be absolute')
    if type(frames) is not int or rate != 44100 or not 30*rate <= frames <= 300*rate:
        raise ValueError('Plan requires decoded full-source length within 30–300 seconds')
    if type(instrumental) is not bool:
        raise ValueError('Instrumental must be explicit bool')
    if type(render_mode) is not str or render_mode not in ('full', 'excerpt'):
        raise ValueError('Render mode must be full or excerpt')
    work, python, worker, rvc, model, index, hubert, demucs_repo = map(str,paths)
    # Preserve the original full-song timeline and worker.chunk_bounds policy:
    # 20-second chunks, folding only tails strictly shorter than five seconds.
    if render_mode == 'full':
        starts=list(range(0,frames,20*rate))
        if len(starts)>1 and frames-starts[-1]<5*rate:
            starts.pop()
        count=len(starts)
    else:
        # Selection determines the actual length of one bounded 12–18 s chunk.
        count=1
    chunks=tuple('vocal_%03d.wav'%n for n in range(count))
    converted=tuple('vocal_%03d_converted.wav'%n for n in range(count))
    opts=('--scratch',work,'--model',model,'--index',index,'--render-mode',render_mode)
    steps=[Step('decode',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1',
        '-i',work+'/source.audio','-map','0:a:0','-ar','44100','-ac','2','-c:a','pcm_f32le',
        work+'/original.wav'),120,('source.audio',),('original.wav',)),
        Step('separate',(python,worker,'separate',*opts,'--demucs-repo',demucs_repo),600,('original.wav',),
             ('vocals.wav','backing.wav',*(chunks if render_mode == 'full' else ())))]
    if render_mode == 'excerpt':
        steps.append(Step('excerpt',(python,worker,'excerpt',*opts),60,
            ('vocals.wav','backing.wav'),('selection.json',*chunks,'excerpt_backing.wav')))
    for number,(original,output) in enumerate(zip(chunks,converted)):
        steps.append(Step('convert_%03d'%number,(python,rvc,'--model',model,'--index',index,
            '--hubert',hubert,'--input',work+'/'+original,'--output',work+'/'+output,'--limit-seconds','25',
            '--pitch','0','--f0-method','harvest','--index-rate','0.5','--filter-radius','3',
            '--rms-mix-rate','0.25','--protect','0.33','--seed','20260928','--resample-sr','44100'),
            180,('selection.json',original) if render_mode == 'excerpt' else (original,),
            (output,)))
    mix_inputs=(('selection.json','excerpt_backing.wav') if render_mode == 'excerpt'
                else ('vocals.wav','backing.wav'))
    steps.extend([
        Step('mix',(python,worker,'mix',*opts,*(('--instrumental',) if instrumental else ())),
             60,(*mix_inputs,*chunks,*converted),('mixed.wav',)),
        Step('encode',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',work+'/mixed.wav',
             '-c:a','libmp3lame','-b:a','192k',work+'/cover.mp3'),90,('mixed.wav',),('cover.mp3',)),
        Step('validate',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',work+'/cover.mp3',
             '-f','null','-'),60,('cover.mp3',),('cover.mp3',))])
    return tuple(steps)


def total_compute_budget(steps):
    # Queue wait is excluded; scheduler also imposes queue expiry and launch cap.
    return sum(step.unit_limit_s for step in steps)
