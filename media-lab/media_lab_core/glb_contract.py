"""Bounded container checks for self-contained generated GLB results.
Geometry decoding and finite-coordinate checks remain the worker's responsibility.
"""
import json
import struct

MAX_BYTES=64*1024**2


def inspect_generated_glb(data):
    if not isinstance(data,bytes) or not 20<=len(data)<=MAX_BYTES:
        raise ValueError('GLB result exceeds container bounds.')
    magic,version,total=struct.unpack_from('<4sII',data)
    if magic!=b'glTF' or version!=2 or total!=len(data):
        raise ValueError('Invalid GLB header.')
    chunks=[];offset=12
    while offset<len(data):
        if offset+8>len(data):raise ValueError('Truncated GLB chunk.')
        size,kind=struct.unpack_from('<II',data,offset);offset+=8
        if size%4 or offset+size>len(data):raise ValueError('Invalid GLB chunk length.')
        chunks.append((kind,data[offset:offset+size]));offset+=size
        if len(chunks)>2:raise ValueError('Unexpected GLB chunks.')
    if len(chunks)!=2 or [c[0] for c in chunks]!=[0x4e4f534a,0x004e4942]:
        raise ValueError('Expected JSON and embedded binary chunks.')
    if len(chunks[0][1])>1024**2:raise ValueError('GLB metadata exceeds bounds.')
    try:meta=json.loads(chunks[0][1])
    except (ValueError,UnicodeDecodeError):raise ValueError('Invalid GLB metadata.') from None
    if not isinstance(meta,dict) or not isinstance(meta.get('asset'),dict) or meta['asset'].get('version')!='2.0':
        raise ValueError('Invalid glTF asset metadata.')
    if meta.get('extensionsUsed') or meta.get('extensionsRequired'):
        raise ValueError('Generated mesh extensions require separate review.')
    buffers=meta.get('buffers')
    if not isinstance(buffers,list) or len(buffers)!=1 or not isinstance(buffers[0],dict) or 'uri' in buffers[0]:
        raise ValueError('Expected one embedded buffer.')
    size=buffers[0].get('byteLength')
    if type(size)!=int or size<=0 or not size<=len(chunks[1][1])<=size+3:
        raise ValueError('Embedded buffer size mismatch.')
    images=meta.get('images',[])
    if not isinstance(images,list) or len(images)>1000:raise ValueError('Invalid image list.')
    for image in images:
        if not isinstance(image,dict) or 'uri' in image or type(image.get('bufferView'))!=int:
            raise ValueError('Images must use embedded buffer views.')
    views=meta.get('bufferViews',[])
    if not isinstance(views,list) or len(views)>10000:raise ValueError('Invalid buffer views.')
    for view in views:
        if not isinstance(view,dict):raise ValueError('Invalid buffer view.')
        start=view.get('byteOffset',0);length=view.get('byteLength')
        if view.get('buffer')!=0 or type(start)!=int or type(length)!=int or start<0 or length<=0 or start+length>size:
            raise ValueError('Buffer view exceeds embedded data.')
    if any(not 0<=image['bufferView']<len(views) for image in images):
        raise ValueError('Image buffer view does not exist.')
    if not isinstance(meta.get('meshes'),list) or not meta['meshes']:
        raise ValueError('GLB contains no mesh.')
    return {'bytes':len(data),'mesh_count':len(meta['meshes']),'embedded_bytes':size,'self_contained':True}
