import copy

import pytest

from media_lab_core import cut
from media_lab_core.storyboard_edit import build_storyboard_project


def test_repeated_sources_and_twenty_scenes_keep_order_and_timing(cut_media):
    source = cut.probe_gallery_file(cut_media['a'])
    before = copy.deepcopy(source)
    scenes = [{'asset_id': 'video', 'seconds': '0.333', 'label': f'Shot {i}', 'narration': f'Line {i}'} for i in range(20)]
    project = build_storyboard_project('cut-storyboard', 'Film', scenes, {'video': source}, storyboard_id='old-board')
    clips = project['timeline']['tracks'][0]['clips']
    assert len(clips) == len(project['storyboard']['scenes']) == 20
    assert len({clip['id'] for clip in clips}) == 20
    assert [clip['label'] for clip in clips] == [f'Shot {i}' for i in range(20)]
    assert project['duration_frames'] == round(20 * .333 * 24)
    assert all(a['source']['sha256'] == source['sha256'] for a in project['assets'])
    assert source == before
    assert project['storyboard']['scenes'][19]['narration'] == 'Line 19'
    assert project == build_storyboard_project('cut-storyboard', 'Film', scenes, {'video': source}, storyboard_id='old-board')


def test_still_duration_is_explicit_and_missing_scenes_are_not_skipped(cut_media):
    still = cut.probe_gallery_file(cut_media['c'])
    scenes = [{'asset_id': 'image', 'seconds': '5'}]
    project = build_storyboard_project('cut-still', 'Still', scenes, {'image': still}, storyboard_id='board')
    assert project['duration_frames'] == 120
    with pytest.raises(cut.CutError, match='Scene 2'):
        build_storyboard_project('cut-still', 'Still', scenes + [{'asset_id': 'missing', 'seconds': 5}], {'image': still}, storyboard_id='board')
    for invalid in [None, True, '5 seconds', '1e3', 0, float('nan')]:
        with pytest.raises(cut.CutError, match='Scene 1'):
            build_storyboard_project('cut-still', 'Still', [{'asset_id': 'image', 'seconds': invalid}], {'image': still}, storyboard_id='board')


def test_short_video_is_not_silently_stretched(cut_media):
    source = cut.probe_gallery_file(cut_media['a'])
    with pytest.raises(cut.CutError, match='shorter'):
        build_storyboard_project('cut-short', 'Film', [{'asset_id': 'video', 'seconds': 20}], {'video': source}, storyboard_id='board')


def test_converted_video_and_still_render_at_the_saved_length(cut_media, tmp_path):
    sources = {key: cut.probe_gallery_file(cut_media[key]) for key in ('a', 'c')}
    project = build_storyboard_project('cut-render', 'Mixed storyboard', [
        {'asset_id': 'a', 'seconds': '0.5'},
        {'asset_id': 'c', 'seconds': '0.75'},
        {'asset_id': 'a', 'seconds': '0.5'},
    ], sources, storyboard_id='original')
    receipt = cut.render_timeline(project, media_dir=cut_media['dir'], output=tmp_path/'storyboard.mp4', work_dir=tmp_path/'work',
                                  export_request=cut.validate_export_request(project, {'quality': 'preview', 'format': 'mp4'}))
    assert receipt['expected_seconds'] == 1.75
    assert abs(float(receipt['ffprobe']['format']['duration']) - 1.75) < .1
    assert len(receipt['sha256']) == 64


def test_soundtrack_stops_at_visual_end_without_changing_its_source(cut_media, tmp_path):
    sources = {key: cut.probe_gallery_file(cut_media[key]) for key in ('c', 'm')}
    before = copy.deepcopy(sources)
    project = build_storyboard_project('cut-song', 'Music storyboard', [{'asset_id': 'c', 'seconds': 1.25}], sources,
                                       storyboard_id='original', music_asset_id='m')
    music = project['timeline']['tracks'][1]['clips'][0]
    assert music['duration_frames'] == music['trim_out_frame'] == project['duration_frames'] == 30
    assert project['provenance']['storyboard_music']['library_asset_id'] == 'm'
    assert sources == before
    receipt = cut.render_timeline(project, media_dir=cut_media['dir'], output=tmp_path/'song.mp4', work_dir=tmp_path/'work',
                                  export_request=cut.validate_export_request(project, {'quality': 'preview', 'format': 'mp4'}))
    assert any(stream['codec_type'] == 'audio' for stream in receipt['ffprobe']['streams'])
    assert abs(float(receipt['ffprobe']['format']['duration']) - 1.25) < .1
