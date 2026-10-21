"""Generate inspectable offline GUI evidence and optional stored-DR smoke run.

This explicitly supports demo/stored only, never EPICS.
"""
import argparse
import json
import os
from pathlib import Path
import time
import numpy as np


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--backend',choices=['demo','stored'],default='demo')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PyQt6.QtWidgets import QApplication
    from .backends import DemoBackend,StoredBackend
    from .gui import DRCorrectionWindow
    from .dialogs import BufferDialog,SRFitDialog
    from .core import serial
    started=time.monotonic()
    backend=DemoBackend() if args.backend=='demo' else StoredBackend(args.output/'plant')
    app=QApplication([]);window=DRCorrectionWindow(backend);window.show();app.processEvents()
    summary=dict(backend=backend.label,seed=32 if args.backend=='stored' else None,modes={})
    def wait():
        deadline=time.monotonic()+600
        while window.task and window.task.isRunning():
            app.processEvents();time.sleep(.01)
            if time.monotonic()>deadline:window.service.cancel.set();raise TimeoutError('Correction did not finish')
        app.processEvents()
        if 'ERROR:' in window.messages.toPlainText():raise RuntimeError(window.messages.toPlainText())
    try:
        initial=backend.snapshot()
        for i,(mode,panel) in enumerate(window.panels.items()):
            window.tabs.setCurrentIndex(i)
            if args.backend=='stored':
                keep=backend.skews[:2] if mode=='skew' else backend.vcorrectors[:2]
                panel.options.fixed_actuators=tuple(n for n in backend.hcorrectors+backend.vcorrectors+backend.skews if n not in keep)
                panel.options.count_v=2
            start=time.monotonic();panel.calculate();wait()
            assert panel.data is not None
            assert backend.same_settings(initial,backend.snapshot()),'CALC did not restore settings'
            panel.apply();wait()
            panel.save_bundle(args.output)
            app.processEvents();window.grab().save(str(args.output/(mode+'_gui.png')))
            data=panel.data;summary['modes'][mode]=dict(solver=data['solver'],actuators=data['actuators'],
                selected=data['selected'],rank=data['rank'],rms_before=np.sqrt(np.mean(np.asarray(data['measured'])**2,axis=1)),
                rms_after=np.sqrt(np.mean(np.asarray(data['after'])**2,axis=1)),wall_seconds=time.monotonic()-start)
            if mode=='dispersion' and args.backend=='demo':
                sr=SRFitDialog(window,data);sr.show();sr.calculate();sr.save(args.output);app.processEvents();sr.grab().save(str(args.output/'sr_gui.png'));sr.close()
            panel.reset();wait();assert backend.same_settings(initial,backend.snapshot()),'RESET did not restore'
            print(mode,serial(summary['modes'][mode]),flush=True)
        window.tabs.setCurrentIndex(3);window.tbt.turns.setValue(256);window.tbt.acquire();wait();window.tbt.analyze();window.tbt.save(args.output)
        app.processEvents();window.grab().save(str(args.output/'tbt_gui.png'))
        summary['tbt']={axis:dict(valid=value['valid'],tune=value.get('tune')) for axis,value in window.tbt.estimate['planes'].items()}
        if args.backend=='stored':summary['acquisitions']=backend.interface.counts;summary['equivalent_measurement_seconds']=backend.interface.time_s
        summary['wall_seconds']=time.monotonic()-started;summary['restored']=backend.same_settings(initial,backend.snapshot())
        (args.output/'summary.json').write_text(json.dumps(serial(summary),indent=2)+'\n')
        (args.output/'gui.log').write_text(window.messages.toPlainText()+'\n')
        print(json.dumps(serial(summary),indent=2),flush=True)
    finally:
        wait();window.close();app.processEvents()


if __name__=='__main__':main()
