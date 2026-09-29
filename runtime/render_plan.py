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
               frames, rate=44100, instrumental=False):
    paths = [Path(p) for p in (workspace,worker_python,worker_script,rvc_script,model,index)]
    if any(not p.is_absolute() for p in paths):
        raise ValueError('Runtime paths must be absolute')
    if type(frames) is not int or rate != 44100 or not 30*rate <= frames <= 300*rate:
        raise ValueError('Plan requires decoded full-source length within 30–300 seconds')
    if type(instrumental) is not bool:
        raise ValueError('Instrumental must be explicit bool')
    work, python, worker, rvc, model, index = map(str,paths)
    # Match worker.chunk_bounds: fold tails shorter than five seconds.
    starts=list(range(0,frames,20*rate))
    if len(starts)>1 and frames-starts[-1]<5*rate:
        starts.pop()
    count=len(starts)
    chunks=tuple('vocal_%03d.wav'%n for n in range(count))
    converted=tuple('vocal_%03d_converted.wav'%n for n in range(count))
    opts=('--scratch',work,'--model',model,'--index',index)
    steps=[Step('decode',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1',
        '-i',work+'/source.audio','-map','0:a:0','-ar','44100','-ac','2','-c:a','pcm_f32le',
        work+'/original.wav'),120,('source.audio',),('original.wav',)),
        Step('separate',(python,worker,'separate',*opts),600,('original.wav',),
             ('vocals.wav','backing.wav',*chunks))]
    for number,(original,output) in enumerate(zip(chunks,converted)):
        steps.append(Step('convert_%03d'%number,(python,rvc,'--model',model,'--index',index,
            '--input',work+'/'+original,'--output',work+'/'+output,'--limit-seconds','25',
            '--pitch','0','--f0-method','harvest','--index-rate','0.5','--filter-radius','3',
            '--rms-mix-rate','0.25','--protect','0.33','--seed','20260928','--resample-sr','44100'),
            180,(original,),(output,)))
    steps.extend([
        Step('mix',(python,worker,'mix',*opts,*(('--instrumental',) if instrumental else ())),
             60,('vocals.wav','backing.wav',*chunks,*converted),('mixed.wav',)),
        Step('encode',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',work+'/mixed.wav',
             '-c:a','libmp3lame','-b:a','192k',work+'/cover.mp3'),90,('mixed.wav',),('cover.mp3',)),
        Step('validate',('ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',work+'/cover.mp3',
             '-f','null','-'),60,('cover.mp3',),('cover.mp3',))])
    return tuple(steps)


def total_compute_budget(steps):
    # Queue wait is excluded; scheduler also imposes queue expiry and launch cap.
    return sum(step.unit_limit_s for step in steps)
