"""
ui/worker_pool.py
=============================================================================
WorkerSignals / Worker / WorkerPool: a tiny generic QThread pool used by
MainWindow for fire-and-forget background calls. Distinct from (and does
not replace) ui/workers.py, which holds the purpose-built workers
(WeatherWorker, TTSWorker, etc).
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal()


class Worker(QThread):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
    
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.result.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()


class WorkerPool:
    def __init__(self):
        self._workers = []
    
    def submit(self, fn, *args, **kwargs) -> Worker:
        worker = Worker(fn, *args, **kwargs)
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        return worker
    
    def _cleanup(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
    
    def shutdown(self):
        for w in self._workers:
            w.terminate()
            w.wait(1000)
        self._workers.clear()


# =============================================================================
# 🔧 AUDIO HELPERS
# =============================================================================
