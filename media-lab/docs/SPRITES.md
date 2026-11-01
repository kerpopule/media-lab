# Sprite atlas export

Media Lab can pack existing PNG animation frames into a transparent atlas and
JSON metadata. This implementation runs on the CPU with Pillow; it does not use
Maestro, WanGP, a model download or a cloud service.

## From the VibeXStudio Library

Connect a Media Lab library with its access code, choose a project, then select
**Make a sprite atlas**. Select server PNGs in animation order and choose **Add
atlas and frame data to project**. Removing and reselecting a frame moves it to
the end. The app copies both files into the project and rewrites the metadata's
image reference to the copied PNG's relative filename. The builder sees both
files in chat. The current UI supports up to 64 server PNGs; device-only frame
packing and advanced layout/pivot controls remain to be integrated.

This uses the read-only library ticket for a stateless derived download at
`POST /api/studio/library/sprites`. It does not modify the library or create a
render job. One export runs at a time, with bounded input and output sizes.

## Command line

From the `media-lab` directory, using its installed Python environment:

```sh
python -m media_lab_core.game_assets walk-0.png walk-1.png walk-2.png \
  --output walk-atlas.zip --columns 3 --pivot 0.5 1
```

Pass frames in animation order. The output ZIP contains `atlas.png` and
`atlas.json` with portable relative names. Existing output files are never
replaced. The command prints a JSON receipt with dimensions, byte count and the
ZIP's SHA-256.

Options:

- `--columns`: columns in the grid; by default, approximately square.
- `--padding`: transparent spacing around each frame; default 2 pixels.
- `--no-trim`: preserve the full original frame rectangles.
- `--pivot X Y`: normalized placement point; default center (`0.5 0.5`).

Frames retain their original pixels and partial alpha. Trimming records both the
cropped rectangle and its original position, so an animation does not jump as its
bounds change. No scaling, rotation, edge extrusion or background inference is
performed. An opaque input still has an opaque background. Animated PNG inputs
must be split into individual frames first.

## Using the metadata in a game

`meta.frameOrder` gives the supplied animation order. For each entry:

- Read its `frame` rectangle from the atlas.
- Place it at `spriteSourceSize.x/y` inside the original `sourceSize` canvas.
- Apply `pivot` relative to that original canvas, not the cropped rectangle.

For a canvas renderer, with a frame entry `f`, atlas image `atlas` and desired
pivot position `x, y`:

```js
ctx.imageSmoothingEnabled = false; // for pixel art
ctx.drawImage(atlas,
  f.frame.x, f.frame.y, f.frame.w, f.frame.h,
  x - f.sourceSize.w * f.pivot.x + f.spriteSourceSize.x,
  y - f.sourceSize.h * f.pivot.y + f.spriteSourceSize.y,
  f.frame.w, f.frame.h);
```

The metadata resembles common atlas formats, but Unity/Godot engine-specific
import presets have not been validated. A fully transparent trimmed frame keeps
its original dimensions in metadata and occupies one transparent pixel in the
atlas.

The web/desktop preview creates its media and JSON resources inside the sandbox.
Static JSON image references resolve relative to the JSON file, and scripts can
fetch the mapped JSON without relying on the app's origin. A four-frame canvas
animation has been checked in this sandbox and as an ordinary exported web
project, including pixel reads. The same document also passes isolated iOS26.5
WKWebView and Android API35 WebView probes with four distinct animation frames
and readable canvas pixels. Native local previews now use that shared resource
document: iOS file-URL fetches returned status0 and failed ordinary response.ok
checks. Neither native probe needs file-URL access enabled.

These are WebView-level probes, not full installed VibeXStudio app tests. They do
not establish support for arbitrary dynamic asset paths, JavaScript module
dependency graphs, every OS release or physical-device lifecycle behavior.

## Limits and remaining work

The exporter accepts 1–256 uniquely named PNG frames, at most 64 MiB compressed
input and 32 million decoded input pixels. The output has the same pixel budget
and an 8192-pixel maximum side. Oversized layouts fail rather than resizing art.

Background removal, manual mask correction, AI animation generation, device-only
Library export and qualified model installation remain separate work.
The current tests prove pixel/alpha preservation, alignment, transparent padding,
portable deterministic archives, invalid-input rejection and no-overwrite output.
They do not establish animation quality or cross-engine import compatibility.
