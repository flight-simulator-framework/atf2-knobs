"""QM dispersion and measured local-bump/XSR controls for DRTuning."""
import copy
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import numpy as np
from PyQt6.QtCore import pyqtSignal,Qt,QTimer
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,QCheckBox,
                            QDoubleSpinBox,QPushButton,QTableWidget,QTableWidgetItem,QFileDialog,QSplitter,QLineEdit,QGroupBox,QScrollArea,QSizePolicy,QProgressBar,QListWidget,QListWidgetItem,QMessageBox)
from Knobs.DRCorrection.core import serial,Cancelled
from Knobs.DRCorrection.gui import Plot
from .dispersion_plot import DispersionPlot
from .coupling import CouplingService,BumpOptions,BumpRangeUnavailable,BumpLimitUnavailable
from .bump_fit import historical_sem_floor
from .bump_presets import bump_preset
from .dispersion import DispersionService,DispersionOptions,QM_PROBE_PRESETS_A
from .qm_checkpoint import load_checkpoint,prepare_destination,save_checkpoint


def control(form,label,value,minimum,maximum,decimals=3):
    spin=QDoubleSpinBox();spin.setDecimals(decimals);spin.setRange(minimum,maximum);spin.setValue(value)
    spin.setMaximumWidth(150)
    form.addRow(label,spin);return spin


def probe_quality_text(quality):
    if not quality:return ''
    if quality.get('skipped'):return 'Skipped: '+quality['reason']
    return f"{quality['orbit_response_rms_mm']*1000:.3g} / {quality['target_change_mm']*1000:.3g}"


