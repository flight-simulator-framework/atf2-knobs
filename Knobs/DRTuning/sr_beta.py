"""Fit measured absolute beta to public linear transport near SR/SR2.

This is a local constrained Courant-Snyder fit. The external Okugi/Kubo SAD
executables referenced by legacy Turn1024 are not present in op-base.
"""
import json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from PyQt6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QFileDialog
from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.core import serial


def fit_sr_beta(names,beta,calibration):
    if calibration.get('schema')!='dr-sr-beta-1' or not calibration.get('source'):
        raise ValueError('Supply versioned public SR beta transport')
    available=dict(zip(names,beta));rows=calibration['bpms']
    chosen=[r for r in rows if r['name'] in available and np.isfinite(available[r['name']]) and available[r['name']]>0]
    if len(chosen)<3:raise ValueError('At least three calibrated absolute beta measurements are required')
    y=np.array([available[r['name']] for r in chosen])
    def transport_row(r):
        if 'beta_transport' in r:return r['beta_transport']
        a,b=r['r11'],r['r12_m'];return [a*a,-2*a*b,b*b]
    basis=np.asarray([transport_row(r) for r in chosen],float)
    if basis.shape!=(len(chosen),3) or not np.isfinite(basis).all():raise ValueError('Invalid beta transport basis')
    if np.linalg.matrix_rank(basis)<3:raise ValueError('Degenerate beta transport basis')
    def twiss(p):
        beta0=np.exp(p[0]);alpha=p[1];return np.array([beta0,alpha,(1+alpha*alpha)/beta0])
    result=least_squares(lambda p:(basis@twiss(p)-y)/y,[np.log(np.median(y)),0.],bounds=([-20,-1000],[20,1000]))
    if not result.success:raise ValueError('SR beta fit failed')
    values=twiss(result.x);monitors=[]
    for row in calibration['monitors']:
        a,b=row['r11'],row['r12_m'];monitors.append(dict(name=row['name'],beta_m=float(np.array([a*a,-2*a*b,b*b])@values)))
    return dict(source=calibration['source'],method='local constrained Courant-Snyder fit (not external SAD executable)',
        names=[r['name'] for r in chosen],measured_beta_m=y,fitted_beta_m=basis@values,
        reference_twiss=dict(beta_m=values[0],alpha=values[1],gamma_per_m=values[2]),monitors=monitors,
        relative_residual_rms=float(np.sqrt(np.mean(result.fun**2))))


class SRBetaDialog(QDialog):
    def __init__(self,window,estimate,phase_result=None):
        super().__init__(window);self.window=window;self.estimate=estimate;self.phase_result=phase_result;self.calibration=None;self.results={}
        self.setWindowTitle('Fit BETA near XSR / SR2');self.resize(850,650)
        layout=QVBoxLayout(self)
        note=QLabel('Measured absolute beta + independently calibrated transport. Local Courant–Snyder fit; legacy external SAD scripts are not bundled.');note.setWordWrap(True);layout.addWidget(note)
        buttons=QHBoxLayout();layout.addLayout(buttons)
        for text,action in [('Load SR optics',self.load),('Use RFTrack model',self.model_calibration),('CALC',self.calculate),('SAVE',self.save)]:
            b=QPushButton(text);b.clicked.connect(action);buttons.addWidget(b)
        if hasattr(window,'emittance_panel'):
            b=QPushButton('Estimate XSR emittance');b.clicked.connect(self.emittance);buttons.addWidget(b)
        self.plot=Plot();layout.addWidget(self.plot);self.result_label=QLabel();layout.addWidget(self.result_label)
    def model_calibration(self):
        def action(progress):
            from .model_optics import sr_beta_calibration
            manager=self.window.service.model_manager
            return sr_beta_calibration(manager.require_current(),manager.optics)
        def done(result):self.calibration=result;self.result_label.setText('RFTrack transport ready. Press CALC to fit measured beta.')
        self.window.start(action,done)
    def load(self):
        path,_=QFileDialog.getOpenFileName(self,'SR beta transport calibration','','JSON (*.json)')
        if path:
            try:self.calibration=json.loads(Path(path).read_text())
            except Exception as e:self.window.error(str(e))
    def calculate(self):
        try:
            if not self.calibration:raise ValueError('Load independent SR transport calibration')
            self.results={}
            for axis,cal in self.calibration['planes'].items():
                beta=self.estimate['planes'][axis].get('beta_m');names=self.estimate['names']
                if beta is None and self.phase_result:
                    rows=[r for r in self.phase_result['planes'][axis] if r['valid']]
                    names=[r['names'][0] for r in rows];beta=[r['beta_m'] for r in rows]
                if beta is None:continue
                self.results[axis]=fit_sr_beta(names,beta,cal)
            if not self.results:raise ValueError('Absolute beta or phase beta is required')
            self.window.latest_sr_beta=serial(self.results)
            self.plot.figure.clear();axes=self.plot.figure.subplots(len(self.results),1,squeeze=False).ravel()
            for ax,(axis,result) in zip(axes,self.results.items()):
                ax.plot(result['measured_beta_m'],'o',label='Measured');ax.plot(result['fitted_beta_m'],label='Fit');ax.legend();ax.set(ylabel='Beta '+axis+' [m]',xlabel='Selected BPM index')
            self.plot.canvas.draw_idle();self.result_label.setText('; '.join(f"{axis}: "+', '.join(f"{m['name']} β={m['beta_m']:.4g} m" for m in r['monitors']) for axis,r in self.results.items()))
        except Exception as e:self.window.error(str(e))
    def emittance(self):
        try:
            if not self.results:raise ValueError('Calculate the SR beta fit first')
            panel=self.window.emittance_panel;panel.import_beta(self.results)
            self.window.tabs.setCurrentWidget(panel);self.hide()
        except Exception as e:self.window.error(str(e))
    def save(self,path=None):
        if not self.results:return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save SR beta fit')
        if not path:return
        root=Path(path);root.mkdir(parents=True,exist_ok=True)
        (root/'sr_beta.json').write_text(json.dumps(serial(self.results),indent=2)+'\n')
        for ext in ('png','pdf'):self.plot.save(root/f'sr_beta.{ext}')
