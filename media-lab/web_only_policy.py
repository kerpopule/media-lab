"""Fail-closed web-only mode. Keeps existing authentication outside this policy."""
import json
import os
from pathlib import Path
import time
from fastapi.responses import JSONResponse

# Only existing sign-in, sign-out, and bounded Library upload/import writes remain.
SAFE_WRITES = frozenset({('POST','/api/gate'),('POST','/api/signout'),('POST','/api/upload'),('POST','/api/import'),('POST','/api/push/subscribe'),('POST','/api/push/unsubscribe')})

def web_only():
    return os.environ.get('MEDIA_LAB_WEB_ONLY') == '1'

def mutation_blocked(method,path):
    return web_only() and method.upper() not in ('GET','HEAD','OPTIONS') and (method.upper(),path) not in SAFE_WRITES

def install(app):
    @app.middleware('http')
    async def protect(request,call_next):
        if mutation_blocked(request.method,request.url.path):
            return JSONResponse({'ok':False,'error':'Spark rendering and compute mutations are paused while GLM owns the pair. Media Lab browsing and Library remain online.','code':'spark_gpu_reserved','web_only':True},status_code=503)
        response=await call_next(request)
        if web_only(): response.headers['X-Media-Lab-Compute-Mode']='web-only'
        return response

    @app.get('/api/compute-mode')
    async def compute_mode():
        result={'web_only':web_only(),'spark_gpu_enabled':not web_only(),'background_workers_enabled':os.environ.get('MEDIA_LAB_DISABLE_BACKGROUND_WORKERS')!='1'}
        path=Path.home()/'media-lab-simple/offline-images/mac-worker.json'
        if path.is_file():
            worker=json.loads(path.read_text())
            result['mac_worker']={**worker,'online':time.time()-worker.get('seen',0)<60}
        else: result['mac_worker']={'online':False,'state':'not-seen'}
        return result
