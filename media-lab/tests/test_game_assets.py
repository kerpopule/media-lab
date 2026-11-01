import io
import json
import zipfile

import pytest
from PIL import Image, PngImagePlugin

from media_lab_core.game_assets import export_frames, pack_frames


def png(image, private_note=False):
    data = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if private_note:
        info.add_text('private', 'source note must not ship')
    image.save(data, format='PNG', pnginfo=info)
    return data.getvalue()


def test_trimming_round_trip_preserves_alignment_pixels_and_partial_alpha():
    frames = []
    originals = []
    for offset in (1, 3):
        frame = Image.new('RGBA', (8, 8))
        frame.paste((30, 80, 190, 128), (offset, 2, offset + 2, 6))
        originals.append(frame)
        frames.append((f'walk-{offset}.png', png(frame, True)))
    result = pack_frames(frames, columns=2, padding=2, pivot=(0.5, 1))
    sheet = Image.open(io.BytesIO(result.png))
    assert sheet.mode == 'RGBA' and 'private' not in sheet.info
    for (name, _), original in zip(frames, originals):
        entry = result.metadata['frames'][name]
        box, source = entry['frame'], entry['spriteSourceSize']
        recovered = Image.new('RGBA', original.size)
        crop = sheet.crop((box['x'], box['y'], box['x'] + box['w'], box['y'] + box['h']))
        recovered.paste(crop, (source['x'], source['y']))
        assert recovered.tobytes() == original.tobytes()
        assert entry['pivot'] == {'x': 0.5, 'y': 1}
        assert sheet.getpixel((box['x'] - 1, box['y']))[3] == 0


def test_empty_frame_and_nontrimmed_export_keep_source_dimensions():
    source = png(Image.new('RGBA', (12, 16)))
    trimmed = pack_frames([('empty.png', source)]).metadata['frames']['empty.png']
    assert trimmed['empty'] and trimmed['sourceSize'] == {'w': 12, 'h': 16}
    assert trimmed['frame']['w'] == trimmed['frame']['h'] == 1
    untrimmed = pack_frames([('empty.png', source)], trim=False).metadata['frames']['empty.png']
    assert not untrimmed['trimmed']
    assert untrimmed['frame']['w'] == 12 and untrimmed['frame']['h'] == 16


def test_portable_archive_is_deterministic_and_cannot_overwrite(tmp_path):
    source = tmp_path / 'badge.png'
    source.write_bytes(png(Image.new('RGBA', (4, 4), 'purple')))
    output = tmp_path / 'sprites.zip'
    receipt = export_frames([source], output)
    first = output.read_bytes()
    other = tmp_path / 'other.zip'
    assert export_frames([source], other)['sha256'] == receipt['sha256']
    assert first == other.read_bytes()
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ['atlas.png', 'atlas.json']
        data = json.loads(archive.read('atlas.json'))
        assert data['meta']['frameOrder'] == ['badge.png']
        assert data['meta']['image'] == 'atlas.png'
    with pytest.raises(FileExistsError):
        export_frames([source], output)
    assert output.read_bytes() == first
    assert not list(tmp_path.glob('.vibex-sprites-*'))


@pytest.mark.parametrize('options', [
    {'columns': 0}, {'columns': 2}, {'padding': -1}, {'padding': True},
    {'pivot': (0, float('nan'))}, {'trim': 'yes'},
])
def test_invalid_options_are_rejected(options):
    with pytest.raises(ValueError):
        pack_frames([('one.png', png(Image.new('RGBA', (4, 4))))], **options)


def test_duplicate_paths_invalid_images_and_layout_budget_are_rejected():
    image = png(Image.new('RGBA', (1, 1)))
    for frames in [[], [('A.png', image), ('a.png', image)], [('../bad.png', image)], [('bad.png', b'not png')]]:
        with pytest.raises(ValueError):
            pack_frames(frames)
    with pytest.raises(ValueError, match='layout is too large'):
        pack_frames([(f'{index}.png', image) for index in range(256)], columns=256, padding=32)
