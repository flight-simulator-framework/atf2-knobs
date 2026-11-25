"""Per-operation RF power monitoring on the live DRTuning worker thread."""
from contextvars import ContextVar
from functools import wraps
import time

from Knobs.DRCorrection.core import Cancelled
from Knobs.linac_power_guard import KlystronPowerGuard, KlystronPowerLoss

_active = ContextVar('dr_power_pause', default=None)


class PowerPause:
    def __init__(self, cancel, notify, factory=KlystronPowerGuard):
        self.cancel = cancel
        self.notify = notify
        self.factory = factory
        self.guard = None
        self.generation = 0
        self.unwinding = False
        self.start_error = None

    def run(self, action, progress):
        token = _active.set(self)
        try:
            return action(progress)
        finally:
            _active.reset(token)
            if self.guard is not None:
                self.guard.close()

    def check(self):
        if self.unwinding:
            return
        if self.start_error:
            raise RuntimeError(self.start_error)
        if self.guard is None:
            try:
                self.start()
            except Cancelled:
                self.unwinding = True
                raise
            except Exception as exc:
                self.start_error = f'RF power monitor could not start: {exc}'
                raise RuntimeError(self.start_error) from exc
        self.check_power()

    def start(self):
        self.guard = self.factory()
        self.guard.start()
        deadline = time.monotonic() + 15.
        while not self.guard.ready:
            if self.cancel.wait(.05):
                raise Cancelled('Cancelled while waiting for RF power')
            if self.check_power():
                deadline = time.monotonic() + 15.
            if time.monotonic() >= deadline:
                raise TimeoutError('No start RF power readings from all klystrons 0–8')

    def check_power(self):
        try:
            self.guard.check()
            return False
        except KlystronPowerLoss as exc:
            reason = str(exc)
        self.generation += 1
        self.notify(self, reason)
        while not self.cancel.is_set():
            if self.guard.resume_if_recovered():
                break
            self.cancel.wait(.05)
        if self.cancel.is_set():
            self.unwinding = True
            raise Cancelled('RF pause cancelled; restoring temporary settings')
        self.notify(self, '')
        return True


def checkpoint():
    monitor = _active.get()
    if monitor is not None:
        monitor.check()


_READ_ERRORS = (RuntimeError, TimeoutError, ValueError)


def stable_read(read, retry_errors=_READ_ERRORS):
    """Repeat a beam read if an RF pause interrupted its samples."""
    monitor = _active.get()
    if monitor is None:
        return read()
    while True:
        monitor.check()
        generation = monitor.generation
        try:
            result = read()
        except Cancelled:
            raise
        except retry_errors:
            if monitor.cancel.is_set():
                raise
            monitor.check()
            if generation == monitor.generation:
                raise
            continue
        monitor.check()
        if generation == monitor.generation:
            return result


def fresh_measurement(*, retry_errors=_READ_ERRORS):
    def decorate(method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            return stable_read(lambda: method(*args, **kwargs), retry_errors)
        return wrapped
    return decorate


def restoration(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        token = _active.set(None)
        try:
            return method(*args, **kwargs)
        finally:
            _active.reset(token)
    return wrapped
