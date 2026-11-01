import numpy as np
import pytest
from media_lab_core.mesh_order import canonical_mesh_arrays


def test_reordering_preserves_geometry_colors_and_winding():
    v=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]])
    f=np.array([[0,1,2],[0,3,1]])
    c=np.array([[10,20,30,255],[40,50,60,255],[70,80,90,255],[100,110,120,255]],dtype=np.uint8)
    permutation=np.array([2,0,3,1]);inverse=np.argsort(permutation)
    reordered=inverse[f[::-1]][:,[1,2,0]]
    a=canonical_mesh_arrays(v,f,c);b=canonical_mesh_arrays(v[permutation],reordered,c[permutation])
    assert all(np.array_equal(x,y) for x,y in zip(a,b))
    original_normals=np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
    normals=np.cross(a[0][a[1][:,1]]-a[0][a[1][:,0]],a[0][a[1][:,2]]-a[0][a[1][:,0]])
    assert sorted(map(tuple,normals))==sorted(map(tuple,original_normals))


def test_invalid_indices_and_ambiguous_duplicate_vertices_are_rejected():
    v=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
    c=np.zeros((3,4),dtype=np.uint8)
    with pytest.raises(ValueError,match='outside'):
        canonical_mesh_arrays(v,np.array([[0,1,3]]),c)
    v[1]=v[0]
    with pytest.raises(ValueError,match='Duplicate'):
        canonical_mesh_arrays(v,np.array([[0,1,2]]),c)
