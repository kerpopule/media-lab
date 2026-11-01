"""Prepare a reviewed offline-only development source tree; never imports it."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

MANIFEST=Path(__file__).resolve().parents[1]/'docs/triposr-source-review.json'


def replace_once(text, old, new):
    if text.count(old)!=1:raise ValueError('Reviewed source anchor changed.')
    return text.replace(old,new,1)


def prepare(source, config, output):
    source,config,output=Path(source),Path(config),Path(output)
    if output.exists() or output.is_symlink():raise ValueError('Choose a new source directory.')
    manifest=json.loads(MANIFEST.read_text())
    verified={}
    for row in manifest['source']['files']:
        path=source/row['path']
        if path.is_symlink():raise ValueError('Source links are not accepted.')
        data=path.read_bytes()
        if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise ValueError('Source hash changed: '+row['path'])
        verified[row['path']]=data
    data=config.read_bytes();expected=manifest['image_encoder_config']
    if config.is_symlink() or len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
        raise ValueError('DINO configuration changed.')
    image=verified['tsr/models/tokenizers/image.py'].decode()
    image=replace_once(image,'from huggingface_hub import hf_hub_download','from pathlib import Path')
    start=image.index('            ViTModel.config_class.from_pretrained(')
    end=image.index('\n        )',start)
    image=image[:start]+'''            ViTModel.config_class.from_json_file(
                str(Path(__file__).resolve().parents[3] / "dino-config.json")
            )'''+image[end:]
    verified['tsr/models/tokenizers/image.py']=image.encode()
    system=verified['tsr/system.py'].decode()
    system=replace_once(system,'from huggingface_hub import hf_hub_download\n','')
    start=system.index('    @classmethod\n    def from_pretrained(')
    end=system.index('\n    def configure(',start)
    system=system[:start]+'''    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        raise RuntimeError("Use the verified local SafeTensors adapter.")
'''+system[end:]
    verified['tsr/system.py']=system.encode()
    utils=verified['tsr/utils.py'].decode()
    utils=replace_once(utils,'import rembg\n','')
    utils=replace_once(utils,'        image = rembg.remove(image, session=rembg_session, **rembg_kwargs)',
                       '        raise RuntimeError("Provide a cutout from the qualified background-removal adapter.")')
    verified['tsr/utils.py']=utils.encode()
    for name,payload in verified.items():
        if name.endswith('.py'):ast.parse(payload,filename=name)
    temporary=Path(tempfile.mkdtemp(dir=output.parent,prefix='.triposr-source-'))
    try:
        for name,payload in verified.items():
            path=temporary/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
        (temporary/'dino-config.json').write_bytes(data)
        receipt={'version':1,'qualified':False,'source_revision':manifest['source']['revision'],
                 'modifications':['local DINO config','disable legacy checkpoint loader','explicit external cutout preparation'],
                 'files':{name:hashlib.sha256(payload).hexdigest() for name,payload in verified.items()}}
        (temporary/'vibex-source-modifications.json').write_text(json.dumps(receipt,indent=2))
        if output.exists():raise ValueError('Destination appeared while preparing.')
        os.rename(temporary,output)
        return receipt
    finally:
        if temporary.exists():shutil.rmtree(temporary)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','config','output'):parser.add_argument(name,type=Path)
    args=parser.parse_args()
    print(json.dumps(prepare(args.source,args.config,args.output),indent=2))
