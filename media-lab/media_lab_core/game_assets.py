"""Deterministic sprite atlas export; no model runtime or network dependency."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32_000_000
MAX_SIDE = 8192


@dataclass(frozen=True)
class SpriteAtlas:
    png: bytes
    metadata: dict

    def files(self) -> dict[str, bytes]:
        return {'atlas.png': self.png,
                'atlas.json': (json.dumps(self.metadata, sort_keys=True, indent=2) + '\n').encode()}

    def archive(self) -> bytes:
        """Stable archive bytes, with no local paths or source image metadata."""
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in self.files().items():
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, content)
        return output.getvalue()


def pack_frames(frames: list[tuple[str, bytes]], *, columns: int | None = None,
                padding: int = 2, trim: bool = True, pivot=(0.5, 0.5)) -> SpriteAtlas:
    """Pack existing PNG frames, retaining alpha and their source-space pivot.

    Trimming changes storage bounds, not animation alignment. Consumers restore
    spriteSourceSize inside sourceSize before placing the normalized pivot.
    Padding remains transparent; frames are never resized or rotated.
    """
    if not 1 <= len(frames) <= 256:
        raise ValueError('Choose between 1 and 256 PNG frames.')
    if type(padding) is not int or not 0 <= padding <= 32:
        raise ValueError('Padding must be an integer between 0 and 32 pixels.')
    if columns is not None and (type(columns) is not int or not 1 <= columns <= len(frames)):
        raise ValueError('Columns must be between 1 and the number of frames.')
    if type(trim) is not bool:
        raise ValueError('Trim must be true or false.')
    if len(pivot) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in pivot):
        raise ValueError('Pivot coordinates must be between 0 and 1.')
    if sum(len(data) for _, data in frames) > MAX_INPUT_BYTES:
        raise ValueError('The selected frames exceed the 64 MiB input limit.')
    decoded, names, pixels = [], set(), 0
    for name, data in frames:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}\.png', name):
            raise ValueError('Frame names must be simple PNG filenames without directories.')
        if name.casefold() in names:
            raise ValueError('Each frame needs a unique filename.')
        names.add(name.casefold())
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format != 'PNG' or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('Choose individual PNG frames; split animations before packing.')
                pixels += source.width * source.height
                if pixels > MAX_PIXELS or max(source.size) > MAX_SIDE:
                    raise ValueError('The selected frames exceed the sprite pixel budget.')
                image = ImageOps.exif_transpose(source).convert('RGBA')
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f'{name} is not a readable PNG frame.') from exc
        size = image.size
        visible = image.getchannel('A').getbbox()
        empty = visible is None
        bounds = visible if trim else (0, 0, *size)
        if empty and trim:
            bounds = (0, 0, 1, 1)
            cropped = Image.new('RGBA', (1, 1))
        else:
            cropped = image.crop(bounds)
        decoded.append((name, cropped, size, bounds, empty))
    columns = columns or math.ceil(math.sqrt(len(decoded)))
    rows = math.ceil(len(decoded) / columns)
    cell_w = max(image.width for _, image, *_ in decoded) + padding * 2
    cell_h = max(image.height for _, image, *_ in decoded) + padding * 2
    width, height = columns * cell_w, rows * cell_h
    if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
        raise ValueError('This layout is too large. Use fewer frames or different columns.')
    sheet = Image.new('RGBA', (width, height))
    entries = {}
    for index, (name, image, size, bounds, empty) in enumerate(decoded):
        x, y = (index % columns) * cell_w + padding, (index // columns) * cell_h + padding
        # Do not use image as the paste mask: doing so squares partial alpha.
        sheet.paste(image, (x, y))
        entries[name] = {
            'frame': {'x': x, 'y': y, 'w': image.width, 'h': image.height},
            'rotated': False, 'trimmed': bounds != (0, 0, *size), 'empty': empty,
            'spriteSourceSize': {'x': bounds[0], 'y': bounds[1], 'w': image.width, 'h': image.height},
            'sourceSize': {'w': size[0], 'h': size[1]},
            'pivot': {'x': pivot[0], 'y': pivot[1]},
        }
    png = io.BytesIO()
    sheet.save(png, format='PNG')
    data = png.getvalue()
    return SpriteAtlas(data, {
        'frames': entries,
        'meta': {'app': 'VibeXStudio', 'version': '1', 'image': 'atlas.png',
                 'format': 'RGBA8888', 'size': {'w': width, 'h': height}, 'scale': '1',
                 'frameOrder': [name for name, _ in frames], 'padding': padding,
                 'sha256': hashlib.sha256(data).hexdigest()},
    })


def export_frames(paths: list[Path], output: Path, **options) -> dict:
    """Export a new ZIP without ever overwriting an existing destination."""
    if sum(path.stat().st_size for path in paths) > MAX_INPUT_BYTES:
        raise ValueError('The selected frames exceed the 64 MiB input limit.')
    atlas = pack_frames([(path.name, path.read_bytes()) for path in paths], **options)
    content = atlas.archive()
    output = Path(output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.vibex-sprites-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic publication with no replacement, including a racing writer.
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'output': str(output.resolve()), 'frames': len(paths), 'bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest(), 'size': atlas.metadata['meta']['size']}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Pack existing PNG frames into a transparent sprite atlas and JSON metadata.')
    parser.add_argument('frames', nargs='+', type=Path, help='PNG frames in animation order')
    parser.add_argument('--output', required=True, type=Path, help='New ZIP path; existing files are never replaced')
    parser.add_argument('--columns', type=int)
    parser.add_argument('--padding', type=int, default=2)
    parser.add_argument('--no-trim', action='store_true')
    parser.add_argument('--pivot', nargs=2, type=float, default=(0.5, 0.5), metavar=('X', 'Y'))
    args = parser.parse_args(argv)
    try:
        result = export_frames(args.frames, args.output, columns=args.columns, padding=args.padding,
                               trim=not args.no_trim, pivot=args.pivot)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Sprite export failed: {exc}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
