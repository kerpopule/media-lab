"""Build an editable timeline from explicitly resolved storyboard scenes.

The caller owns authorization, Library lookup and probing. This layer never
downloads media, substitutes missing scenes, writes files or starts rendering.
"""
import hashlib
import json
import math
import re

from . import cut


def _seconds(value, index):
    if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
        value = float(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise cut.CutError(f'Scene {index}: choose a positive duration in seconds.')
    return float(value)


def build_storyboard_project(project_id, title, scenes, sources, *, storyboard_id, fps=24, music_asset_id=None):
    """Convert every scene, in order, including repeated uses of one asset.

    ``scenes`` contains explicit asset_id and seconds choices, plus optional
    label/narration. ``sources`` contains already-probed Cut gallery records.
    Timing rounds cumulative boundaries to frames so individual rounding errors
    cannot accumulate. The exact submitted choices are bound in provenance.
    """
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 128:
        raise cut.CutError('Choose between one and 128 storyboard scenes.')
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
        raise cut.CutError('Choose a frame rate between 1 and 60.')
    items, resolved = [], []
    cursor_seconds, cursor_frames = 0.0, 0
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise cut.CutError(f'Scene {index}: the scene is invalid.')
        asset_id = scene.get('asset_id')
        source = sources.get(asset_id) if isinstance(asset_id, str) else None
        if not source or source.get('kind') not in ('image', 'video') or source.get('exists') is False:
            raise cut.CutError(f'Scene {index}: choose an available image or video from Library.')
        seconds = _seconds(scene.get('seconds'), index)
        cursor_seconds += seconds
        if cursor_seconds > 600:
            raise cut.CutError('This editor supports storyboards up to ten minutes.')
        end = round(cursor_seconds * fps)
        frames = end - cursor_frames
        if frames < 1:
            raise cut.CutError(f'Scene {index}: its duration is shorter than one frame.')
        if source['kind'] == 'video':
            duration = _seconds(source.get('duration_seconds'), index)
            if frames > round(duration * fps):
                raise cut.CutError(f'Scene {index}: the chosen video is shorter than this scene. Choose a longer clip or shorten the scene.')
        label = str(scene.get('label') or f'Scene {index}')[:240]
        # Occurrence identities keep repeated footage independently editable.
        items.append({**source, 'job_id': f'board-scene-{index}', 'title': label})
        resolved.append({'asset_id': asset_id, 'seconds': seconds, 'start_frame': cursor_frames,
                         'duration_frames': frames, 'label': label,
                         'narration': str(scene.get('narration') or '')[:4000]})
        cursor_frames = end
    if music_asset_id is not None:
        music = sources.get(music_asset_id)
        if not music or music.get('kind') != 'music' or music.get('exists') is False:
            raise cut.CutError('Choose an available audio file from Library for the soundtrack.')
        items.append({**music, 'job_id': 'board-soundtrack', 'title': 'Soundtrack'})
    project = cut.build_gallery_project(project_id, title, items, fps=fps)
    clips = project['timeline']['tracks'][0]['clips']
    for clip, scene, choice in zip(clips, project['storyboard']['scenes'], resolved):
        clip.update(start_frame=choice['start_frame'], duration_frames=choice['duration_frames'],
                    trim_in_frame=0, trim_out_frame=choice['duration_frames'])
        scene['narration'] = choice['narration']
        scene['source_metadata']['library_asset_id'] = choice['asset_id']
    if music_asset_id is not None:
        music_clip = project['timeline']['tracks'][1]['clips'][0]
        music_frames = min(cursor_frames, music_clip['duration_frames'])
        music_clip.update(duration_frames=music_frames, trim_out_frame=music_frames)
        project['provenance']['storyboard_music'] = {'library_asset_id': music_asset_id,
                                                     'end_frame': music_frames, 'fit': 'stop_at_visual_end'}
    encoded = json.dumps(resolved, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    project['provenance'].update(project_source='storyboard', storyboard_id=str(storyboard_id),
                                 storyboard_choices_sha256=hashlib.sha256(encoded).hexdigest(),
                                 storyboard_choices=resolved)
    project['version_history'][0]['summary'] = f'Imported {len(scenes)} storyboard scenes.'
    cut._set_duration(project)
    cut.validate_manifest(project)
    return project
