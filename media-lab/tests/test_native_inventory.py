from types import SimpleNamespace
from pathlib import Path
import pytest
from media_lab_core.native_inventory import collect, parse_dynamic


def test_dynamic_metadata_preserves_needed_and_search_paths():
    text=''' 0x0001 (NEEDED) Shared library: [libtorch.so]
 0x0001 (NEEDED) Shared library: [libc.so.6]
 0x000e (SONAME) Library soname: [extension.so]
 0x001d (RUNPATH) Library runpath: [$ORIGIN/lib:/usr/lib]
'''
    assert parse_dynamic(text)=={'needed':['libtorch.so','libc.so.6'],'soname':['extension.so'],
                                 'rpath':[],'runpath':['$ORIGIN/lib:/usr/lib']}


def test_version_and_runtime_boundary_are_checked(tmp_path):
    outside=tmp_path/'outside';outside.write_bytes(b'plain text')
    root=tmp_path/'runtime';root.mkdir()
    package=SimpleNamespace(version='1',files=[Path('outside')],locate_file=lambda _:outside)
    with pytest.raises(ValueError,match='version'):
        collect([{'name':'sample','version':'2'}],root,lambda _:package)
    with pytest.raises(ValueError,match='escapes'):
        collect([{'name':'sample','version':'1'}],root,lambda _:package)
