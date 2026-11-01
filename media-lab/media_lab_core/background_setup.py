"""Development setup controller with fixed paths and reviewed-plan binding."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys
import subprocess
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from .birefnet_cpu import CPU_PLATFORMS, MANIFEST
from .background_install import install, digest, write_receipt
from .background_host import verify_receipt
from .background_lifecycle import lifecycle_slot
from .background_remove import remove


def python_abi(path):
    """Inspect the configured interpreter without site packages or model imports."""
    try:
        result = subprocess.run([str(path), '-I', '-S', '-c',
            'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")'],
            capture_output=True, text=True, timeout=5, check=True)
        value = result.stdout.strip()
        return value if value.startswith('cp') and value[2:].isdigit() else None
    except (OSError, subprocess.SubprocessError):
        return None


def managed_receipt(root, artifacts=None):
    """The fixed receipt path when the saved preference is enabled; never a path from the file."""
    path=Path(root)/'studio-background-enabled.json'
    artifacts=Path(artifacts) if artifacts is not None else Path(root)/'studio-artifacts'
    try:
        if path.is_symlink() or path.stat().st_size>4096:return None
        value=json.loads(path.read_text())
        if value.get('version')==1 and value.get('enabled') is True:
            return str(artifacts/'qualification.json')
    except (OSError,ValueError,AttributeError):pass
    return None


class Setup:
    def __init__(self, root, *, python=None, uv=None, execute=install, get_host=None, externally_controlled=lambda:False,
                 artifact_root=None, runtime_root=None):
        self.root=Path(root).resolve()
        # Legacy app: root/studio-artifacts + root/studio-runtimes. The independent
        # host passes its own fixed artifacts/runtimes directories explicitly so
        # the API, host and installer never mix stores.
        self.artifacts=Path(artifact_root) if artifact_root is not None else self.root/'studio-artifacts'
        self.runtimes=Path(runtime_root) if runtime_root is not None else self.root/'studio-runtimes'
        if not self.artifacts.is_absolute() or not self.runtimes.is_absolute():
            raise ValueError('Setup artifact and runtime roots must be absolute.')
        self.python=Path(python) if python else (Path(sys.executable) if sys.version_info[:2]==(3,12) else None)
        found=uv or shutil.which('uv')
        self.uv=Path(found) if found else None
        self.execute=execute
        self.get_host=get_host
        self.externally_controlled=externally_controlled
        self.operation=None
        self.thread=None
        self.lock=threading.Lock()
        self.error=None

    def installation_root(self):
        fingerprint=hashlib.sha256((digest(MANIFEST)+digest(MANIFEST.with_suffix('.requirements.lock'))).encode()).hexdigest()
        return self.runtimes/('birefnet-'+fingerprint[:16])

    def plan(self):
        import psutil
        manifest=json.loads(MANIFEST.read_text())
        lock=MANIFEST.with_suffix('.requirements.lock')
        directory=self.installation_root()
        reasons=[]
        if (platform.system(),platform.machine()) not in CPU_PLATFORMS:
            reasons.append('This model is currently qualified on macOS arm64 and Linux aarch64 CPU only.')
        for label,path in (('Python 3.12',self.python),('uv',self.uv)):
            if path is None or not path.is_file():reasons.append('Install '+label+' on this server first.')
        abi = python_abi(self.python) if self.python and self.python.is_file() else None
        if self.python and self.python.is_file() and abi != manifest['python_abi']:
            reasons.append('The configured Python could not be verified as Python 3.12. Select a working Python 3.12 interpreter on this server.')
        memory=psutil.virtual_memory()
        available=memory.available
        probe=self.root
        while not probe.exists():probe=probe.parent
        free=shutil.disk_usage(probe).free
        if available<12*1024**3:reasons.append('Free 12 GiB of memory before qualification.')
        if free<8*1024**3:reasons.append('Free an 8 GiB installation workspace reserve.')
        identity={'controller':{name:digest(Path(__file__).with_name(name)) for name in ('background_setup.py','background_install.py','background_host.py','background_lifecycle.py','runtime_inventory.py','background_remove.py')},
                  'notices':digest(MANIFEST.parent/'runtime-notices/manifest.json'),'manifest':digest(MANIFEST),'lock':digest(lock),'root':str(directory),
                  'artifacts':str(self.artifacts),
                  'python':str(self.python) if self.python else None,'uv':str(self.uv) if self.uv else None,
                  'python_sha256':digest(self.python) if self.python and self.python.is_file() else None,
                  'uv_sha256':digest(self.uv) if self.uv and self.uv.is_file() else None}
        plan_id=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        return {'version':1,'planId':plan_id,'capability':'remove-background','model':'BiRefNet',
                'revision':manifest['revision'],'modelLicense':'MIT','developmentOnly':True,
                'modelDownloadBytes':sum(item['bytes'] for item in [*manifest['files'],manifest['weight']]),
                'workspaceReserveBytes':8*1024**3,'memoryRequiredBytes':12*1024**3,
                'availableMemoryBytes':available,'totalMemoryBytes':getattr(memory,'total',None),'freeDiskBytes':free,'blockedReasons':reasons,
                'pythonAbi':abi,
                'installationRoot':str(directory),'artifactRoot':identity['artifacts'],
                'activation':'Installation qualifies the runtime; enabling the host is a separate action.'}

    def status(self):
        path=self.installation_root()/'install.json'
        record={}
        if path.is_file() and not path.is_symlink() and path.stat().st_size<65536:
            try:
                loaded=json.loads(path.read_text())
                if isinstance(loaded,dict):record=loaded
            except (OSError,ValueError):pass
        running=bool(self.thread and self.thread.is_alive())
        host=self.get_host() if self.get_host else None
        desired=bool(managed_receipt(self.root,self.artifacts))
        return {'version':1,'desiredEnabled':desired,'hostReady':bool(host and host.engines()),
                'hostActive':bool(host and host.thread and host.thread.is_alive()),'hostError':host.error if host and desired else None,
                'externallyControlled':self.externally_controlled(),'operation':self.operation if running else None,'runningHere':running,'runtimeRemoved':(self.artifacts/'runtime-disabled.json').exists(),'recordedStatus':record.get('status','not-installed'),
                'stage':record.get('stage'),'error':self.error,
                'note':'A recorded status is not proof that an engine is currently enabled.'}

    def start(self, plan_id, reinstall=False):
        with self.lock:
            if self.thread and self.thread.is_alive():raise ValueError('This setup is already running.')
            plan=self.plan()
            if plan_id!=plan['planId']:raise ValueError('The setup plan changed. Review it again.')
            if plan['blockedReasons']:raise ValueError(' '.join(plan['blockedReasons']))
            self.error=None
            self.operation='install'
            def run():
                try:
                    self.execute(root=Path(plan['installationRoot']),artifact_root=Path(plan['artifactRoot']),
                                 python=self.python,uv=self.uv,reinstall=reinstall)
                except Exception:
                    self.error='Setup stopped. Inspect the recorded stage and server install log before retrying.'
            self.thread=threading.Thread(target=run,name='studio-background-setup',daemon=True)
            self.thread.start()
        return {'accepted':True,'planId':plan_id}

    def activation(self, plan_id, enabled):
        with self.lock:
            if self.thread and self.thread.is_alive():raise ValueError('Another setup action is running.')
            if not self.get_host or self.externally_controlled():
                raise ValueError('This host is controlled by server configuration; change it there first.')
            if plan_id!=self.plan()['planId']:raise ValueError('The setup plan changed. Review it again.')
            host=self.get_host()
            self.error=None
            self.operation='enable' if enabled else 'disable'
            def run():
                try:
                    self._activation(host,enabled)
                except Exception as error:
                    self.error=str(error) if isinstance(error,ValueError) else 'The host could not change state. Check its qualification and active work.'
            self.thread=threading.Thread(target=run,name='studio-background-activation',daemon=True)
            self.thread.start()
        return {'accepted':True,'enabled':enabled}

    def remove(self, plan_id):
        with self.lock:
            if self.thread and self.thread.is_alive():raise ValueError('Another setup action is running.')
            if not self.get_host or self.externally_controlled():
                raise ValueError('This host is controlled by server configuration; change it there first.')
            if plan_id!=self.plan()['planId']:raise ValueError('The setup plan changed. Review it again.')
            host=self.get_host()
            if managed_receipt(self.root,self.artifacts) or (host.thread and host.thread.is_alive()):
                raise ValueError('Disable the model and let current work finish before removing it.')
            self.error=None
            self.operation='remove'
            def run():
                try:remove(self.installation_root(),execute=True)
                except Exception:
                    self.error='Removal stopped. Your creations are retained. Check active work and the installation receipt before retrying.'
            self.thread=threading.Thread(target=run,name='studio-background-removal',daemon=True)
            self.thread.start()
        return {'accepted':True,'planId':plan_id}

    def _activation(self, host, enabled):
        config=self.root/'studio-background-enabled.json'
        artifacts=self.artifacts
        active=bool(host.thread and host.thread.is_alive())
        if not enabled:
            # Persist the restart preference before draining; never cancel work.
            if active:
                write_receipt(config,{'version':1,'enabled':False})
                host.stop()
                if host.thread.is_alive():raise ValueError('Healthy work is still finishing. Wait and retry disabling.')
            else:
                with lifecycle_slot(artifacts):write_receipt(config,{'version':1,'enabled':False})
            host.receipt=None
            return
        receipt=artifacts/'qualification.json'
        if active:
            if host.stop_requested.is_set():raise ValueError('Wait for the host to finish disabling before enabling it again.')
            if host.receipt is None or str(host.receipt)!=str(receipt):raise ValueError('Disable the current host before switching its runtime.')
            write_receipt(config,{'version':1,'enabled':True})
            return
        with lifecycle_slot(artifacts):
            if (artifacts/'runtime-disabled.json').exists():raise ValueError('Reinstall the removed model before enabling it.')
            verified=verify_receipt(receipt)
            root=self.installation_root()
            record=json.loads((root/'install.json').read_text())
            if record.get('status')!='qualified' or digest(root/'runtime-inventory.json')!=record.get('inventory_sha256'):
                raise ValueError('Installation or notice evidence needs verification before enabling.')
            if (verified['runtime'],verified['package'],verified['cache'],verified['artifact_root']) != (
                    str(root/'runtime/bin/python'),str(root/'package'),str(root/'model-cache'),str(artifacts)):
                raise ValueError('Qualification does not match this installed model.')
            write_receipt(config,{'version':1,'enabled':True})
            host.receipt=str(receipt)
        host.start()  # Revalidates and advertises only after its thread owns the lifetime slot.



class Action(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    planId:str=Field(pattern=r'^[a-f0-9]{64}$')
    reinstall:bool=False


class Removal(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    planId:str=Field(pattern=r'^[a-f0-9]{64}$')


class Activation(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    planId:str=Field(pattern=r'^[a-f0-9]{64}$')
    enabled:bool


def router(get_setup, authorized, enabled):
    api=APIRouter()
    def require(request):
        if not authorized(request):raise HTTPException(403,'Sign in with the server admin code to manage model setup.')
        if not enabled():raise HTTPException(409,'Independent model setup is not enabled on this development host.')
    @api.get('/api/setup/background/plan')
    def plan(request:Request):
        require(request);return get_setup().plan()
    @api.get('/api/setup/background/status')
    def status(request:Request):
        require(request);return get_setup().status()
    @api.post('/api/setup/background/install',status_code=202)
    def start(body:Action,request:Request):
        require(request)
        try:return get_setup().start(body.planId,body.reinstall)
        except ValueError as error:raise HTTPException(409,str(error)) from None
    @api.post('/api/setup/background/activation',status_code=202)
    def activation(body:Activation,request:Request):
        require(request)
        try:return get_setup().activation(body.planId,body.enabled)
        except ValueError as error:raise HTTPException(409,str(error)) from None
    @api.post('/api/setup/background/remove',status_code=202)
    def removal(body:Removal,request:Request):
        require(request)
        try:return get_setup().remove(body.planId)
        except ValueError as error:raise HTTPException(409,str(error)) from None
    return api
