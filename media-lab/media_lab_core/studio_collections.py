"""Scoped read access to saved characters, voices and storyboards."""
from fastapi import APIRouter, Depends, HTTPException, Request
import hashlib
import json

NAMES = ('characters', 'voices', 'storyboards')


def source_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def router(load_collections, authorize, library_assets=None):
    def permitted(request: Request):
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or not authorize(header[7:]):
            raise HTTPException(401, 'Connect Library to view saved characters and storyboards.')

    api = APIRouter(prefix='/api/studio/collections', dependencies=[Depends(permitted)])

    def records(name):
        if name not in NAMES:
            raise HTTPException(404, 'This collection is unavailable.')
        try:
            data = load_collections()
            values = data.get(name, [])
            if not isinstance(values, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in values):
                raise ValueError('invalid collection')
            issues = data.get('relationshipIssues', [])
            if not isinstance(issues, list):
                raise ValueError('invalid relationship issues')
            values = [{**row, 'sourceSha256': source_digest(row), 'relationshipIssues': [
                {key: issue[key] for key in ('field', 'targetId', 'reason')}
                for issue in issues if isinstance(issue, dict)
                and issue.get('recordId') == row['id']
                and all(isinstance(issue.get(key), str) for key in ('field', 'targetId', 'reason'))
            ]} for row in values]
            if library_assets is None:
                return values
            available = library_assets()
            def linked(value, found):
                if isinstance(value, dict):
                    for child in value.values(): linked(child, found)
                elif isinstance(value, list):
                    for child in value: linked(child, found)
                elif isinstance(value, str):
                    from urllib.parse import unquote
                    asset = available.get(unquote(value))
                    if asset is not None:
                        found[asset['id']] = asset
            output = []
            for row in values:
                found = {}
                linked(row, found)
                enriched = {**row, 'libraryAssets': list(found.values())}
                if name == 'storyboards' and isinstance(row.get('beats'), list):
                    from urllib.parse import unquote
                    song = available.get(unquote(row['song_url'])) if isinstance(row.get('song_url'), str) else None
                    if song is not None and song.get('kind') == 'audio':
                        enriched['soundtrackAsset'] = song
                    beats = []
                    for beat in row['beats']:
                        if not isinstance(beat, dict):
                            beats.append(beat)
                            continue
                        links = []
                        for field in ('still_url', 'clip_url', 'assembly_source_url', 'poster', 'thumbnail_url'):
                            value = beat.get(field)
                            asset = available.get(unquote(value)) if isinstance(value, str) else None
                            if asset is not None:
                                links.append({'field': field, **asset})
                        beats.append({**beat, 'mediaLinks': links})
                    enriched['beats'] = beats
                output.append(enriched)
            return output
        except (OSError, ValueError, TypeError, AttributeError):
            raise HTTPException(503, 'Saved collections could not be read. Check server storage.') from None

    @api.get('/{name}')
    def listing(name: str):
        return {'version': 1, 'collection': name, 'records': records(name)}

    @api.get('/{name}/{record_id}')
    def detail(name: str, record_id: str):
        row = next((row for row in records(name) if row['id'] == record_id), None)
        if row is None:
            raise HTTPException(404, 'This saved record is unavailable.')
        return {'version': 1, 'collection': name, 'record': row}

    return api
