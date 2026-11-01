"""Stable ordering for bounded, vertex-colored triangle meshes.
Does not weld vertices, reverse winding, smooth geometry, or claim mesh quality.
"""
import numpy as np


def canonical_mesh_arrays(vertices, faces, colors):
    v=np.asarray(vertices);f=np.asarray(faces);c=np.asarray(colors)
    if v.ndim!=2 or v.shape[1]!=3 or not 1<=len(v)<=1000000 or not np.issubdtype(v.dtype,np.floating):
        raise ValueError('Expected bounded floating-point vertices.')
    if not np.isfinite(v).all():raise ValueError('Vertices must be finite.')
    if f.ndim!=2 or f.shape[1]!=3 or not 1<=len(f)<=2000000 or not np.issubdtype(f.dtype,np.integer):
        raise ValueError('Expected bounded integer triangles.')
    if f.min()<0 or f.max()>=len(v):raise ValueError('Triangle index is outside the mesh.')
    if c.shape!=(len(v),4) or c.dtype!=np.uint8:raise ValueError('Expected one RGBA byte color per vertex.')
    # Float64 exactly represents float32 coordinates and all byte colors.
    keys=np.column_stack((v,c))
    order=np.lexsort(keys.T[::-1])
    if len(order)>1 and np.any(np.all(keys[order][1:]==keys[order][:-1],axis=1)):
        raise ValueError('Duplicate vertex/color records require an explicit topology policy.')
    inverse=np.empty(len(order),dtype=np.int64);inverse[order]=np.arange(len(order))
    triangles=inverse[f]
    # Cyclic rotation preserves winding; sorting each triangle would not.
    starts=triangles.argmin(axis=1)
    triangles=np.take_along_axis(triangles,(np.arange(3)[None,:]+starts[:,None])%3,axis=1)
    triangles=triangles[np.lexsort(triangles.T[::-1])]
    return v[order].copy(),triangles,c[order].copy()
