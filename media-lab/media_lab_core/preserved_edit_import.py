"""Copy a host-preserved edit into one paired owner's project list."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from . import cut


def import_edit(preserved, owned, project_id, media_root):
    source=cut.project_dir(preserved,project_id)
    target=cut.project_dir(owned,project_id)
    content={}
    for name in ('project.json','journal.jsonl'):
        path=source/name
        if path.is_symlink() or path.stat().st_size>8*1024**2:
            raise ValueError('preserved edit is not a bounded regular project')
        content[name]=path.read_bytes()
    digest=hashlib.sha256(content['project.json']+b'\0'+content['journal.jsonl']).hexdigest()
    project=json.loads(content['project.json']);cut.validate_manifest(project)
    if project['project_id']!=project_id:raise ValueError('preserved project identity differs')
    for asset in project.get('assets',[]):
        path=cut._asset_file(Path(media_root),asset)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=asset['source']['sha256']:
                raise ValueError('preserved edit source changed')
    target.parent.mkdir(parents=True,exist_ok=True)
    with (target.parent/'.preserved-import.lock').open('a+b') as lock:
        with cut._exclusive_file_lock(lock):
            if target.exists():
                receipt=target/'preserved-import.json'
                if not receipt.is_file() or json.loads(receipt.read_text()).get('sourceSha256')!=digest:
                    raise ValueError('another project already uses this identity')
                return cut.open_project(owned,project_id).load()
            temporary=Path(tempfile.mkdtemp(prefix='.import-',dir=target.parent))
            try:
                for name,raw in content.items():
                    fd=os.open(temporary/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                cut._atomic_write(temporary/'preserved-import.json',{'sourceSha256':digest})
                os.rename(temporary,target)
            finally:
                if temporary.exists():shutil.rmtree(temporary)
    return cut.open_project(owned,project_id).load()
