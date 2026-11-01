import ast
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import web_only_policy as policy

ROOT=Path(__file__).resolve().parents[1]
class Policy(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'MEDIA_LAB_WEB_ONLY':'1','MEDIA_LAB_DISABLE_BACKGROUND_WORKERS':'1'});self.env.start()
        self.app=FastAPI();self.called=[]
        @self.app.get('/library')
        def library():return {'online':True}
        @self.app.post('/api/generate')
        def generate():self.called.append('gpu');return {'bad':'ran'}
        @self.app.post('/api/gate')
        def gate():self.called.append('gate');return {'ok':True}
        policy.install(self.app)
        self.client=TestClient(self.app)
    def tearDown(self):self.env.stop()
    def test_read_available(self):
        response=self.client.get('/library');self.assertEqual(response.status_code,200);self.assertEqual(response.headers['x-media-lab-compute-mode'],'web-only')
    def test_compute_fails_before_handler(self):
        response=self.client.post('/api/generate',json={'model':'H3'});self.assertEqual(response.status_code,503);self.assertEqual(self.called,[])
    def test_signin_preserved(self):self.assertEqual(self.client.post('/api/gate').status_code,200);self.assertEqual(self.called,['gate'])
    def test_dangerous_writes_all_denied(self):
        for path in ['/api/residency/apply','/api/maestro','/api/image','/api/chat','/api/providers','/api/studio/jobs','/api/setup/install','/api/admin/retry']:
            self.assertTrue(policy.mutation_blocked('POST',path),path)
    def test_normal_mode_unchanged(self):
        with patch.dict(os.environ,{'MEDIA_LAB_WEB_ONLY':'0'}):self.assertEqual(self.client.post('/api/generate').status_code,200)
    def test_mode_truthful(self):
        d=self.client.get('/api/compute-mode').json();self.assertTrue(d['web_only']);self.assertFalse(d['spark_gpu_enabled']);self.assertFalse(d['background_workers_enabled'])
    def test_auth_still_outermost(self):
        @self.app.middleware('http')
        async def auth(request,call_next):
            from fastapi.responses import JSONResponse
            if request.headers.get('x-fixture-auth')!='yes':return JSONResponse({'auth':'required'},status_code=401)
            return await call_next(request)
        c=TestClient(self.app);self.assertEqual(c.get('/library').status_code,401);self.assertEqual(c.post('/api/generate').status_code,401);self.assertEqual(c.post('/api/generate',headers={'x-fixture-auth':'yes'}).status_code,503)

class Lifespan(unittest.TestCase):
    def context(self,web):
        source=(ROOT/'app.py').read_text();tree=ast.parse(source)
        fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_studio_lifespan')
        names=['reap_orphan_maestro_runners','_start_studio_background_host','_gpu_write_handoff_on_shutdown']
        namespace={'os':os,'asynccontextmanager':asynccontextmanager,**{n:Mock() for n in names}}
        stopped=Mock()
        async def stop():stopped()
        namespace['_stop_studio_background_host']=stop
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'lifespan-fixture','exec'),namespace)
        async def exercise():
            async with namespace['_studio_lifespan'](None):pass
        with patch.dict(os.environ,{'MEDIA_LAB_WEB_ONLY':'1' if web else '0'}):asyncio.run(exercise())
        return namespace,stopped
    def test_web_never_touches_gpu_or_background(self):
        namespace,stopped=self.context(True)
        for name in ['reap_orphan_maestro_runners','_start_studio_background_host','_gpu_write_handoff_on_shutdown']:namespace[name].assert_not_called()
        stopped.assert_not_called()
    def test_original_lifecycle_kept(self):
        namespace,stopped=self.context(False)
        for name in ['reap_orphan_maestro_runners','_start_studio_background_host']:namespace[name].assert_called_once()
        # Current main hands warm residency on during a graceful normal-mode stop.
        namespace['_gpu_write_handoff_on_shutdown'].assert_called_once()
        stopped.assert_called_once()
    def test_recovery_is_conditional(self):
        tree=ast.parse((ROOT/'app.py').read_text())
        nodes=[n for n in tree.body if isinstance(n,ast.If) and 'RESIDENCY.recover()' in ast.unparse(n)]
        self.assertEqual(len(nodes),1);self.assertIn("MEDIA_LAB_WEB_ONLY",ast.unparse(nodes[0].test))
    def test_mode_banner_installed(self):
        html=(ROOT/'static/index.html').read_text();self.assertEqual(html.count('/static/web-only-mode.js'),1)
    def test_service_blocks_cuda_and_worker_start(self):
        text=(ROOT/'systemd/media-lab-web.service').read_text()
        for value in ['MEDIA_LAB_WEB_ONLY=1','MEDIA_LAB_DISABLE_BACKGROUND_WORKERS=1','CUDA_VISIBLE_DEVICES=-1','Conflicts=media-lab-simple.service','MemoryMax=1536M','Before=media-lab-simple.service']:self.assertIn(value,text)

if __name__=='__main__':unittest.main(verbosity=2)