class CorrectionTool(QWidget):
    plot_type=Plot
    update=pyqtSignal(object)
    def __init__(self,window):
        super().__init__();self.window=window;self.result=None
        layout=QVBoxLayout(self);split=QSplitter();self.splitter=split;layout.addWidget(split)
        left=QWidget();self.controls=QVBoxLayout(left);self.form=QFormLayout();self.controls.addLayout(self.form)
        right=QWidget();self.right=QVBoxLayout(right);self.plot=self.plot_type();self.right.addWidget(self.plot,1)
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
        rows=list(rows)
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
        self.last_stage_message=None
        activity=QGroupBox('Current operation');activity_layout=QVBoxLayout(activity)
        self.activity_label=QLabel('Idle — Choose a bump position, then calibrate.')
        self.activity_label.setWordWrap(True);self.activity_label.setStyleSheet('font-size:15px;font-weight:bold;')
        self.activity_currents=QLabel();self.activity_currents.setWordWrap(True)
        self.acquisition_progress=QProgressBar();self.acquisition_progress.setRange(0,10)
        self.acquisition_progress.setFormat('BPM acquisition: idle');self.acquisition_progress.setValue(0)
        self.closure_status=QLabel('Calibration: 1 Baseline → 2 Measure 8 correctors → 3 Solve currents → 4 Close ±Y bumps → 5 Restore')
        self.closure_status.setWordWrap(True)
        for widget in (self.activity_label,self.activity_currents,self.acquisition_progress,self.closure_status):activity_layout.addWidget(widget)
        self.bpm_health=QLabel();self.bpm_health.setWordWrap(True)
        activity_layout.addWidget(self.bpm_health);self.bpm_health.hide()
        self.right.insertWidget(0,activity)
        self.availability=None
        self.after_task=None;self.queue=[];self.running_queue=False;self.completed=[];self.skipped=[]
        self.rank_started=False;self.scan_resume=None;self.plot_target=''
        self.tempfiles_directory=Path('/mnt/nas1/atf-data/op/tempfiles')
        self.run_data_directory=Path('/mnt/nas1/atf-data/flight-simulator/DRTuning')
        targets=QGroupBox('SD/SF by |trim current|')
        target_layout=QVBoxLayout(targets)
        self.queue_list=QListWidget()
        self.queue_list.setMinimumHeight(125)
        target_layout.addWidget(self.queue_list)
        target_actions=QHBoxLayout();target_layout.addLayout(target_actions)
        rank_button=QPushButton('Refresh ranking');rank_button.clicked.connect(self.rank)
        self.queue_button=QPushButton('Run checked in order');self.queue_button.clicked.connect(self.start_queue)
        target_actions.addWidget(rank_button);target_actions.addWidget(self.queue_button)
        self.queue_button.setEnabled(False)
        self.queue_list.itemChanged.connect(self.refresh_queue_button)
        self.target=QComboBox();self.target.addItems([f'{f}.{i}' for f in ('SD1R','SF1R') for i in range(1,35)])
        self.form.addRow('SD / SF bump position',self.target)
        self.amplitude=control(self.form,'Scan width [mm]',.8,.02,4.)
        self.amplitude.setToolTip('Full scan width. Try centered and shifted windows containing zero, then narrow down to 0.2 mm.')
        self.refine=control(self.form,'Refinement ± [mm]',.1,.001,1.)
        self.refine.setToolTip('Candidate offset from the fitted minimum. Measure one side at a time. Edge minima can expand the full window to 1 mm.')
        self.probe=control(self.form,'Corrector probe ± [A]',.05,.001,.5)
        self.step=control(self.form,'Max change from baseline [A]',2.,.001,5.)
        self.leak=control(self.form,'Outside COD max [µm]',30.,.1,1000.,1)
        self.rms=control(self.form,'Outside COD RMS [µm]',20.,.1,1000.,1)
        self.settle=control(self.form,'Settle before acquisition [s]',1.,0.,60.)
        self.controls.insertWidget(0,QLabel('BPM: 10 updates / XSR: 10 updates\nXSR:PROJ_V_SIGMA — vertical size [µm]'))
        self.range_status=QLabel(self.scan_width_text())
        self.range_status.setWordWrap(True);self.controls.insertWidget(1,self.range_status)
        self.preset_status=QLabel();self.preset_status.setWordWrap(True);self.controls.insertWidget(2,self.preset_status)
        self.availability_status=QLabel('Positions not checked against current BPMs / currents.');self.availability_status.setWordWrap(True)
        self.controls.insertWidget(3,self.availability_status)
        self.controls.insertWidget(0,targets)
        matrix_box=QGroupBox('Shared corrector response')
        matrix_layout=QVBoxLayout(matrix_box)
        actions=QHBoxLayout();matrix_layout.addLayout(actions)
        measure_matrix=QPushButton('Measure all ZH/ZV')
        measure_matrix.clicked.connect(self.measure_matrix);actions.addWidget(measure_matrix)
        load_matrix=QPushButton('Load matrix…')
        load_matrix.clicked.connect(self.load_matrix);actions.addWidget(load_matrix)
        self.matrix=None
        self.use_matrix=QCheckBox('Use shared matrix');self.use_matrix.setEnabled(False)
        self.use_matrix.toggled.connect(self.toggle_matrix);matrix_layout.addWidget(self.use_matrix)
        self.controls.insertWidget(1,matrix_box)
        self.add_button('Check BPMs / current limits',self.check_positions)
        self.calibrate_button=self.add_button('1. Calibrate and close bump',self.calibrate)
        self.scan_button=self.add_button('2. Scan → fit → set optimum',self.scan)
        self.reset_button=self.add_button('Restore baseline currents',self.reset)
        self.scan_button.setEnabled(False);self.reset_button.setEnabled(False)
        for widget in (self.amplitude,self.refine,self.probe,self.step,self.leak,self.rms,self.settle):widget.valueChanged.connect(self.settings_changed)
        self.target.currentIndexChanged.connect(self.invalidate)
        self.status.setText('Choose a bump position, then calibrate its preset BPMs / correctors.\nTarget mm uses saved nominal transport; corrector response is measured here.')
        self.preview_preset();self.redraw()
    def toggle_matrix(self,enabled):
        self.service.response_matrix=self.matrix if enabled else None
        self.invalidate()

    def matrix_ready(self,matrix,path):
        self.matrix=matrix
        self.use_matrix.setText(f"Use shared matrix ({len(matrix['columns'])} correctors)")
        skipped=matrix.get('skipped',{})
        summary=str(path)
        if skipped:summary+='\nSkipped (current limits): '+', '.join(skipped)
        self.use_matrix.setToolTip(summary);self.use_matrix.setEnabled(True)
        if self.use_matrix.isChecked():self.toggle_matrix(True)
        else:self.use_matrix.setChecked(True)
        self.status.setText(summary);self.window.log(summary)

    def measure_matrix(self):
        opt=self.options();options=self.model_options()
        directory=self.run_data_directory/f'{datetime.now():%Y}'
        path=directory/f'bump_response_{datetime.now():%Y%m%d_%H%M%S_%f}.json'
        state={}
        def work(progress):
            self.service.last_result=None
            prepare_destination(path)
            matrix=self.service.measure_response_matrix(opt,options,state,progress,self.update.emit)
            save_checkpoint(path,matrix)
            return matrix
        self.run(work,lambda matrix:self.matrix_ready(matrix,path),'Measure shared bump response')

    def load_matrix(self):
        path,_=QFileDialog.getOpenFileName(self,'Load measured bump response',
            str(self.run_data_directory/f'{datetime.now():%Y}'),'Bump response (bump_response_*.json);;JSON (*.json)')
        if not path:return
        def work(_):
            self.service.last_result=None
            matrix=json.loads(Path(path).read_text(encoding='utf-8'))
            self.service.use_response_matrix(matrix)
            return matrix
        self.run(work,lambda matrix:self.matrix_ready(matrix,path),'Load shared bump response')

    def options(self):
        return BumpOptions(amplitude_mm=self.amplitude.value()/2,refine_mm=self.refine.value(),
            calibration_mm=min(.1,self.amplitude.value()/2),probe_A=self.probe.value(),max_step_A=self.step.value(),
            outside_max_mm=self.leak.value()/1000,outside_rms_mm=self.rms.value()/1000,settle_s=self.settle.value())
    def model_options(self):
        options=copy.deepcopy(self.window.panels['dispersion'].options)
        options.model_match_tunes=False
        return options
    def scan_width_text(self):
        widths=' → '.join(f'{width:.3f}' for width in self.options().scan_widths_mm)
        return f'Full scan width [mm]: {widths}. Center may shift.'
    def preview_preset(self):
        target=self.target.currentData() or self.target.currentText()
        preset=bump_preset(target)
        self.preset_status.setText('Default BPMs: '+', '.join(preset['internal_bpms'])+'\nDefault correctors: '+', '.join(preset['actuators'])+
            '\nSaved nominal transport; calibration measures the current response.' if preset else 'No bump position selected.')
    def clear_availability(self):
        self.availability=None
        for i in range(self.queue_list.count()):
            item=self.queue_list.item(i)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEnabled)
        for i in range(self.target.count()):
            item=self.target.model().item(i);item.setEnabled(True);item.setToolTip('Not checked against current BPMs / currents')
        self.availability_status.setText('Positions not checked against current BPMs / currents.')
        if self.target.currentIndex()<0 and self.target.count():self.target.setCurrentIndex(0)
    def settings_changed(self,*_):
        self.clear_availability();self.invalidate()
    def invalidate(self,*_):
        self.geometry=None;self.calibration=None;self.scan_button.setEnabled(False)
        index=self.target.currentIndex()
        self.calibrate_button.setEnabled(index>=0 and self.target.model().item(index).isEnabled())
        self.range_status.setText(self.scan_width_text())
        self.preview_preset()
        target=self.target.currentData() or self.target.currentText()
        self.activity_label.setText('Idle — '+target+' preset ready for calibration.' if index>=0 else 'No available bump position.')
        self.activity_currents.clear();self.acquisition_progress.setValue(0);self.acquisition_progress.setFormat('BPM acquisition: idle')
        self.closure_status.setText('Calibration: 1 Baseline → 2 Measure response → 3 Solve currents → 4 Close ±Y → 5 Restore')
    def check_positions(self):
        opt=self.options();model_options=self.model_options()
        self.clear_availability();self.invalidate()
        self.activity_label.setText('Checking BPMs and current margins for all positions; no magnet writes')
        self.run(lambda p:self.service.available_targets(opt,model_options,p,self.update.emit),self.positions_checked,'Check bump positions (read-only)')
    def positions_checked(self,report):
        self.invalidate();self.availability=report
        rows={row['target']:row for row in report['rows']};enabled=[]
        self.target.blockSignals(True)
        for i in range(self.target.count()):
            target=self.target.itemData(i) or self.target.itemText(i);row=rows.get(target)
            item=self.target.model().item(i);available=bool(row and row['available']);item.setEnabled(available)
            if available:
                enabled.append(i);item.setToolTip('Available at last check; BPMs: '+', '.join(row['internal_bpms']))
            else:item.setToolTip(row.get('reason','Not checked') if row else 'Not checked')
        if self.target.currentIndex() not in enabled:self.target.setCurrentIndex(enabled[0] if enabled else -1)
        self.target.blockSignals(False);self.calibrate_button.setEnabled(bool(enabled));self.preview_preset()
        for i in range(self.queue_list.count()):
            item=self.queue_list.item(i)
            entry=rows.get(item.data(Qt.ItemDataRole.UserRole),{})
            if not entry.get('available'):
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(entry.get('reason','Not checked'))
        self.availability_status.setText(f'Last check: {len(enabled)}/{self.target.count()} positions available. Gray = unavailable.\nRecheck after machine settings change; scan width still requires measured closure.')
        self.set_table(['Target','Available','Reason / BPMs'],[(r['target'],'Yes' if r['available'] else 'No',
            ', '.join(r['internal_bpms']) if r['available'] else r['reason']) for r in report['rows']])
        self.status.setText('Preset BPMs and current budget checked. Minimum predicted scan width '+f'{self.options().scan_widths_mm[-1]:.3f} mm. No magnet changed.')
    def rank(self):
        def done(rows):
            previous=set(self.checked_targets()) if self.queue_list.count() else None
            self.clear_availability();self.invalidate();self.target.blockSignals(True);self.target.clear()
            self.queue_list.clear()
            for row in rows:
                if not row['available']:continue
                label=f"{row['name']}   {row['current_A']:+.4f} A"
                self.target.addItem(label,row['name'])
                item=QListWidgetItem(label,self.queue_list)
                item.setData(Qt.ItemDataRole.UserRole,row['name'])
                checked=row['name'] in previous if previous is not None else self.queue_list.count()<=5
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.refresh_queue_button()
            self.target.blockSignals(False)
            self.invalidate()
            for row in rows:
                if not row['available']:self.window.log(row['name']+': '+row.get('error','Readback unavailable'))
            self.status.setText('Sorted by absolute trim readback current, descending; signed currents shown.')
        self.activity_label.setText('Reading SD/SF currents and ranking by magnitude; no magnet writes')
        self.run(lambda _:self.service.ranked_targets(),done,'Read SD/SF trim currents')

    def showEvent(self,event):
        super().showEvent(event)
        if not self.rank_started and self.window.backend.unit('skews')=='A':
            self.rank_started=True
            QTimer.singleShot(0,self.rank)

    def checked_targets(self):
        items=(self.queue_list.item(i) for i in range(self.queue_list.count()))
        return [item.data(Qt.ItemDataRole.UserRole) for item in items if item.checkState()==Qt.CheckState.Checked]

    def refresh_queue_button(self,*_):
        self.queue_button.setEnabled(bool(self.checked_targets()) and not self.running_queue)

    def start_queue(self):
        self.queue=self.checked_targets()
        if not self.queue:return
        self.completed=[];self.skipped=[];self.running_queue=True;self.refresh_queue_button()
        self.next_target()

    def next_target(self):
        if not self.queue:
            self.running_queue=False;self.refresh_queue_button()
            summary='Completed: '+(', '.join(row['target'] for row in self.completed) or '0')
            if self.skipped:summary+='\nSkipped: '+', '.join(row['target'] for row in self.skipped)
            self.status.setText(summary);self.activity_label.setText(summary)
            return
        index=self.target.findData(self.queue[0])
        if index<0 or not self.target.model().item(index).isEnabled():
            self.recovery('Selected target unavailable: '+self.queue[0],self.next_target)
            return
        self.target.setCurrentIndex(index)
        for i in range(self.queue_list.count()):
            if self.queue_list.item(i).data(Qt.ItemDataRole.UserRole)==self.queue[0]:
                self.queue_list.setCurrentRow(i)
                break
        self.calibrate()

    def after_worker(self,action):
        if self.window.tabs.isEnabled():QTimer.singleShot(0,action)
        else:self.after_task=action

    def run(self,action,done,label):
        recovery_data=None
        target=self.queue[0] if self.running_queue else self.target.currentData() or self.target.currentText()
        def work(progress):
            try:
                if recovery_data and recovery_data.get('restoration_error'):
                    self.service.reset(recovery_data)
                    self.service.verify_geometry(recovery_data.get('pending_geometry') or recovery_data.get('calibration',recovery_data))
                    recovery_data.pop('restoration_error',None)
                return action(progress)
            except Exception as exc:
                partial=self.service.last_result
                if isinstance(exc,BumpRangeUnavailable) and partial and partial.get('restored') and not partial.get('restoration_error'):
                    g=partial.get('pending_geometry') or partial.get('calibration',partial)
                    exhausted_helpers=self.running_queue and len(g.get('closure_helpers',()))==2
                    if g.get('target')==target and (isinstance(exc,BumpLimitUnavailable) or exhausted_helpers):
                        try:
                            self.service.verify_geometry(g)
                            self.service.check()
                        except Cancelled as cancel:
                            return dict(bump_error=str(cancel),cancelled=True,partial=partial)
                        except Exception as check:
                            partial.update(restored=False,restoration_error=str(check))
                            return dict(bump_error='Baseline verification failed: '+str(check),cancelled=False,partial=partial)
                        return dict(skip_target=g['target'],reason=str(exc),partial=partial)
                return dict(bump_error=str(exc),cancelled=isinstance(exc,Cancelled),partial=partial)
        def finished(value):
            nonlocal recovery_data
            if isinstance(value,dict) and 'skip_target' in value:
                self.finish_skipped(value);return
            if not isinstance(value,dict) or 'bump_error' not in value:
                done(value);return
            partial=value['partial']
            recovery_data=partial
            self.on_update(dict(kind='failure',data=partial))
            self.window.error(value['bump_error'])
            if value['cancelled']:
                self.running_queue=False;self.queue=[];self.refresh_queue_button();self.invalidate();return
            if partial and partial.get('schema')=='dr-coupling-bump-1' and (partial.get('restored') or partial.get('restoration_error')):
                self.scan_resume=partial
            self.after_worker(lambda:self.recovery(value['bump_error'],lambda:self.window.start(work,finished,label=label)))
        self.window.start(work,finished,label=label)

    def finish_skipped(self,value):
        target=value['skip_target'];reason=value['reason']
        self.result=value['partial']
        directory=self.run_data_directory/f'{datetime.now():%Y}'
        path=directory/f'bump_skipped_{target}_{datetime.now():%Y%m%d_%H%M%S_%f}.json'
        record=dict(target=target,status='skipped',reason=reason,baseline_verified=True)
        try:
            directory.mkdir(parents=True,exist_ok=True)
            save_checkpoint(path,dict(record,result=self.result))
        except OSError as exc:
            message=f'{target}: failed to save skipped target: {exc}'
            self.after_worker(lambda:self.recovery(message,lambda:self.finish_skipped(value)))
            return
        self.skipped.append(dict(record,path=str(path)))
        self.window.log(f'{target} skipped. Baseline verified. {reason}\n{path}')
        self.scan_resume=None
        self.invalidate();self.reset_button.setEnabled(False)
        self.status.setText(f'{target} skipped: {reason}. Baseline verified.')
        self.activity_label.setText(self.status.text())
        if self.running_queue:
            self.queue.pop(0);self.after_worker(self.next_target)

    def recovery(self,message,retry):
        box=QMessageBox(self)
        box.setWindowTitle('Bump paused')
        box.setText((self.plot_target+'\n' if self.plot_target else '')+message)
        end=box.addButton('Save and end',QMessageBox.ButtonRole.RejectRole)
        resume=box.addButton('Resume',QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(end);box.exec()
        if box.clickedButton() is resume:
            retry();return
        data=dict(error=message,completed=self.completed,skipped=self.skipped,pending=self.queue,partial=self.result,
                  live_samples=[dict(phase=phase,amplitude_mm=a,xsr=xsr) for (phase,a),xsr in self.samples.items()])
        directory=self.run_data_directory/f'{datetime.now():%Y}'
        dest=directory/f'bump_{datetime.now():%Y%m%d_%H%M%S_%f}.json'
        while True:
            try:
                dest.parent.mkdir(parents=True,exist_ok=True)
                temporary=dest.with_name(dest.name+'.part')
                temporary.write_text(json.dumps(serial(data),allow_nan=False),encoding='utf-8')
                temporary.replace(dest)
                self.window.log('Saved '+str(dest))
                break
            except OSError as exc:
                self.window.error(str(exc))
                path,_=QFileDialog.getSaveFileName(self,'Save interrupted bump run',dest.name,'JSON (*.json)')
                if not path:
                    QTimer.singleShot(0,lambda:self.recovery(message,retry))
                    return
                dest=Path(path)
        self.running_queue=False;self.queue=[];self.refresh_queue_button();self.invalidate()

    def finish_scan(self,result):
        from matplotlib.figure import Figure
        target=result['calibration']['target']
        try:
            figure=Figure(figsize=(8,5),layout='constrained')
            self.draw_xsr(figure.subplots(),target,result['points'],result['fits'],
                          [(p['amplitude_mm'],p['xsr']) for p in result['points']])
            self.tempfiles_directory.mkdir(parents=True,exist_ok=True)
            dest=self.tempfiles_directory/f'bump_{target}_{datetime.now():%Y%m%d_%H%M%S_%f}.png'
            figure.savefig(dest,dpi=150)
            if result['fits'] and result['fits'][-1].get('point_count'):
                points=result['points'];fit=result['fits'][-1]
                summary=dict(schema='dr-bump-xsr-history-1',target=target,machine=self.window.backend.identity,
                    median_sem_um=float(np.median([p['xsr']['sem'] for p in points[:-1]])),
                    verification_error_um=result['final']['xsr']['mean']-fit['predicted_size_um'],
                    points=[dict(amplitude_mm=p['amplitude_mm'],requested_amplitude_mm=p['requested_amplitude_mm'],
                                 phase=p['phase'],mean_um=p['xsr']['mean'],sem_um=p['xsr']['sem']) for p in points],
                    fits=result['fits'],sem_floor_um=result['sem_floor_um'],xsr_bounds_mm=result['xsr_bounds_mm'])
                save_checkpoint(dest.with_suffix('.xsr.json'),summary)
        except Exception as exc:
            message='Optimum applied. XSR plot export failed: '+str(exc)
            self.after_worker(lambda:self.recovery(message,lambda:self.finish_scan(result)))
            return
        self.window.log('Saved '+str(dest))
        self.completed.append(dict(target=target,result=result,plot=str(dest)))
        self.scan_resume=None
        if self.running_queue:
            self.queue.pop(0)
            self.after_worker(self.next_target)
    def calibrate(self):
        if self.target.currentIndex()<0 or not self.target.model().item(self.target.currentIndex()).isEnabled():
            self.window.error('Choose an available bump position; recheck BPMs / current limits');return
        target=self.target.currentData() or self.target.currentText();opt=self.options();options=self.model_options()
        self.plot_target=target;self.scan_resume=None
        if not self.running_queue:self.completed=[];self.skipped=[]
        self.geometry=None;self.calibration=None;self.result=None;self.scan_button.setEnabled(False)
        self.points=[];self.fits=[];self.samples={};self.last_orbit=None;self.last_stage_message=None
        self.redraw()
        self.activity_label.setText(target+': Starting calibration — Checking preset and BPMs.')
        self.activity_currents.clear();self.acquisition_progress.setValue(0)
        self.closure_status.setText('Single-corrector probes also move outside COD. Y bump closure follows response measurement.')
        def done(value):
            self.geometry=value;self.calibration=value;self.result=value;self.scan_button.setEnabled(True)
            self.set_table(['Corrector','Baseline [A]','Measured knob [A/mm]'],zip(value['actuators'],value['centers'],value['knob_A_per_mm']))
            shared=len(value.get('shared_columns',[]));total=len(value['actuators'])
            self.status.setText(f"{value['target']} calibrated in both signs. Response: {shared} shared, "
                                f"{total-shared} measured here. Baseline restored. Ready for XSR scan.")
            self.activity_label.setText(value['target']+': Calibration complete; baseline currents verified. Ready to scan.')
            if self.running_queue:self.after_task=self.scan
        self.run(lambda p:self.service.calibrate_target(target,opt,options,p,self.update.emit),done,'Calibrate preset bump')
    def scan(self):
        if self.calibration is None:return
        opt=self.options();g=copy.deepcopy(self.calibration)
        self.points=[];self.samples={};self.fits=[];self.redraw()
        def done(value):
            if value.get('skip_reason'):
                self.finish_skipped(dict(skip_target=g['target'],reason=value['skip_reason'],partial=value));return
            self.calibration=value['calibration']
            self.result=value;self.scan_button.setEnabled(False);self.calibrate_button.setEnabled(False);self.reset_button.setEnabled(True)
            self.status.setText(f"Set {value['final_amplitude_mm']:+.4f} mm; XSR {value['final']['xsr']['mean']:.3f} µm.\nCalibrate again before another scan.")
            self.activity_label.setText(f"Scan complete — Optimum Y {value['final_amplitude_mm']:+.4f} mm applied; currents remain at the optimum.")
            self.finish_scan(value)
        def measure(p):
            if not self.scan_resume:
                self.service.xsr_history_floor_um=historical_sem_floor(self.tempfiles_directory,self.window.backend.identity)
                self.update.emit(dict(kind='scan_restart',target=g['target']))
            return self.service.scan(g,opt,p,self.update.emit,resume=self.scan_resume)
        self.run(measure,done,'Local bump / XSR minimization')
    def reset(self):
        if self.result is None:return
        data=self.result
        def done(_):
            self.invalidate();self.reset_button.setEnabled(False);self.status.setText('Baseline currents restored. Calibrate again.')
            self.activity_label.setText('Baseline restore commands sent. Ready to recalibrate.')
        self.activity_label.setText('Sending baseline-current restore commands to all correctors')
        self.run(lambda _:self.service.reset(data),done,'Restore local bump currents')
    def on_update(self,event):
        kind=event['kind']
        if kind=='bpm_health':
            held=', '.join(event['frozen'])
            self.bpm_health.setText('Temporarily excluded BPMs: '+held)
            self.bpm_health.setVisible(bool(held))
            if held:self.window.log(self.bpm_health.text())
            if event['recovered']:self.window.log('BPM updates resumed: '+', '.join(event['recovered']))
            return
        if kind=='scan_restart':
            self.plot_target=event['target'];self.points=[];self.samples={};self.fits=[];self.last_orbit=None
            self.redraw();return
        if kind=='xsr_bounds':
            low,high=event['bounds']
            self.range_status.setText(f'Expanded XSR window: {low:+.3f} to {high:+.3f} mm.');return
        if kind=='scan_history':
            self.plot_target=event['target'];self.points=list(event['points']);self.fits=list(event['fits'])
            self.samples={(p['phase'],p['amplitude_mm']):p['xsr'] for p in self.points}
            self.redraw();return
        if kind=='status':
            self.activity_label.setText(event['message'])
            if 'samples' in event:
                self.acquisition_progress.setRange(0,event['total']);self.acquisition_progress.setValue(event['samples'])
                self.acquisition_progress.setFormat(f"BPM updates: {event['samples']}/{event['total']}")
            else:
                self.acquisition_progress.setRange(0,10);self.acquisition_progress.setValue(0)
                self.acquisition_progress.setFormat('BPM acquisition: idle')
            if 'magnets' in event:
                currents=[];readbacks=event.get('readback_A')
                for i,(n,base,command) in enumerate(zip(event['magnets'],event['baseline_A'],event['command_A'])):
                    text=f'{n}: baseline {base:+.4f} → command {command:+.4f} A'
                    if readbacks is not None:text+=f' / readback {readbacks[i]:+.4f} A'
                    currents.append(text)
                self.activity_currents.setText('\n'.join(currents))
            elif event['stage']!='bpm_retry':self.activity_currents.clear()
            marker=(event['stage'],event['message'])
            if marker!=self.last_stage_message:
                self.window.log(event['message']);self.last_stage_message=marker
            return
        if kind=='geometry':
            self.geometry=event['data'];return
        if kind=='sample':
            self.samples[(event['phase'],event['amplitude_mm'])]=event['xsr']
            self.status.setText(f"{event['phase']}: {event['amplitude_mm']:+.4f} mm\nXSR updates {event['xsr']['count']}/10; mean {event['xsr']['mean']:.3f} µm")
            self.activity_label.setText(f"Acquiring XSR: {event['phase']} / Y {event['amplitude_mm']:+.4f} mm")
            self.acquisition_progress.setRange(0,10);self.acquisition_progress.setValue(event['xsr']['count'])
            self.acquisition_progress.setFormat(f"XSR updates: {event['xsr']['count']}/10")
        elif kind=='point':
            point=event['point'];self.points.append(point)
            self.samples[(point['phase'],point['amplitude_mm'])]=point['xsr']
        elif kind=='fit':self.fits[event.get('index',len(self.fits)):]=[event['fit']]
        elif kind=='orbit':
            self.last_orbit=event
            state=event['state'];closed='Closure passed' if event.get('closed') else 'Checking / correcting closure'
            limits=f"Limits: max {self.leak.value():g} µm / RMS {self.rms.value():g} µm"
            planes=np.asarray(event['outside']).reshape(2,-1)*1000
            planes=planes[:,np.isfinite(planes).all(axis=0)]
            cod='\n'.join(f'{axis}: max {max(abs(row)):.2f} µm / RMS {np.sqrt(np.mean(row**2)):.2f} µm'
                           for axis,row in zip(('H','V'),planes))
            self.closure_status.setText(f"{closed} / correction {event['iteration']}\n"
                f"Estimated delta Y {state[2]:+.4f} mm (target {event['amplitude_mm']:+.4f}) / delta X {state[0]:+.4f} mm\n"
                f"Outside COD\n{cod}\n{limits}")
        elif kind=='range':
            selected=event['scan_range'];low,high=selected['bounds_mm']
            self.range_status.setText(f'Scan range {low:+.3f} to {high:+.3f} mm; width {selected["width_mm"]:.3f} mm'+
                (' (reduced)' if selected['fallback'] else '')+'.')
        elif kind=='failure':
            if event.get('data') is not None:self.result=event['data']
            self.scan_button.setEnabled(False);self.calibrate_button.setEnabled(False)
            if self.result is not None:self.reset_button.setEnabled(bool(self.result.get('restoration_error')))
            self.status.setText('Operation failed / cancelled. See restoration status in the detailed log; acquired data can be saved.')
            self.range_status.setText('Scan stopped. See saved range checks and restoration status.')
            data=event.get('data') or {}
            restoration=('Restoration failed: '+data['restoration_error'] if data.get('restoration_error') else
                         'Baseline restore commands sent' if data.get('restored') else 'Check detailed log for restoration status')
            previous=self.activity_label.text()
            self.activity_label.setText('Stopped / '+restoration+'\nLast operation: '+previous+('\n'+str(data['error']) if data.get('error') else ''))
            self.acquisition_progress.setFormat('Acquisition stopped')
        self.redraw()
    @staticmethod
    def draw_xsr(ax,target,points,fits,samples):
        for a,xsr in samples:
            ax.scatter(np.full(xsr['count'],a),xsr['values'],s=9,alpha=.3,color='C0')
        for i,p in enumerate(points):
            ax.errorbar(p['amplitude_mm'],p['xsr']['mean'],yerr=p['xsr']['sem'],fmt='o',color='C0',
                        label='10-update mean ± SEM' if i==0 else None)
        for i,fit in enumerate(fits):
            best=fit['best_mm'];bracketed=fit.get('bracketed',True)
            label=f'Fit {i+1}: '+(f'{best:+.4f} mm' if bracketed else 'minimum not bracketed')
            if i==len(fits)-1 and bracketed and fit.get('ci95_mm'):
                lo,hi=fit['ci95_mm'];label+=f' (95% CI {lo:+.3f} to {hi:+.3f})'
            ax.plot(fit['amplitude_mm'],fit['size_um'],'--' if i==0 else '-',label=label)
            if bracketed:ax.axvline(best,color=f'C{i+1}',alpha=.4)
        ax.set(xlabel='Vertical displacement at SD/SF [mm]',ylabel='XSR σy [µm]',title=f'{target}  XSR σy')
        if points or fits:ax.legend(fontsize=8)
        ax.grid(alpha=.2)

    def redraw(self):
        figure=self.plot.figure;figure.clear();ax,orbit=figure.subplots(2,1)
        self.draw_xsr(ax,self.plot_target,self.points,self.fits,[(a,xsr) for (_,a),xsr in self.samples.items()])
        if self.last_orbit is not None:
            event=self.last_orbit;g=self.calibration or self.geometry
            if g is not None:
                n=len(g['outside_indices']);s=np.asarray(g['positions_m'])[g['outside_indices']]
                orbit.plot(s,event['outside'][:n],'.',label='Outside ΔX');orbit.plot(s,event['outside'][n:],'.',label='Outside ΔY')
                orbit.legend(fontsize=8)
            orbit.set_title(f"a={event['amplitude_mm']:+.3f} mm / max {event['outside_max_mm']:.4f} mm / RMS {event['outside_rms_mm']:.4f} mm")
        orbit.axhline(self.leak.value()/1000,color='gray',ls=':');orbit.axhline(-self.leak.value()/1000,color='gray',ls=':')
        orbit.set(xlabel='Outside BPM s [m]',ylabel='COD change [mm]');orbit.grid(alpha=.2)
        self.plot.canvas.draw_idle()


class QMDispersionPanel(CorrectionTool):
    plot_type=DispersionPlot
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
        correction=section('Correction')
        self.step=control(correction,'Max change / Apply [A]',defaults.step_A,.01,3.,2)
        self.step.setSingleStep(.01)
        self.step.setToolTip('Maximum absolute current change per Apply. Absolute current limits come from parameter A/B.')
        self.selection=QComboBox();self.selection.addItem('SVD — all QMs','all');self.selection.addItem('Greedy — select QMs','greedy')
        correction.addRow('Solver',self.selection)
        self.coil=QComboBox();self.coil.addItem('Trim','trim');self.coil.addItem('Main','main')
        correction.addRow('QM coil',self.coil)
        self.active_coil='trim'
        self.coil_exclusions={'trim':', '.join(defaults.fixed_actuators),'main':''}
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
        self.calc_button.setToolTip('Measure all selected QMs at each RF setting. Repeat only probes that need a different width.')
        self.calc_button.setStyleSheet('font-weight: 600;')
        self.set_button.setStyleSheet('font-weight: 600;')
        self.bpm_button,self.recalculate_button=button_row([
            ('Select BPMs…',lambda:self.window.open_tool('selection')),('Recalculate',self.recalculate)])
        self.recalculate_button.setToolTip('Reuse measured responses. After Apply, calculate the next correction from the measured dispersion / COD and applied currents.')
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
        self.show_initial_probes()
        self.coil.currentIndexChanged.connect(self.change_coil)
        for w in (self.maximum,self.probe,self.step,self.target_snr,self.rf,self.ridge):
            w.setMaximumWidth(105);w.valueChanged.connect(self.invalidate)
        self.selection.currentIndexChanged.connect(lambda _:correction.setRowVisible(self.maximum,self.selection.currentData()=='greedy'))
        self.selection.currentIndexChanged.connect(self.invalidate)
        self.excluded.textChanged.connect(self.invalidate)
    def show_initial_probes(self):
        opt=self.options()
        self.set_table(['QM '+opt.coil,'Initial probe [A]'],[(n,f'{QM_PROBE_PRESETS_A.get(n,opt.probe_A):.2f}')
                       for n in opt.actuators()])
    def change_coil(self):
        self.coil_exclusions[self.active_coil]=self.excluded.text()
        self.active_coil=self.coil.currentData()
        self.excluded.setText(self.coil_exclusions[self.active_coil])
        if self.result and self.result['options'].get('coil','trim')==self.active_coil:
            self.display(self.result)
        else:
            self.plot.clear_data();self.show_initial_probes()
            for button in (self.set_button,self.recalculate_button,self.resume_button,self.reset_button,self.save_button):
                button.setEnabled(False)
            self.status.setText(f'{self.coil.currentText()} selected. Run CALC to measure the response.')
    def invalidate(self,*_):
        self.plot.set_excluded(self.window.panels['dispersion'].options.excluded_bpms)
        self.set_button.setEnabled(False)
        if self.result is None or self.result['options'].get('coil','trim')!=self.active_coil:
            self.show_initial_probes();return
        if 'response' in self.result:
            self.status.setText('Selection / options changed. Recalculate from measured response before Apply.\n'
                                'Changing RF settings or QM selection requires a new CALC.')
    def proposal_matches_options(self,data):
        saved=dict(data['options']);saved.setdefault('coil','trim');current=asdict(self.options())
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
            saved=data['options'];self.coil.setCurrentIndex(self.coil.findData(saved.get('coil','trim')))
            shared=set(self.window.panels['dispersion'].options.fixed_actuators)
            self.excluded.setText(', '.join(n for n in saved['fixed_actuators'] if n not in shared))
            self.service.restore_width_cache(data);self.service.last_result=data;self.display(data)
            self.save_status.setText('Loaded: '+Path(path).name)
            self.window.log('Loaded QM scan: '+path)
        except Exception as exc:self.window.error(str(exc))
    def resume_scan(self):
        if self.result is None:return
        data=self.result;opt=self.options();path=self.new_checkpoint_path();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.resume(data,opt,p,self.update.emit,checkpoint_path=path),
                 self.display,'Resume QM scan')
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
        return DispersionOptions(coil=self.coil.currentData(),probe_A=self.probe.value(),step_A=self.step.value(),frequency_change_khz=self.rf.value(),
            use_probe_presets=True,adaptive_probe=True,reuse_probe_widths=True,characterize_first_scan=False,
            target_snr=self.target_snr.value(),
            momentum_compaction=shared.momentum_compaction,measure_tunes=False,constrain_cod=False,
            regularization=self.ridge.value(),selection=self.selection.currentData(),maximum=int(self.maximum.value()),
            excluded_bpms=shared.excluded_bpms,fixed_actuators=tuple(sorted(set(shared.fixed_actuators)|{n.strip() for n in self.excluded.text().split(',') if n.strip()})))
    def calculate(self):
        opt=self.options();path=self.new_checkpoint_path();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.calculate(opt,p,self.update.emit,checkpoint_path=path,group_rf=True),self.display,'QM dispersion')
    def recalculate(self):
        if self.result is None:return
        data=self.result;opt=self.options();self.set_button.setEnabled(False)
        self.run(lambda p:self.service.recalculate(data,opt,p),self.display,'Recalculate QM correction')
    def apply(self):
        if self.result is None:return
        if not self.proposal_matches_options(self.result):
            self.invalidate();self.window.error('Selection / options changed; recalculate before Apply');return
        data=self.result;self.set_button.setEnabled(False)
        self.run(lambda p:self.service.apply(data,p,self.update.emit),self.applied,'Apply QM correction')
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
        if data['options'].get('coil','trim')!=self.active_coil:return
        self.result=data;self.save_button.setEnabled(True)
        self.plot.correction(data)
        valid=np.asarray(data['valid'],bool)
        if 'delta' in data:
            rows=[]
            for i,(n,c,d) in enumerate(zip(data['actuators'],data['centers'],data['delta'])):
                q=data.get('probe_quality',[]);q=q[i] if i<len(q) else None
                quality=probe_quality_text(q)
                snr=('Not measured' if q.get('skipped') else f"{q['snr']:.3g}" if q['snr'] is not None else 'No measured scatter') if q else ''
                estimate=data.get('successful_probe_widths',{}).get(n,{}).get('width_A')
                rows.append((n,f'{c:.5f}',f'{d:+.5f}',f'{c+d:.5f}',quality,snr,f'{estimate:.2f}' if estimate is not None else ''))
            self.set_table(['QM '+self.active_coil,'Before [A]','Delta [A]','Proposed [A]','Response / target [µm]','S/N','Next probe [A]'],rows)
            self.set_button.setEnabled(bool(not data.get('applied') and not data.get('error') and
                                       self.proposal_matches_options(data) and np.any(abs(np.asarray(data['delta']))>1.e-12)))
            if data.get('applied'):message='Applied. Dispersion remeasured. Recalculate for the next iteration.'
            elif not self.proposal_matches_options(data):message='Settings changed. Recalculate before Apply.'
            elif not np.any(abs(np.asarray(data['delta']))>1.e-12):message='No correction proposed.'
            else:message=f"Ready to apply (iteration {data.get('iteration_index',0)+1})."
            self.status.setText(message+f'\nFit BPMs: {valid.sum()} / {len(valid)}')
        else:
            self.set_button.setEnabled(False)
            state=data.get('scan_state',{});done=len(data['rf_results']) if 'rf_results' in data else state.get('completed',0)
            self.status.setText(f"Measured: {done}/{len(data['actuators'])} QMs.")
            qualities=dict(zip(data['actuators'],data.get('probe_quality',[])))
            qualities.update({n:r['quality'] for n,r in data.get('rf_results',{}).items()})
            estimates=data.get('successful_probe_widths',{})
            self.set_table(['QM '+self.active_coil,'Scan','Response / target [µm]','Next probe [A]'],
                [(n,('Skipped' if qualities[n].get('skipped') else 'Complete') if n in qualities else 'Pending',
                  probe_quality_text(qualities.get(n)),
                  f"{estimates[n]['width_A']:.2f}" if n in estimates else '')
                 for n in data['actuators']])
        if data.get('error'):
            self.status.setText(('Baseline restored.' if data.get('restored') else 'Operation failed; inspect restoration log.')+'\n'+data['error'])
        elif data.get('proposal_unavailable_reason'):self.status.setText('S/N below target. Run CALC to remeasure.')
        if data.get('skipped_qms'):
            self.status.setText(self.status.text()+'\nSkipped QM: '+', '.join(r['magnet'] for r in data['skipped_qms']))
        state=data.get('scan_state',{})
        self.resume_button.setEnabled(bool(state and state.get('status')!='complete' and not data.get('applied')))
        self.reset_button.setEnabled(bool(data.get('applied') or data.get('iteration_origin') or data.get('error') or state.get('active_magnet')))
        self.recalculate_button.setEnabled('response' in data and not data.get('restoration_error') and
                                           (not data.get('error') or bool(data.get('restored'))))
