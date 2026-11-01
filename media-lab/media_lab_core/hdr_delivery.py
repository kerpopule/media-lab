"""Default managed Media Lab delivery. Never fall back to mislabeled SDR.
Preserve the engine original outside the Library; only publish verified HLG.
An SDR -> HLG grade expands highlights artistically, not recovered sensor HDR.
"""
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from . import hdr_finish

POLICY = 'hlg2020-avc-high8-v1'

def encode(source, target, *, assume_sdr709=False, cancelled=lambda: False):
    source, target = Path(source), Path(target)
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError('Refuse existing output/source overwrite')
    if cancelled():
        raise RuntimeError('Video finishing cancelled')
    stream = next(s for s in hdr_finish.probe(source)['streams'] if s['codec_type']=='video')
    transfer = stream.get('color_transfer')
    if transfer == 'smpte2084':
        raise ValueError('PQ source needs a qualified PQ-to-HLG transform; refusing mislabel')
    if transfer == 'arib-std-b67':
        if stream.get('color_primaries')!='bt2020' or stream.get('color_space')!='bt2020nc':
            raise ValueError('Inconsistent HLG source metadata')
        from .sdr_delivery import run
        run(['ffmpeg','-nostdin','-v','error','-n','-i',str(source),'-map','0:v:0','-map','0:a?',
            '-c:v','libx264','-profile:v','high','-pix_fmt','yuv420p','-preset','medium','-crf','17',
            '-x264-params','colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
            '-color_primaries','bt2020','-color_trc','arib-std-b67','-colorspace','bt2020nc','-color_range','tv',
            '-c:a','copy','-movflags','+faststart',str(target)],cancelled=cancelled)
        receipt=hdr_finish.verify(target,'x264',cancelled=cancelled)
        receipt['source_transform']='HLG2020 preserved, no second grade'
    else:
        if transfer not in (None,'unknown','bt709'):
            raise ValueError('Unsupported source transfer, refusing an implicit conversion')
        for field in ('color_primaries','color_space'):
            if stream.get(field) not in (None,'unknown','bt709'):
                raise ValueError('Incompatible source gamut, refusing implicit BT709')
        if stream.get('color_range') not in (None,'unknown','tv'):
            raise ValueError('Full-range source needs a qualified transform')
        hdr_finish.finish(source,target,'x264',assume=assume_sdr709,cancelled=cancelled)
        receipt=json.loads(target.with_suffix('.receipt.json').read_text())
    if cancelled():
        raise RuntimeError('Video finishing cancelled before publication')
    receipt.update(policy=POLICY, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    target.with_suffix('.receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt

def retain_original(source, job_dir):
    from .sdr_delivery import digest
    source, job_dir = Path(source), Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    original = job_dir / 'engine-original.mp4'
    if not original.exists():
        shutil.copy2(source, original)
    elif digest(source) != digest(original):
        raise ValueError('Existing engine original differs; preserve both, do not overwrite')
    return original


def prepare(source, target, job_dir, *, cancelled=lambda: False, face_fix=None, sdr_target=None):
    """Target is an unpublished owned staging path, never a current Library file."""
    source,target,job_dir=Path(source),Path(target),Path(job_dir)
    targets = [target] + ([Path(sdr_target)] if sdr_target is not None else [])
    if len({p.resolve() for p in targets}) != len(targets) or any(
            p.exists() or p.resolve() == source.resolve() for p in targets):
        raise ValueError('Refuse existing output/source overwrite')
    job_dir.mkdir(parents=True,exist_ok=True)
    retain_original(source, job_dir)
    with tempfile.TemporaryDirectory(prefix='hdr-grade-',dir=job_dir) as temp:
        working=Path(temp)/'working.mp4'
        shutil.copy2(source,working)
        if face_fix:
            face_fix(working)  # before HDR, never overwrite graded pixels with SDR enhancement
        sdr_receipt = None
        if sdr_target is not None:
            from . import sdr_delivery
            sdr_receipt = sdr_delivery.encode(working, sdr_target, assume_sdr709=True,
                                               cancelled=cancelled)
        try:
            receipt=encode(working,target,assume_sdr709=True,cancelled=cancelled)
            if sdr_receipt is not None:
                receipt['sdr_delivery'] = sdr_receipt
                target.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        except BaseException:
            target.unlink(missing_ok=True)
            if sdr_target is not None:
                Path(sdr_target).unlink(missing_ok=True)
            raise
    return receipt


def sdr_poster(source, target, job_dir):
    """Tone-mapped JPEG preview; an HDR MP4 must not be flattened by tag-blind JPEG export."""
    from .hdr_color import cube
    job_dir=Path(job_dir);job_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hdr-poster-',dir=job_dir) as temporary:
        lut=Path(temporary)/'inspection.cube';cube(lut)
        vf=f'scale=in_color_matrix=bt2020:in_range=tv:out_range=pc,format=gbrp16le,lut3d=file={lut.name}:interp=tetrahedral,format=rgb24,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(Path(source).resolve()),'-vf',vf,'-frames:v','1',str(Path(target).resolve())],cwd=temporary,check=True,capture_output=True,timeout=60)


def enforce_library_video(source, job_dir, *, input_source=None, cancelled=lambda: False):
    """Final gate for produced Library MP4s, not import/legacy asset migration."""
    source, job_dir = Path(source), Path(job_dir)
    stream = next(s for s in hdr_finish.probe(source)['streams'] if s['codec_type']=='video')
    if stream.get('color_transfer') == 'arib-std-b67':
        if stream.get('codec_name')=='h264' and stream.get('profile')=='High' and stream.get('pix_fmt')=='yuv420p':
            return hdr_finish.verify(source,'x264')
    elif input_source and Path(input_source).is_file():
        input_stream = next(s for s in hdr_finish.probe(input_source)['streams'] if s['codec_type']=='video')
        if input_stream.get('color_transfer') in ('arib-std-b67','smpte2084'):
            raise ValueError('Finishing engine lost HDR source signaling; refusing an implicit second grade')
    job_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='library-hdr-',dir=job_dir) as temp:
        target=Path(temp)/'hlg.mp4'
        receipt=prepare(source,target,job_dir/'retained',cancelled=cancelled)
        if cancelled():raise RuntimeError('Cancelled before Library HDR publication')
        import os
        os.replace(target,source)
    return receipt
