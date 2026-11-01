"""Verified local checkpoint loading; no downloading or dynamic class imports.
Callers supply reviewed constructors and a SafeTensors reader. This is not
model, license, platform or concurrent-filesystem qualification.
"""
import hashlib
import json
from pathlib import Path
import re


def verified_path(root, entry, limit):
    name=entry['path']
    if not isinstance(name,str) or '\\' in name or any(p in ('','.','..') for p in name.split('/')):
        raise ValueError('Use a relative checkpoint file path.')
    size=entry['bytes'];digest=entry['sha256']
    if type(size) is not int or not 0<size<=limit or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
        raise ValueError('Invalid checkpoint identity.')
    path=root
    for part in name.split('/'):
        path=path/part
        if path.is_symlink():raise ValueError('Checkpoint paths must not contain symlinks.')
    if not path.is_file() or path.stat().st_size!=size:raise ValueError('Checkpoint file missing or changed.')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:raise ValueError('Checkpoint hash changed.')
    return path


def _verify_model(root, descriptor, constructors):
    config=verified_path(root,descriptor['config'],64*1024)
    weights=verified_path(root,descriptor['weights'],32*1024**3)
    if weights.suffix!='.safetensors':raise ValueError('Use a reviewed SafeTensors checkpoint.')
    options=json.loads(config.read_text())
    if not isinstance(options,dict) or set(options)!={'name','args'} or not isinstance(options['args'],dict):
        raise ValueError('Invalid model configuration.')
    name=options['name']
    if not isinstance(name,str) or name!=descriptor['model_class'] or name not in constructors:
        raise ValueError('Model constructor is not reviewed.')
    if not callable(constructors[name]):raise ValueError('Model constructor is not callable.')
    return options,weights


def _construct(verified, constructors, read_safetensors):
    options,weights=verified
    model=constructors[options['name']](**options['args'])
    model.load_state_dict(read_safetensors(str(weights)),strict=True)
    model.eval()
    return model


def load_verified_model(root, descriptor, constructors, read_safetensors):
    verified=_verify_model(Path(root).resolve(strict=True),descriptor,constructors)
    return _construct(verified,constructors,read_safetensors)


def load_verified_components(root, descriptors, expected_roles, constructors, read_safetensors):
    """Verify every required file before allocating any component model."""
    if not isinstance(descriptors,list) or not 0<len(descriptors)<=32:
        raise ValueError('Choose a bounded reviewed component set.')
    roles=[row.get('role') for row in descriptors]
    if any(not isinstance(role,str) or not role for role in roles):
        raise ValueError('Each model component requires a role.')
    if len(set(roles))!=len(roles) or set(roles)!=set(expected_roles):
        raise ValueError('Model component roles differ from the reviewed pipeline.')
    root=Path(root).resolve(strict=True)
    verified=[_verify_model(root,row,constructors) for row in descriptors]
    return {role:_construct(value,constructors,read_safetensors) for role,value in zip(roles,verified)}
