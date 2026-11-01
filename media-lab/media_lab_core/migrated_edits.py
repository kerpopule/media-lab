"""Adapt preserved Cut project snapshots and journals into a separate project root."""
import hashlib
import json
import os
from pathlib import Path
from . import cut


def migrate_edits(preservation, destination):
    source=Path(preservation).resolve(strict=True)
    destination=Path(destination).absolute()
    if destination.resolve().is_relative_to(source) or source.is_relative_to(destination.resolve()):
        raise ValueError('editing destination must be separate from preservation storage')
    receipt=json.loads((source/'migration-receipt.json').read_text())
    if not receipt.get('stagedCopyComplete'):
        raise ValueError('preservation copy is incomplete')
    mapping={row['originalPath']:row for row in receipt['files']}
    paths={'/'+old:'/media/'+row['path'] for old,row in mapping.items() if old.startswith('media/')}
    def adapt(value):
        if isinstance(value,dict):return {k:adapt(v) for k,v in value.items()}
        if isinstance(value,list):return [adapt(v) for v in value]
        return paths.get(value,value) if isinstance(value,str) else value
    def read(row):
        path=(source/row['path']).resolve()
        if not path.is_relative_to(source):raise ValueError('preservation path escapes storage')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('preserved editing data changed')
        return raw
    def verify(project):
        cut.validate_manifest(project)
        for asset in project.get('assets',[]):
            file=cut._asset_file(source,asset)
            with file.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=asset['source']['sha256']:
                    raise ValueError('editing source fingerprint does not match preserved media')
    prepared=[]
    for old,row in mapping.items():
        if not old.startswith('cut/projects/') or not old.endswith('/project.json'):continue
        project=adapt(json.loads(read(row)));verify(project)
        original_id=Path(old).parent.name
        if project['project_id']!=original_id:raise ValueError('editing project ID does not match its directory')
        journal_row=mapping.get(str(Path(old).with_name('journal.jsonl')))
        records=[]
        if journal_row:
            for line in read(journal_row).decode('utf-8').splitlines():
                if not line.strip():continue
                record=adapt(json.loads(line))
                for key in ('project_before','project_after'):
                    if record.get(key):verify(record[key])
                records.append(record)
        prepared.append((project,records))
    destination.mkdir(mode=0o700)  # Never merge into existing editing state.
    results=[]
    for project,records in prepared:
        directory=cut.project_dir(destination,project['project_id']);directory.mkdir(parents=True,mode=0o700)
        for name,data in [('project.json',json.dumps(project)),('journal.jsonl',''.join(json.dumps(r)+'\n' for r in records))]:
            fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        restored=cut.open_project(destination,project['project_id']).load()
        verify(restored)
        results.append({'projectId':project['project_id'],'revision':restored['revision'],'journalRecords':len(records)})
    return results
