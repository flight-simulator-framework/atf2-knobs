"""QM dispersion and measured local-bump/XSR controls for DRTuning."""
import copy
import json
from pathlib import Path
import numpy as np
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,
                            QDoubleSpinBox,QPushButton,QTableWidget,QTableWidgetItem,QFileDialog,QSplitter,QLineEdit,QCheckBox)
from Knobs.DRCorrection.core import serial
from Knobs.DRCorrection.gui import Plot
from .coupling import CouplingService,BumpOptions
from .dispersion import DispersionService,DispersionOptions


def control(form,label,value,minimum,maximum,decimals=3):
    spin=QDoubleSpinBox();spin.setDecimals(decimals);spin.setRange(minimum,maximum);spin.setValue(value)
    spin.setMaximumWidth(150)
    form.addRow(label,spin);return spin


class CorrectionTool(QWidget):
    update=pyqtSignal(object)
    def __init__(self,window):
        super().__init__();self.window=window;self.result=None
        layout=QVBoxLayout(self);split=QSplitter();layout.addWidget(split)
        left=QWidget();self.controls=QVBoxLayout(left);self.form=QFormLayout();self.controls.addLayout(self.form)
        right=QWidget();self.right=QVBoxLayout(right);self.plot=Plot();self.right.addWidget(self.plot,1)
        self.table=QTableWidget();self.table.setMaximumHeight(170);self.right.addWidget(self.table)
        self.status=QLabel();self.status.setWordWrap(True);self.controls.addWidget(self.status)
        self.buttons=QVBoxLayout();self.controls.addLayout(self.buttons)
        save=QPushButton('Save data + plot');save.clicked.connect(self.save);self.controls.addWidget(save)
        self.controls.addStretch();split.addWidget(left);split.addWidget(right);split.setSizes([310,900])
        self.update.connect(self.on_update)
    def add_button(self,label,slot):
        button=QPushButton(label);button.clicked.connect(slot);self.buttons.addWidget(button);return button
    def set_table(self,headers,rows):
        self.table.setColumnCount(len(headers));self.table.setHorizontalHeaderLabels(headers);self.table.setRowCount(len(rows))
        for i,row in enumerate(rows):
            for j,value in enumerate(row):self.table.setItem(i,j,QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
    def run(self,action,done,label):
        def guarded(progress):
            try:return action(progress)
            except BaseException:
                self.update.emit(dict(kind='failure',data=copy.deepcopy(self.service.last_result)))
                raise
        self.window.start(guarded,done,label=label)
    def save(self):
        data=self.result
        if data is None:self.window.error('No acquired data to save');return
        path,_=QFileDialog.getSaveFileName(self,'Save correction measurements','','JSON (*.json)')
        if path:
            try:
                dest=Path(path).with_suffix('.json');dest.write_text(json.dumps(serial(data),indent=2,allow_nan=False))
                self.plot.save(dest.with_suffix('.png'));self.window.log('Saved '+str(dest))
            except Exception as exc:self.window.error(str(exc))


class CouplingPanel(CorrectionTool):
    def __init__(self,window):
        super().__init__(window);self.service=CouplingService(window.backend,window.service.cancel)
        self.geometry=None;self.calibration=None;self.points=[];self.samples={};self.fits=[];self.last_orbit=None
        self.target=QComboBox();self.target.addItems([f'{f}.{i}' for f in ('SD1R','SF1R') for i in range(1,35)])
        self.form.addRow('SD / SF bump position',self.target)
        self.amplitude=control(self.form,'Scan half-range [mm]',.4,.01,2.)
        self.refine=control(self.form,'Refinement ± [mm]',.1,.001,1.)
        self.probe=control(self.form,'Corrector probe ± [A]',.05,.001,.5)
        self.step=control(self.form,'Max change from baseline [A]',1.,.001,5.)
        self.leak=control(self.form,'Outside COD max [mm]',.01,.0001,1.,4)
        self.rms=control(self.form,'Outside COD RMS [mm]',.005,.0001,1.,4)
        self.settle=control(self.form,'Settle before acquisition [s]',1.,0.,60.)
        self.controls.insertWidget(0,QLabel('BPM: 10 updates / XSR: 10 updates\nXSR:PROJ_V_SIGMA — vertical size [µm]'))
        self.add_button('Rank SD/SF by |trim current|',self.rank)
        self.add_button('1. Select BPMs / correctors',self.prepare)
        self.calibrate_button=self.add_button('2. Calibrate and close bump',self.calibrate)
        self.scan_button=self.add_button('3. Scan → fit → set optimum',self.scan)
        self.reset_button=self.add_button('Restore baseline currents',self.reset)
        self.calibrate_button.setEnabled(False);self.scan_button.setEnabled(False);self.reset_button.setEnabled(False)
        for widget in (self.amplitude,self.refine,self.probe,self.step,self.leak,self.rms,self.settle):widget.valueChanged.connect(self.invalidate)
        self.target.currentIndexChanged.connect(self.invalidate)
        self.status.setText('Select a target, then inspect the automatically selected BPMs and correctors.\nTarget mm is reconstructed from local BPMs using model transport.')
        self.redraw()
    def options(self):
        return BumpOptions(amplitude_mm=self.amplitude.value(),refine_mm=self.refine.value(),
            calibration_mm=min(.1,self.amplitude.value()),probe_A=self.probe.value(),max_step_A=self.step.value(),
            outside_max_mm=self.leak.value(),outside_rms_mm=self.rms.value(),settle_s=self.settle.value())
    def invalidate(self,*_):
        self.geometry=None;self.calibration=None;self.calibrate_button.setEnabled(False);self.scan_button.setEnabled(False)
    def rank(self):
        def done(rows):
            self.invalidate();self.target.blockSignals(True);self.target.clear()
            for row in rows:
                if row['available']:self.target.addItem(f"{row['name']}   {row['current_A']:+.4f} A",row['name'])
            self.target.blockSignals(False)
            self.set_table(['Trim','Readback [A]','Status'],[(r['name'],r['current_A'],r.get('error','Available')) for r in rows])
            self.status.setText('Sorted by absolute trim readback current, descending; signed currents shown.')
        self.run(lambda _:self.service.ranked_targets(),done,'Read SD/SF trim currents')
    def prepare(self):
        target=self.target.currentData() or self.target.currentText();opt=self.options()
        model_options=copy.deepcopy(self.window.panels['dispersion'].options)
        def done(g):
            self.geometry=g;self.calibration=None;self.result=g;self.calibrate_button.setEnabled(True);self.scan_button.setEnabled(False)
            self.set_table(['Corrector','Baseline [A]','Model knob [A/mm]'],zip(g['actuators'],g['centers'],g['model_knob_A_per_mm']))
            self.status.setText('Target '+g['target']+'\nInternal BPMs: '+', '.join(g['internal_bpms'])+
                f"\nOutside: {len(g['outside_indices'])} BPMs, X and Y.\n4 V + 4 H correctors selected to close both planes.")
        self.run(lambda p:self.service.prepare(target,opt,model_options,p),done,'Select local bump geometry')
    def calibrate(self):
        if self.geometry is None:return
        opt=self.options();g=copy.deepcopy(self.geometry)
        def done(value):
            self.calibration=value;self.result=value;self.scan_button.setEnabled(True)
            self.set_table(['Corrector','Baseline [A]','Measured knob [A/mm]'],zip(value['actuators'],value['centers'],value['knob_A_per_mm']))
            self.status.setText('Bump calibrated in both signs; original currents restored. Ready for XSR scan.')
        self.run(lambda p:self.service.calibrate(g,opt,p,self.update.emit),done,'Measure and close local bump')
    def scan(self):
        if self.calibration is None:return
        opt=self.options();g=copy.deepcopy(self.calibration)
        self.points=[];self.samples={};self.fits=[];self.redraw()
        def done(value):
            self.result=value;self.scan_button.setEnabled(False);self.calibrate_button.setEnabled(False);self.reset_button.setEnabled(True)
            self.status.setText(f"Set {value['final_amplitude_mm']:+.4f} mm; XSR {value['final']['xsr']['mean']:.3f} µm.\nFinal XSR and outside COD verified. Re-select / calibrate before another scan.")
        self.run(lambda p:self.service.scan(g,opt,p,self.update.emit),done,'Local bump / XSR minimization')
    def reset(self):
        if self.result is None:return
        data=self.result
        def done(_):
            self.invalidate();self.reset_button.setEnabled(False);self.status.setText('Baseline currents restored. Select / calibrate again.')
        self.run(lambda _:self.service.reset(data),done,'Restore local bump currents')
    def on_update(self,event):
        kind=event['kind']
        if kind=='sample':
            self.samples[(event['phase'],event['amplitude_mm'])]=event['xsr']
            self.status.setText(f"{event['phase']}: {event['amplitude_mm']:+.4f} mm\nXSR updates {event['xsr']['count']}/10; mean {event['xsr']['mean']:.3f} µm")
        elif kind=='point':self.points.append(event['point'])
        elif kind=='fit':self.fits.append(event['fit'])
        elif kind=='orbit':self.last_orbit=event
        elif kind=='failure':
            if event.get('data') is not None:self.result=event['data']
            self.scan_button.setEnabled(False);self.calibrate_button.setEnabled(False)
            if self.result is not None:self.reset_button.setEnabled(bool(self.result.get('restoration_error')))
            self.status.setText('Operation failed / cancelled. See restoration status in the detailed log; acquired data can be saved.')
        self.redraw()
    def redraw(self):
        figure=self.plot.figure;figure.clear();ax,orbit=figure.subplots(2,1)
        for (phase,a),xsr in self.samples.items():
            ax.scatter(np.full(xsr['count'],a),xsr['values'],s=9,alpha=.3,color='C0')
        for i,p in enumerate(self.points):
            ax.errorbar(p['amplitude_mm'],p['xsr']['mean'],yerr=p['xsr']['sem'],fmt='o',color='C0',
                        label='10-update mean ± SEM' if i==0 else None)
        for i,fit in enumerate(self.fits):
            ax.plot(fit['amplitude_mm'],fit['size_um'],'--' if i==0 else '-',label=f'Fit {i+1}: {fit["best_mm"]:+.4f} mm')
            ax.axvline(fit['best_mm'],color=f'C{i+1}',alpha=.4)
        ax.set(xlabel='Vertical displacement at SD/SF [mm]',ylabel='XSR σy [µm]',title='Raw updates, mean and size² fit')
        if self.points or self.fits:ax.legend(fontsize=8)
        ax.grid(alpha=.2)
        if self.last_orbit is not None:
            event=self.last_orbit;g=self.calibration or self.geometry
            if g is not None:
                n=len(g['outside_indices']);s=np.asarray(g['positions_m'])[g['outside_indices']]
                orbit.plot(s,event['outside'][:n],'.',label='Outside ΔX');orbit.plot(s,event['outside'][n:],'.',label='Outside ΔY')
                orbit.legend(fontsize=8)
            orbit.set_title(f"a={event['amplitude_mm']:+.3f} mm / max {event['outside_max_mm']:.4f} mm / RMS {event['outside_rms_mm']:.4f} mm")
        orbit.axhline(self.leak.value(),color='gray',ls=':');orbit.axhline(-self.leak.value(),color='gray',ls=':')
        orbit.set(xlabel='Outside BPM s [m]',ylabel='COD change [mm]');orbit.grid(alpha=.2)
        self.plot.canvas.draw_idle()


class QMDispersionPanel(CorrectionTool):
    def __init__(self,window):
        super().__init__(window);self.service=DispersionService(window.backend,window.service.cancel)
        defaults=DispersionOptions()
        self.selection=QComboBox();self.selection.addItem('All enabled / regularized SVD','all');self.selection.addItem('Greedy / best columns','greedy')
        self.selection.setMaximumWidth(230);self.form.addRow('Solver',self.selection)
        self.excluded=QLineEdit();self.excluded.setMaximumWidth(150);self.excluded.setPlaceholderText('QM2R.1, QM2R.2')
        self.excluded.setText(', '.join(defaults.fixed_actuators))
        self.excluded.setToolTip('Comma-separated physical QM trim names');self.form.addRow('Exclude QM trims',self.excluded)
        self.maximum=control(self.form,'Greedy maximum trims',8,1,16,0)
        self.probe=control(self.form,'QM probe ± [A]',defaults.probe_A,.001,1.)
        self.step=control(self.form,'QM correction limit ± [A]',defaults.step_A,.001,2.)
        self.rf=control(self.form,'RF full span [kHz]',defaults.frequency_change_khz,.01,10.)
        self.measure_tunes=QCheckBox('Measure / constrain H and V tunes');self.measure_tunes.setChecked(defaults.measure_tunes)
        self.form.addRow(self.measure_tunes)
        self.tune=control(self.form,'Max tune change',.002,.00001,.1,5)
        self.tune.setEnabled(defaults.measure_tunes)
        self.cod=control(self.form,'Max COD change [mm]',.05,.001,1.)
        self.ridge=control(self.form,'Relative regularization',.01,0.,1.,4)
        self.add_button('1. Measure response / CALC',self.calculate)
        self.set_button=self.add_button('2. Apply / remeasure dispersion',self.apply)
        self.reset_button=self.add_button('Restore original QM currents',self.reset)
        self.set_button.setEnabled(False);self.reset_button.setEnabled(False)
        self.status.setText('QM1–4R and QM20–23R, .1/.2 (16 candidates; QM2R.1/.2 excluded by default).\nTarget: zero Dx at MB19–31 / MB66–80; preserve measured arc dispersion, Dy and COD.\n10 updated BPM frames per RF setting. Tune acquisition / constraints optional; off by default.')
        for w in (self.maximum,self.probe,self.step,self.rf,self.tune,self.cod,self.ridge):w.valueChanged.connect(lambda *_:self.set_button.setEnabled(False))
        self.selection.currentIndexChanged.connect(lambda *_:self.set_button.setEnabled(False))
        self.excluded.textChanged.connect(lambda *_:self.set_button.setEnabled(False))
        self.measure_tunes.toggled.connect(self.tune.setEnabled)
        self.measure_tunes.toggled.connect(lambda *_:self.set_button.setEnabled(False))
    def options(self):
        shared=self.window.panels['dispersion'].options
        return DispersionOptions(probe_A=self.probe.value(),step_A=self.step.value(),frequency_change_khz=self.rf.value(),
            momentum_compaction=shared.momentum_compaction,measure_tunes=self.measure_tunes.isChecked(),
            tune_tolerance=self.tune.value(),orbit_tolerance_mm=self.cod.value(),
            regularization=self.ridge.value(),selection=self.selection.currentData(),maximum=int(self.maximum.value()),
            excluded_bpms=shared.excluded_bpms,fixed_actuators=tuple(set(shared.fixed_actuators)|{n.strip() for n in self.excluded.text().split(',') if n.strip()}))
    def calculate(self):
        opt=self.options();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.calculate(opt,p,self.update.emit),self.display,'QM horizontal dispersion response')
    def apply(self):
        if self.result is None:return
        data=self.result;self.set_button.setEnabled(False)
        self.run(lambda p:self.service.apply(data,p,self.update.emit),self.display,'Set QM correction and measure actual dispersion')
    def reset(self):
        if self.result is None:return
        data=self.result
        def done(_):self.display(data);self.set_button.setEnabled(False);self.status.setText('Original QM currents restored. CALC again.')
        self.run(lambda _:self.service.reset(data),done,'Restore QM currents')
    def on_update(self,event):
        if event.get('data') is not None:self.display(event['data'])
        if event['kind']=='failure':self.set_button.setEnabled(False)
    def display(self,data):
        self.result=data;figure=self.plot.figure;figure.clear();figure.set_layout_engine('constrained',rect=(0,0,1,.89))
        axes=figure.subplots(2,2).ravel()
        valid=np.asarray(data['valid'],bool);s=np.asarray(data['positions_m']);order=np.argsort(s)
        for i,ax in enumerate(axes):
            for key,label,style in [('full_measured','Measured before','k.'),('full_predicted','Predicted','C2--'),('full_after','Measured after SET','C3-'),('full_target','Target',':')]:
                if key not in data:continue
                mask=valid.copy()
                if key=='full_after':mask &= np.asarray(data['after_valid'],bool)
                if key=='full_after' and data.get('restored'):label='After SET (since restored)'
                ax.plot(s[order],np.where(mask,np.asarray(data[key])[i],np.nan)[order],style,label=label,
                        **({'color':'gray'} if key=='full_target' else {}))
            ax.set(xlabel='s [m]',ylabel='η [mm / (dp/p)]' if i<2 else 'COD [mm]',title=data['full_signal_labels'][i]);ax.grid(alpha=.2)
        handles,labels=axes[0].get_legend_handles_labels()
        figure.legend(handles,labels,loc='upper center',ncol=2,fontsize=8)
        self.plot.canvas.draw_idle()
        if 'delta' in data:
            self.set_table(['QM trim','Before [A]','Delta [A]','Proposed [A]'],
                [(n,f'{c:.5f}',f'{d:+.5f}',f'{c+d:.5f}') for n,c,d in zip(data['actuators'],data['centers'],data['delta'])])
            self.set_button.setEnabled(not data.get('applied') and not data.get('error') and np.any(abs(np.asarray(data['delta']))>1.e-12))
            tune_status=('Predicted tune change: '+str(np.round(data['predicted_tune_change'],6))
                         if data.get('predicted_tune_change') is not None else 'Tune acquisition / tune constraints skipped.')
            self.status.setText(('SET applied; dispersion remeasured.' if data.get('applied') else 'CALC ready; probe currents restored.')+
                '\n'+data['solver']+'\n'+tune_status)
        if data.get('error'):
            self.status.setText(('Baseline restored.' if data.get('restored') else 'Operation failed; inspect restoration log.')+'\n'+data['error'])
        self.reset_button.setEnabled(bool(data.get('applied') or data.get('error')))
