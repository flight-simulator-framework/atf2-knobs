"""QM dispersion and measured local-bump/XSR controls for DRTuning."""
import copy
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import numpy as np
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,
                            QDoubleSpinBox,QPushButton,QTableWidget,QTableWidgetItem,QFileDialog,QSplitter,QLineEdit,QGroupBox,QScrollArea,QSizePolicy)
from Knobs.DRCorrection.core import serial
from Knobs.DRCorrection.gui import Plot
from .coupling import CouplingService,BumpOptions
from .dispersion import DispersionService,DispersionOptions,QM_PROBE_PRESETS_A
from .qm_checkpoint import load_checkpoint


def control(form,label,value,minimum,maximum,decimals=3):
    spin=QDoubleSpinBox();spin.setDecimals(decimals);spin.setRange(minimum,maximum);spin.setValue(value)
    spin.setMaximumWidth(150)
    form.addRow(label,spin);return spin


def probe_quality_text(quality):
    if not quality:return ''
    if quality.get('skipped'):return 'Skipped: '+quality['reason']
    return f"{quality['orbit_response_rms_mm']*1000:.3g} / {quality['target_change_mm']*1000:.3g}"


class CorrectionTool(QWidget):
    update=pyqtSignal(object)
    def __init__(self,window):
        super().__init__();self.window=window;self.result=None
        layout=QVBoxLayout(self);split=QSplitter();self.splitter=split;layout.addWidget(split)
        left=QWidget();self.controls=QVBoxLayout(left);self.form=QFormLayout();self.controls.addLayout(self.form)
        right=QWidget();self.right=QVBoxLayout(right);self.plot=Plot();self.right.addWidget(self.plot,1)
        self.table=QTableWidget();self.table.setMaximumHeight(170);self.right.addWidget(self.table)
        self.status=QLabel();self.status.setWordWrap(True);self.controls.addWidget(self.status)
        self.buttons=QVBoxLayout();self.controls.addLayout(self.buttons)
        self.save_button=QPushButton('Save data + plot');self.save_button.clicked.connect(self.save);self.controls.addWidget(self.save_button)
        self.controls.addStretch();scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(left)
        split.addWidget(scroll);split.addWidget(right);split.setSizes([340,900])
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
        path,_=QFileDialog.getSaveFileName(self,'Save correction measurements',str(getattr(self,'autosave_directory','')),'JSON (*.json)')
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
        self.controls.setSpacing(6)
        self.splitter.setSizes([380,900]);self.splitter.setStretchFactor(0,0);self.splitter.setStretchFactor(1,1)
        self.splitter.widget(0).setMinimumWidth(340)
        def section(title):
            box=QGroupBox(title);form=QFormLayout(box)
            form.setVerticalSpacing(5);self.form.addRow(box);return form
        measurement=section('Measurement')
        self.rf=control(measurement,'RF span [kHz]',defaults.frequency_change_khz,.01,10.)
        self.target_snr=control(measurement,'Target S/N',defaults.target_snr,1.,100.,1)
        self.probe=control(measurement,'Fallback probe [A]',defaults.probe_A,.01,3.,2);self.probe.setSingleStep(.01)
        self.probe.setToolTip('Used for a QM without an individual starting width.')
        self.probe_limit=control(measurement,'Current limit ± [A]',defaults.probe_current_limit_A,.01,3.,2)
        self.probe_limit.setSingleStep(.01)
        correction=section('Correction')
        self.selection=QComboBox();self.selection.addItem('SVD — all QMs','all');self.selection.addItem('Greedy — select QMs','greedy')
        correction.addRow('Solver',self.selection)
        self.maximum=control(correction,'Maximum QMs',defaults.maximum,1,16,0)
        correction.setRowVisible(self.maximum,False)
        self.excluded=QLineEdit(', '.join(defaults.fixed_actuators));correction.addRow('Exclude QMs',self.excluded)
        self.ridge=control(correction,'Regularization',defaults.regularization,0.,1.,4)
        self.ridge.setToolTip('Larger values reduce correction from weak responses.')
        def button_row(items):
            row=QHBoxLayout();self.buttons.addLayout(row);buttons=[]
            for label,slot in items:
                button=QPushButton(label);button.setMinimumHeight(32);button.clicked.connect(slot)
                row.addWidget(button);buttons.append(button)
            return buttons
        self.calc_button,self.set_button=button_row([
            ('1. CALC',self.calculate),('2. Apply',self.apply)])
        self.calc_button.setStyleSheet('font-weight: 600;')
        self.set_button.setStyleSheet('font-weight: 600;')
        self.bpm_button,self.recalculate_button=button_row([
            ('Select BPMs…',lambda:self.window.open_tool('selection')),('Recalculate',self.recalculate)])
        self.recalculate_button.setToolTip('Reuse measured responses with the current BPM selection.')
        self.load_button,self.resume_button=button_row([
            ('Load scan…',self.load_scan),('Resume',self.resume_scan)])
        self.reset_button=self.add_button('Restore original QM / RF',self.reset)
        for button in (self.set_button,self.recalculate_button,self.resume_button,self.reset_button):button.setEnabled(False)
        self.controls.removeWidget(self.save_button)
        storage=QGroupBox('Data');storage_layout=QVBoxLayout(storage)
        self.autosave_directory=Path('/atf/data/flight-simulator/DRTuning')/datetime.now().strftime('%Y')
        self.save_directory=QLabel(str(self.autosave_directory));self.save_directory.setWordWrap(True)
        self.save_directory.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.save_directory.setToolTip(str(self.autosave_directory))
        storage_layout.addWidget(self.save_directory)
        self.save_status=QLabel('Measurements and applied results save automatically.');self.save_status.setWordWrap(True)
        self.save_status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        storage_layout.addWidget(self.save_status)
        row=QHBoxLayout();storage_layout.addLayout(row)
        self.folder_button=QPushButton('Change folder…');self.folder_button.clicked.connect(self.choose_autosave_directory)
        row.addWidget(self.folder_button);row.addWidget(self.save_button)
        self.controls.insertWidget(self.controls.count()-1,storage)
        self.status.setText('Ready to measure.')
        self.status.setStyleSheet('padding: 6px; font-weight: 600;')
        self.table.setMaximumHeight(260)
        self.set_table(['QM trim','Initial probe [A]'],
            [(name,f'{width:.2f}') for name,width in QM_PROBE_PRESETS_A.items()])
        for w in (self.maximum,self.probe,self.probe_limit,self.target_snr,self.rf,self.ridge):
            w.setMaximumWidth(105);w.valueChanged.connect(self.invalidate)
        self.selection.currentIndexChanged.connect(lambda _:correction.setRowVisible(self.maximum,self.selection.currentData()=='greedy'))
        self.selection.currentIndexChanged.connect(self.invalidate)
        self.excluded.textChanged.connect(self.invalidate)
    def invalidate(self,*_):
        self.set_button.setEnabled(False)
        if self.result is not None and 'response' in self.result and not self.result.get('applied'):
            self.status.setText('Selection / options changed. Recalculate from measured response before Apply.\n'
                                'Changing RF settings or QM selection requires a new CALC.')
    def proposal_matches_options(self,data):
        saved=dict(data['options']);current=asdict(self.options())
        for key in ('excluded_bpms','fixed_actuators'):
            saved[key]=sorted(saved.get(key,()));current[key]=sorted(current[key])
        return saved==current
    def choose_autosave_directory(self):
        path=QFileDialog.getExistingDirectory(self,'Automatic save folder',str(self.autosave_directory))
        if path:
            self.autosave_directory=Path(path);self.save_directory.setText(path);self.save_directory.setToolTip(path)
            self.save_status.setText('Save folder updated.')
    def new_checkpoint_path(self):
        stamp=datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')
        return Path(self.autosave_directory)/('qm_scan_'+stamp+'_'+uuid4().hex[:8]+'.checkpoint.json')
    def load_scan(self):
        path,_=QFileDialog.getOpenFileName(self,'Load QM scan checkpoint',str(self.autosave_directory),'JSON (*.json)')
        if not path:return
        try:
            data=self.service.validate_checkpoint(load_checkpoint(path))
            self.service.restore_width_cache(data);self.service.last_result=data;self.display(data)
            self.save_status.setText('Loaded: '+Path(path).name)
            self.window.log('Loaded QM scan: '+path)
        except Exception as exc:self.window.error(str(exc))
    def resume_scan(self):
        if self.result is None:return
        data=self.result;opt=self.options();path=self.new_checkpoint_path();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.resume(data,opt,p,self.update.emit,checkpoint_path=path),
                 self.display,'Resume QM response measurement')
    def applied(self,data):
        self.display(data)
        # File errors must never roll back an already verified machine correction.
        try:
            directory=Path(self.autosave_directory);directory.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().astimezone()
            dest=directory/('qm_dispersion_'+stamp.strftime('%Y%m%d_%H%M%S_%f')+'_'+uuid4().hex[:8]+'.json')
            data['autosave']=dict(saved_at=stamp.isoformat(),json=str(dest),plot=str(dest.with_suffix('.png')))
            dest.write_text(json.dumps(serial(data),indent=2,allow_nan=False),encoding='utf-8')
            self.plot.save(dest.with_suffix('.png'))
            self.save_status.setText('Auto-saved JSON + PNG:\n'+dest.name)
            self.window.log('QM Apply auto-saved: '+str(dest)+' and '+str(dest.with_suffix('.png')))
        except Exception as exc:
            message='QM SET remains applied. Automatic save failed: '+str(exc)+'\nUse Save data + plot to retry.'
            self.save_status.setText(message);self.window.log(message)
    def options(self):
        shared=self.window.panels['dispersion'].options
        return DispersionOptions(probe_A=self.probe.value(),step_A=self.probe_limit.value(),frequency_change_khz=self.rf.value(),
            use_probe_presets=True,adaptive_probe=True,reuse_probe_widths=True,characterize_first_scan=False,
            probe_current_limit_A=self.probe_limit.value(),target_snr=self.target_snr.value(),
            momentum_compaction=shared.momentum_compaction,measure_tunes=False,constrain_cod=False,
            regularization=self.ridge.value(),selection=self.selection.currentData(),maximum=int(self.maximum.value()),
            excluded_bpms=shared.excluded_bpms,fixed_actuators=tuple(sorted(set(shared.fixed_actuators)|{n.strip() for n in self.excluded.text().split(',') if n.strip()})))
    def calculate(self):
        opt=self.options();path=self.new_checkpoint_path();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.calculate(opt,p,self.update.emit,checkpoint_path=path),self.display,'QM horizontal dispersion response')
    def recalculate(self):
        if self.result is None:return
        data=self.result;opt=self.options();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.recalculate(data,opt,p),self.display,'Recalculate QM correction from measured response')
    def apply(self):
        if self.result is None:return
        if not self.proposal_matches_options(self.result):
            self.invalidate();self.window.error('Selection / options changed; recalculate before Apply');return
        data=self.result;self.set_button.setEnabled(False)
        self.run(lambda p:self.service.apply(data,p,self.update.emit),self.applied,'Set QM correction and measure actual dispersion')
    def reset(self):
        if self.result is None:return
        data=self.result
        def done(_):self.display(data);self.set_button.setEnabled(False);self.status.setText('Original QM / RF settings restored. Resume, recalculate or CALC again.')
        self.run(lambda _:self.service.reset(data),done,'Restore QM currents')
    def on_update(self,event):
        if event['kind']=='checkpoint':
            self.save_status.setText(f"Scan checkpoint: {event['completed']}/{event['total']} saved; {event.get('skipped',0)} skipped")
            return
        if event.get('data') is not None:self.display(event['data'])
        if event['kind']=='failure':self.set_button.setEnabled(False)
    def display(self,data):
        self.result=data;figure=self.plot.figure;figure.clear();figure.set_layout_engine('constrained',rect=(0,0,1,.89))
        axes=figure.subplots(2,2).ravel()
        valid=np.asarray(data['valid'],bool);s=np.asarray(data['positions_m'],float)
        known=np.isfinite(s).all()
        if not known:s=np.arange(1,len(data['names'])+1)
        order=np.argsort(s)
        for i,ax in enumerate(axes):
            for key,label,style in [('full_measured','Measured before','k.'),('full_predicted','Predicted','C2--'),('full_after','Measured after SET','C3-'),('full_target','Target',':')]:
                if key not in data:continue
                mask=valid.copy()
                if key=='full_after':mask &= np.asarray(data['after_valid'],bool)
                if key=='full_after' and data.get('restored'):label='After SET (since restored)'
                ax.plot(s[order],np.where(mask,np.asarray(data[key],float)[i],np.nan)[order],style,label=label,
                        **({'color':'gray'} if key=='full_target' else {}))
            ax.set(xlabel='s [m]' if known else 'BPM index (measurement order)',ylabel='η [mm / (dp/p)]' if i<2 else 'COD [mm]',title=data['full_signal_labels'][i]);ax.grid(alpha=.2)
        handles,labels=axes[0].get_legend_handles_labels()
        figure.legend(handles,labels,loc='upper center',ncol=2,fontsize=8)
        self.plot.canvas.draw_idle()
        if 'delta' in data:
            rows=[]
            for i,(n,c,d) in enumerate(zip(data['actuators'],data['centers'],data['delta'])):
                q=data.get('probe_quality',[]);q=q[i] if i<len(q) else None
                quality=probe_quality_text(q)
                snr=('Not measured' if q.get('skipped') else f"{q['snr']:.3g}" if q['snr'] is not None else 'No measured scatter') if q else ''
                estimate=data.get('successful_probe_widths',{}).get(n,{}).get('width_A')
                rows.append((n,f'{c:.5f}',f'{d:+.5f}',f'{c+d:.5f}',quality,snr,f'{estimate:.2f}' if estimate is not None else ''))
            self.set_table(['QM trim','Before [A]','Delta [A]','Proposed [A]','Response / target [µm]','S/N','Next probe [A]'],rows)
            self.set_button.setEnabled(bool(not data.get('applied') and not data.get('error') and
                                       self.proposal_matches_options(data) and np.any(abs(np.asarray(data['delta']))>1.e-12)))
            if data.get('applied'):message='Applied. Dispersion remeasured.'
            elif not self.proposal_matches_options(data):message='Settings changed. Recalculate before Apply.'
            elif not np.any(abs(np.asarray(data['delta']))>1.e-12):message='No correction proposed.'
            else:message='Ready to apply.'
            self.status.setText(message+f'\nFit BPMs: {valid.sum()} / {len(valid)}')
        else:
            self.set_button.setEnabled(False)
            state=data.get('scan_state',{});done=state.get('completed',0)
            self.status.setText(f"Measured: {done}/{len(data['actuators'])} QMs.")
            qualities=data.get('probe_quality',[])
            estimates=data.get('successful_probe_widths',{})
            self.set_table(['QM trim','Scan','Response / target [µm]','Next probe [A]'],
                [(n,('Skipped' if qualities[i].get('skipped') else 'Complete') if i<done else 'Pending',
                  probe_quality_text(qualities[i]) if i<len(qualities) else '',
                  f"{estimates[n]['width_A']:.2f}" if n in estimates else '')
                 for i,n in enumerate(data['actuators'])])
        if data.get('error'):
            self.status.setText(('Baseline restored.' if data.get('restored') else 'Operation failed; inspect restoration log.')+'\n'+data['error'])
        elif data.get('proposal_unavailable_reason'):self.status.setText('S/N below target. Run CALC to remeasure.')
        if data.get('skipped_qms'):
            self.status.setText(self.status.text()+'\nSkipped QM: '+', '.join(r['magnet'] for r in data['skipped_qms']))
        state=data.get('scan_state',{})
        self.resume_button.setEnabled(bool(state and state.get('status')!='complete' and not data.get('applied')))
        self.reset_button.setEnabled(bool(data.get('applied') or data.get('error') or state.get('active_magnet')))
        self.recalculate_button.setEnabled('response' in data and not data.get('applied') and not data.get('restoration_error') and
                                           (not data.get('error') or bool(data.get('restored'))))
