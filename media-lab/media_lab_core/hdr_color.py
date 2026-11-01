"""Independent BT.2100 HLG inspection transform. Not upstream skill code.

1000-nit reference HLG display, system gamma1.2; BT.2020 -> BT.709
linear matrix, extended Reinhard SDR viewing trim, gamma2.4 output.
This proxy is for composition inspection, not proof of physical display nits.
"""
from pathlib import Path
import argparse
import json
import subprocess
import numpy as np

LUMA = np.array([.2627, .6780, .0593])
TO709 = np.array([[1.660491, -.587641, -.072850],
                  [-.124550, 1.132900, -.008349],
                  [-.018151, -.100579, 1.118730]])
A = .17883277
B = 1 - 4 * A
C = .5 - A * np.log(4 * A)


def inverse_hlg(signal):
    signal = np.clip(signal, 0, 1)
    return np.where(signal <= .5, signal * signal / 3,
                    (np.exp((signal - C) / A) + B) / 12)


def inspection_trim(signal):
    scene = inverse_hlg(signal)
    luminance = scene @ LUMA
    display = scene * np.maximum(luminance, 0)[..., None] ** .2 * 1000
    y = display @ LUMA / 203
    white = 1000 / 203
    mapped = y * (1 + y / (white * white)) / (1 + y)
    gain = np.divide(mapped, y, out=np.zeros_like(y), where=y > 1e-12)
    rgb = (display / 203 * gain[..., None]) @ TO709.T
    return np.clip(rgb, 0, 1) ** (1 / 2.4)


def cube(path, size=33):
    if path.exists():
        raise FileExistsError(path)
    # .cube contract: red coordinate advances fastest.
    b, g, r = np.meshgrid(*[np.linspace(0, 1, size)] * 3, indexing='ij')
    out = inspection_trim(np.stack((r, g, b), -1).reshape(-1, 3))
    with path.open('x') as f:
        f.write('TITLE "HLG1000 to SDR709 inspection"\n')
        f.write(f'LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(f, out, fmt='%.8f')


def selftest():
    assert np.allclose(inverse_hlg(np.array([0., .5, 1.])), [0, 1/12, 1], atol=1e-7)
    neutral = np.repeat(np.linspace(0, 1, 1001)[:, None], 3, axis=1)
    out = inspection_trim(neutral)
    assert np.isfinite(out).all()
    assert np.min(np.diff(out, axis=0)) >= -1e-8
    assert np.max(np.ptp(out, axis=1)) < 1e-5
    assert np.allclose(out[0], 0)
    assert np.allclose(out[-1], 1, atol=1e-5)
    print(json.dumps({'inverse_hlg_vectors': 'pass', 'neutral_monotonic': 'pass',
                      'black_white_endpoints': 'pass', 'scope': 'inspection transform only'}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--cube', type=Path)
    a = ap.parse_args()
    if a.selftest:
        selftest()
    if a.cube:
        cube(a.cube)


if __name__ == '__main__':
    main()
