"""Independent SDR709 -> HLG2020 finishing and exact signaling verification.
No raw third-party scripts. No source overwrite, no shell execution, no upload.
Color equations: BT.2100 HLG OETF/OOTF, BT.709/2020 linear primaries.
SDR display-linear source assumption is explicit gamma2.4, diffuse white203nit.
Selective highlight expansion is an artistic decision, not recovered HDR detail.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import subprocess
import numpy as np
from .hdr_color import A, B, C, LUMA, inverse_hlg

FROM709 = np.array([[.627404, .329283, .043313],
                    [.069097, .919540, .011362],
                    [.016391, .088013, .895595]])
Y709 = np.array([.2126, .7152, .0722])


def oetf(scene):
    scene = np.clip(scene, 0, 1)
    return np.where(scene <= 1/12, np.sqrt(3*scene),
                    A*np.log(np.maximum(12*scene-B, 1e-12))+C)


def transform(rgb, boost=True, white=203.0, shoulder_power=3.0):
    if not 100 <= white <= 800:
        raise ValueError('white must be within bounded100..800nit creative range')
    if not .5 <= shoulder_power <= 6:
        raise ValueError('shoulder power must be bounded .5..6')
    linear709 = np.clip(rgb, 0, 1) ** 2.4
    y = linear709 @ Y709
    # Broad smooth shoulder, with extra expansion confined to brighter material.
    position = np.clip((y-.40)/.60, 0, 1)
    shoulder = (position*position*(3-2*position)) ** shoulder_power
    luminance_nits = white*y + ((1000-white)*shoulder if boost else 0)
    scale = np.divide(luminance_nits, y, out=np.full_like(y, white), where=y > 1e-12)
    display2020 = (linear709*scale[..., None]) @ FROM709.T / 1000
    yd = display2020 @ LUMA
    undo_ootf = np.maximum(yd, 1e-12) ** (1/1.2-1)
    return oetf(display2020*undo_ootf[..., None])


def write_lut(path, boost, white=203.0, shoulder_power=3.0):
    b, g, r = np.meshgrid(*[np.linspace(0, 1, 65)]*3, indexing='ij')
    values = transform(np.stack((r, g, b), -1).reshape(-1, 3), boost, white, shoulder_power)
    with path.open('x') as f:
        f.write('TITLE "SDR709 display gamma2.4 to HLG2020"\nLUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(f, values, fmt='%.8f')


def probe(path):
    return json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=60).stdout)


def colr_atoms(data):
    found = []
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}
    def walk(start, end):
        pos = start
        while pos+8 <= end:
            size, kind = struct.unpack_from('>I4s', data, pos)
            head = 8
            if size == 1:
                if pos+16 > end: raise ValueError('Truncated extended box')
                size = struct.unpack_from('>Q', data, pos+8)[0]
                head = 16
            elif size == 0:
                size = end-pos
            if size < head or pos+size > end:
                raise ValueError('Invalid MP4 box')
            body = pos+head
            stop = pos+size
            if kind in containers:
                walk(body, stop)
            elif kind == b'stsd':
                walk(body+8, stop)
            elif kind in {b'avc1', b'avc3', b'hvc1', b'hev1'}:
                walk(body+78, stop)
            elif kind == b'colr' and data[body:body+4] in {b'nclx', b'nclc'}:
                found.append({'type': data[body:body+4].decode(), 'values': list(struct.unpack_from('>HHH', data, body+4)), 'offset':pos})
            pos = stop
    walk(0, len(data))
    return found


def verify(path, codec, *, cancelled=lambda: False):
    info = probe(path)
    v = next(x for x in info['streams'] if x['codec_type']=='video')
    expected = {'codec_name': 'h264' if codec=='x264' else 'hevc',
                'pix_fmt': 'yuv420p' if codec=='x264' else 'yuv420p10le',
                'color_range':'tv','color_primaries':'bt2020',
                'color_transfer':'arib-std-b67','color_space':'bt2020nc'}
    for k, value in expected.items():
        if v.get(k) != value: raise ValueError(f'Wrong {k}: {v.get(k)!r}')
    if codec=='x264' and v.get('profile') != 'High': raise ValueError('Wrong H264 profile')
    boxes = colr_atoms(path.read_bytes())
    if not boxes or any(x['values'] != [9,18,9] for x in boxes): raise ValueError('Container color signaling mismatch')
    trace = subprocess.run(['ffmpeg','-v','verbose','-i',str(path),'-map','0:v:0','-c:v','copy','-bsf:v','trace_headers','-frames:v','1','-f','null','-'],capture_output=True,text=True,check=True,timeout=60).stderr
    vui = {}
    for key in ['colour_primaries','transfer_characteristics','matrix_coefficients']:
        matches = re.findall(r'\b'+key+r'\s+\S+\s*=\s*(\d+)', trace)
        if not matches: raise ValueError('Missing VUI field '+key)
        vui[key] = sorted(set(map(int,matches)))
    if list(vui.values()) != [[9],[18],[9]]: raise ValueError('VUI signaling mismatch')
    from .sdr_delivery import run
    run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(path),'-f','null','-'], cancelled=cancelled)
    return {'probe':info, 'container_colr':boxes, 'codec_vui':vui, 'full_decode_exit':0,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'limits':'Technical signaling only; compatible HDR-display creative review still required.'}


def finish(source, target, codec, boost=True, assume=False, white=203.0, shoulder_power=3.0, *, cancelled=lambda: False):
    source, target = source.resolve(), target.resolve()
    if target.exists() or source==target: raise ValueError('Refuse existing output/source overwrite')
    info = probe(source)
    stream = next(x for x in info['streams'] if x['codec_type']=='video')
    if stream.get('color_transfer') in {'arib-std-b67','smpte2084'}: raise ValueError('Already HDR; do not convert twice')
    if not assume and (stream.get('color_primaries') != 'bt709' or stream.get('color_space') != 'bt709' or stream.get('color_range') != 'tv'):
        raise ValueError('Ambiguous SDR source; explicit source assumption required')
    lut = target.parent / ('hlg-' + hashlib.sha256(target.name.encode()).hexdigest()[:20] + '.cube')
    write_lut(lut, boost, white, shoulder_power)
    # Pass LUT basename relative to cwd, not caller-controlled filter syntax.
    vf = f'scale=in_color_matrix=bt709:in_range=tv:out_range=pc:flags=accurate_rnd+full_chroma_int,format=gbrp16le,lut3d=file={lut.name}:interp=tetrahedral,scale=out_color_matrix=bt2020:out_range=tv:flags=accurate_rnd+full_chroma_int,format={"yuv420p" if codec=="x264" else "yuv420p10le"},setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=arib-std-b67:range=tv'
    encoding = ['-c:v','libx264','-profile:v','high','-pix_fmt','yuv420p','-x264-params','colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc'] if codec=='x264' else ['-c:v','libx265','-pix_fmt','yuv420p10le','-tag:v','hvc1','-x265-params','colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:log-level=error']
    cmd = ['ffmpeg','-v','error','-n','-i',str(source),'-map','0:v:0','-map','0:a?','-vf',vf,*encoding,'-preset','medium','-crf','17','-color_primaries','bt2020','-color_trc','arib-std-b67','-colorspace','bt2020nc','-color_range','tv','-c:a','copy','-movflags','+faststart',str(target)]
    from .sdr_delivery import run
    try:
        run(cmd,cwd=target.parent,cancelled=cancelled)
        receipt=verify(target,codec,cancelled=cancelled)
    finally:
        lut.unlink(missing_ok=True)
    receipt.update(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),boost=boost,white_nits=white,shoulder_power=shoulder_power,source_assumption='BT709 limited display gamma2.4',command=cmd)
    target.with_suffix('.receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'output':str(target),'sha256':receipt['sha256'],'codec_vui':receipt['codec_vui'],'container':receipt['container_colr'],'full_decode_exit':0}))


def selftest():
    x = np.linspace(0,1,1001)
    assert np.allclose(inverse_hlg(oetf(x)),x,atol=1e-7)
    neutral = np.repeat(x[:,None],3,axis=1)
    out = transform(neutral)
    assert np.isfinite(out).all() and np.min(np.diff(out,axis=0))>=-1e-8
    assert np.max(np.ptp(out,axis=1))<1e-5
    assert np.allclose(transform(np.ones((1,3)),boost=False),.75,atol=.002)
    assert np.allclose(transform(np.ones((1,3))),1,atol=1e-5)
    print(json.dumps({'forward_inverse_hlg_vectors':'pass','monotonic_neutral':'pass','SDR203nit_white_near_HLG75':'pass','boosted1000nit_white':'pass'}))


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('source',type=Path,nargs='?')
    p.add_argument('target',type=Path,nargs='?')
    p.add_argument('--codec',choices=['x264','x265'],default='x264')
    p.add_argument('--assume-sdr709-limited',action='store_true')
    p.add_argument('--no-boost',action='store_true')
    p.add_argument('--white',type=float,default=203.0)
    p.add_argument('--shoulder-power',type=float,default=3.0)
    p.add_argument('--selftest',action='store_true')
    a=p.parse_args()
    if a.selftest: selftest()
    if a.source and a.target: finish(a.source,a.target,a.codec,not a.no_boost,a.assume_sdr709_limited,a.white,a.shoulder_power)
