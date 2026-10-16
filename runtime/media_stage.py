"""Dedicated stage entry point, called only inside the bounded stage supervisor.

Unlike legacy worker CLI this needs no musicdl runtime or source-search inputs.
The supervisor owns the inference lock for the entire child process lifetime.
"""
import argparse
from pathlib import Path

if __package__:
    from .context_window import validate_context_window
    from .worker import verify_limits, chunk_bounds, mix as mix_full
    from .excerpt_selection import select_excerpt, mix_excerpt
else:
    from context_window import validate_context_window
    from worker import verify_limits, chunk_bounds, mix as mix_full
    from excerpt_selection import select_excerpt, mix_excerpt


def validate_timeline(args):
    """Reject oversized or changed windows before reading PCM or loading models."""
    if type(args.render_mode) is not str or args.render_mode not in ('full','excerpt'):
        raise ValueError('Render mode must be full or excerpt')
    context={'start_frame':args.context_start_frame,'end_frame':args.context_end_frame}
    validate_context_window(context,args.source_frames,frozen=args.render_mode=='excerpt')
    if args.render_mode=='full' and args.source_frames>300*44100:
        raise ValueError('Full source exceeds 300 seconds')
    return context


def separate(args):
    """Separate once into shared stems; only full also writes all RVC chunks.

    Excerpt selection owns its single chunk, so no unsealed full-song chunks
    may be produced in excerpt mode. Neither mode uses default model caches.
    """
    render_mode=args.render_mode
    if type(render_mode) is not str or render_mode not in ('full', 'excerpt'):
        raise ValueError('Render mode must be full or excerpt')
    context=validate_timeline(args)
    repo=Path(args.demucs_repo)
    if not repo.is_absolute() or not repo.is_dir() or repo.is_symlink():
        raise ValueError('显式 Demucs repo 缺失或无效，拒绝默认缓存/联网')
    import numpy as np
    import soundfile as sf
    info=sf.info(args.scratch/'original.wav')
    expected_frames=context['end_frame']-context['start_frame']
    if (info.samplerate!=44100 or info.channels!=2 or info.frames!=expected_frames):
        raise ValueError('无效的输入音频：与冻结上下文窗口不一致')
    data,rate=sf.read(args.scratch/'original.wav',dtype='float32',always_2d=True)
    if (rate!=44100 or data.shape!=(info.frames,2) or not np.isfinite(data).all()):
        raise ValueError('无效的输入音频')
    # No model import or load before cheap offline input/repo validation.
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    wave=torch.from_numpy(data.T.copy())
    ref=wave.mean(0)
    scale,center=ref.std(),ref.mean()
    if float(scale)<1e-6:
        raise ValueError('音频无声音')
    model=get_model('htdemucs',repo=repo).cpu().eval()
    with torch.inference_mode():
        stems=apply_model(model,((wave-center)/scale)[None],device='cpu',
            shifts=0,split=True,segment=5,overlap=0.25,num_workers=0,progress=False)[0]*scale+center
    voice=stems[model.sources.index('vocals')].T.numpy()
    backing=sum(stems[i] for i,name in enumerate(model.sources) if name!='vocals').T.numpy()
    if (voice.shape!=data.shape or backing.shape!=data.shape or
            not np.isfinite(voice).all() or not np.isfinite(backing).all()):
        raise ValueError('分离音轨时间轴或数值异常')
    mono=voice.mean(axis=1)
    sf.write(args.scratch/'vocals.wav',mono,rate,subtype='FLOAT')
    sf.write(args.scratch/'backing.wav',backing,rate,subtype='FLOAT')
    if render_mode == 'full':
        for number,(start,end) in enumerate(chunk_bounds(len(mono),rate)):
            sf.write(args.scratch/f'vocal_{number:03d}.wav',mono[start:end],rate,subtype='FLOAT')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',choices=['separate','excerpt','mix'])
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--demucs-repo',type=Path)
    parser.add_argument('--render-mode',choices=['full','excerpt'],default='full')
    parser.add_argument('--source-frames',type=int,required=True)
    parser.add_argument('--context-start-frame',type=int,required=True)
    parser.add_argument('--context-end-frame',type=int,required=True)
    parser.add_argument('--instrumental',action='store_true')
    args=parser.parse_args()
    if args.stage=='separate' and args.demucs_repo is None:
        parser.error('separate requires explicit --demucs-repo; default cache is forbidden')
    if args.stage=='excerpt' and args.render_mode!='excerpt':
        parser.error('excerpt stage requires --render-mode excerpt')
    verify_limits()
    validate_timeline(args)
    mixer=mix_full if args.render_mode=='full' else mix_excerpt
    {'separate':separate,'excerpt':select_excerpt,'mix':mixer}[args.stage](args)


if __name__=='__main__':
    main()
