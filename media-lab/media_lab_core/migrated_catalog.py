"""Build a Studio catalog from a verified preservation copy, keeping original IDs."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def prepare_catalog(directory):
    root = Path(directory).resolve(strict=True)
    receipt = json.loads((root/'migration-receipt.json').read_text())
    if not receipt.get('stagedCopyComplete'):
        raise ValueError('preservation copy is incomplete')
    mapping = {row['originalPath']: row for row in receipt['files']}
    external = root/'external-reference-receipt.json'
    if external.exists():
        extra = json.loads(external.read_text())
        if not extra.get('complete'):
            raise ValueError('external preservation copy is incomplete')
        for entry in extra['files']:
            old = entry['originalPath']
            if old in mapping and mapping[old] != entry:
                raise ValueError('conflicting preserved source paths')
            mapping[old] = entry
    original = mapping['gallery.json']
    metadata = (root / original['path']).resolve()
    if not metadata.is_relative_to(root):
        raise ValueError('metadata escapes storage')
    raw = metadata.read_bytes()
    if hashlib.sha256(raw).hexdigest() != original['sha256']:
        raise ValueError('preserved gallery metadata changed')
    rows = json.loads(raw)
    out = []
    seen = set()
    for row in rows:
        item = dict(row)
        identity = item.get('id')
        if not isinstance(identity, str) or identity in seen:
            raise ValueError('gallery has missing or duplicate IDs')
        seen.add(identity)
        for field in ('url', 'poster'):
            value = item.get(field)
            if not value:
                continue
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path.startswith('/media/'):
                raise ValueError('gallery reference needs external import')
            old = unquote(url.path).lstrip('/')
            entry = mapping.get(old)
            if not entry:
                raise ValueError('gallery references an uncopied file')
            path = (root / entry['path']).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('copied gallery file is missing or escapes storage')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                    raise ValueError('copied gallery file changed')
            item[field] = '/media/' + quote(entry['path'], safe='/')
            if field == 'url':
                item['folder'] = str(Path(entry['path']).parent)
                item['originalUrl'] = value
        out.append(item)
    # The old gallery was capped and omitted existing files. Keep its original
    # identities, then expose the remaining preserved media with deterministic IDs.
    offered = {unquote(row.get('url', '')[7:]) for row in out if row.get('url', '').startswith('/media/')}
    supported = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.mp4', '.webm',
                 '.mov', '.mkv', '.mp3', '.wav', '.flac', '.m4a', '.ogg', '.glb'}
    for old, entry in mapping.items():
        if Path(entry['path']).suffix.lower() not in supported or entry['path'] in offered:
            continue
        path = (root/entry['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('preserved media is missing or escapes storage')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                raise ValueError('preserved media changed')
        identity = 'preserved-' + hashlib.sha256(old.encode()).hexdigest()[:32]
        if identity in seen:
            raise ValueError('preserved media ID collides with gallery ID')
        seen.add(identity)
        offered.add(entry['path'])
        out.append({'id':identity, 'title':Path(old).stem, 'url':'/media/'+quote(entry['path'], safe='/'),
                    'folder':str(Path(entry['path']).parent), 'originalUrl':old if old.startswith(('/', 'https://', 'http://')) else '/'+old,
                    'status':'done', 'engine':'Preserved library file', 'ts':0})
    return out


def prepare_collections(directory):
    """Rewrite exact preserved file references while retaining all semantic IDs.

    Missing ID relationships are reported, never guessed or replaced. Source
    metadata remains untouched alongside these adapted collections.
    """
    root = Path(directory).resolve(strict=True)
    receipt = json.loads((root/'migration-receipt.json').read_text())
    if not receipt.get('stagedCopyComplete'):
        raise ValueError('preservation copy is incomplete')
    mapping = {row['originalPath']: row for row in receipt['files']}
    external = root/'external-reference-receipt.json'
    if external.exists():
        extra = json.loads(external.read_text())
        if not extra.get('complete'):
            raise ValueError('external preservation copy is incomplete')
        mapping.update({row['originalPath']: row for row in extra['files']})
    replacements = {}
    for old, row in mapping.items():
        new = '/media/' + quote(row['path'], safe='/')
        replacements[old] = new
        if old.startswith('media/'):
            replacements['/'+old] = new
    rewritten = 0
    def adapt(value):
        nonlocal rewritten
        if isinstance(value, dict):
            return {key: adapt(child) for key, child in value.items()}
        if isinstance(value, list):
            return [adapt(child) for child in value]
        if isinstance(value, str) and value in replacements:
            rewritten += 1
            return replacements[value]
        return value
    collections = {}
    def read_records(original_path):
        row = mapping.get(original_path)
        if row is None:
            return []
        path = (root/row['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('metadata escapes storage')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('preserved collection metadata changed')
        values = json.loads(raw)
        if not isinstance(values, list) or any(not isinstance(v, dict) or not isinstance(v.get('id'), str) or not v['id'] for v in values):
            raise ValueError('collection has invalid records')
        if len({v['id'] for v in values}) != len(values):
            raise ValueError('collection has duplicate IDs')
        return values
    for name in ('characters', 'voices', 'storyboards'):
        collections[name] = adapt(read_records(name+'.json'))
    archived = read_records('archive/characters/archived.json')
    active_ids = {row['id'] for row in collections['characters']}
    for original in archived:
        if original['id'] in active_ids:
            raise ValueError('character ID appears in active and archived collections')
        row = dict(original)
        # Legacy archive moved the character sheet without changing sheet_url.
        # Resolve only the exact ID-derived sheet recorded in the receipt.
        sheet = 'char_'+row['id']+'.png'
        preserved = mapping.get('archive/characters/'+sheet)
        if preserved and row.get('sheet_url') == '/media/'+sheet:
            row['sheet_url'] = '/media/'+quote(preserved['path'], safe='/')
            rewritten += 1
        row['archived'] = True
        collections['characters'].append(adapt(row))
    characters = {x['id'] for x in collections['characters']}
    voices = {x['id'] for x in collections['voices']}
    issues = []
    def check(owner, field, target, allowed):
        if target and target not in allowed:
            issues.append({'recordId':owner, 'field':field, 'targetId':target,
                           'reason':'target_not_in_preserved_collection'})
    for row in collections['characters']:
        if row.get('archived'):
            for index, reference in enumerate(row.get('refs', [])):
                if isinstance(reference, str) and reference.startswith('/media/'):
                    asset = (root/unquote(reference[7:])).resolve()
                    if not asset.is_relative_to(root) or not asset.is_file():
                        issues.append({'recordId':row['id'], 'field':f'refs/{index}',
                                       'targetId':reference, 'reason':'archived_reference_file_missing'})
        check(row['id'], 'voice_id', row.get('voice_id'), voices)
    for row in collections['voices']:
        check(row['id'], 'character_id', row.get('character_id'), characters)
    for row in collections['storyboards']:
        song_id = row.get('song_id')
        if song_id:
            song = replacements.get(f'media/{song_id}.mp3')
            if song:
                row['song_url'] = song
            else:
                issues.append({'recordId':row['id'], 'field':'song_id', 'targetId':song_id,
                               'reason':'song_file_not_in_preservation_copy'})
        for target in row.get('cast', []):
            check(row['id'], 'cast', target, characters)
        for index, beat in enumerate(row.get('beats', [])):
            for target in beat.get('cast', []):
                check(row['id'], f'beats/{index}/cast', target, characters)
    return {'version':1, **collections, 'rewrittenFileReferences':rewritten,
            'relationshipIssues':issues, 'migrationComplete':False}
