"""Director admission/recovery lifetime, without starting or stopping models."""
import threading

from .director_adapter import LocalDirectorAdapter


class DirectorHost:
    def __init__(self, adapter: LocalDirectorAdapter):
        self.adapter=adapter
        self._condition=threading.Condition()
        self._thread=None
        self._accepting=False
        self._stopping=False
        self._active=0
        self._sequence=0

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('The director host is already running.')
            self._stopping=False
            self._accepting=True
            # A daemon could disappear during shutdown and release an uncertain
            # GPU lease. Graceful shutdown must wait for confirmed recovery.
            self._thread=threading.Thread(target=self._recover,name='studio-director-recovery',daemon=False)
            try:self._thread.start()
            except BaseException:
                self._accepting=False
                raise

    def __call__(self,owner,messages):
        with self._condition:
            if not self._accepting:
                raise RuntimeError('The director host is not accepting requests.')
            self._active+=1
            self._sequence+=1
        try:return self.adapter(owner,messages)
        finally:
            with self._condition:
                self._active-=1
                self._condition.notify_all()

    def _recover(self):
        while True:
            with self._condition:
                sequence=self._sequence
            try:recovered=self.adapter.poll_recovery()
            except Exception:recovered=False
            with self._condition:
                if self._stopping and not self._active and recovered and sequence==self._sequence:
                    return
                self._condition.wait(timeout=.25)

    def stop(self):
        """Stop admission and wait for active/uncertain work without force release.

        May remain pending while the runtime is unavailable. External process
        killing cannot preserve an OS flock and is not graceful shutdown.
        """
        with self._condition:
            self._accepting=False
            self._stopping=True
            self._condition.notify_all()
            thread=self._thread
        if thread is not None:thread.join()
