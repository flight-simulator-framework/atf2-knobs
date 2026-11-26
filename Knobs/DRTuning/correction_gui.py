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
                            QDoubleSpinBox,QPushButton,QTableWidget,QTableWidgetItem,QFileDialog,QLineEdit,QGroupBox,QScrollArea,QSizePolicy,QProgressBar,QListWidget,QListWidgetItem,QMessageBox)
from Knobs.DRCorrection.core import serial,Cancelled
from Knobs.DRCorrection.gui import Plot
from .presentation import ControlSplitter, MatrixProgress, plot_and_results
from .dispersion_plot import DispersionPlot, OP_TEMPFILES_DIRECTORY, save_dispersion_png
from .dispersion_source import QM_FILES,local_time,matrix_caption,observation_from_result
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
        layout=QVBoxLayout(self);split=ControlSplitter(layout);self.splitter=split
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
    def suggested_save_path(self,data):
        return getattr(self,'autosave_directory','')
    def save(self):
        data=self.result
        if data is None:self.window.error('No acquired data to save');return
        path,_=QFileDialog.getSaveFileName(self,'Save correction measurements',str(self.suggested_save_path(data)),'JSON (*.json)')
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
        self.pause_requested=False;self.rank_started=False;self.scan_resume=None;self.plot_target=''
        self.tempfiles_directory=OP_TEMPFILES_DIRECTORY
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
        single=QGroupBox('Single target')
        single_layout=QVBoxLayout(single)
        single_layout.addWidget(self.target)
        self.amplitude=control(self.form,'Scan width [mm]',.8,.02,4.)
        self.amplitude.setToolTip('Full scan width. Try centered and shifted windows containing zero, then narrow down to 0.2 mm.')
        self.refine=control(self.form,'Refinement ± [mm]',.1,.001,1.)
        self.refine.setToolTip('Candidate offset from the fitted minimum. Measure one side at a time. Edge minima can expand the full window to 1 mm.')
        self.probe=control(self.form,'Corrector probe ± [A]',.05,.001,.5)
        self.bump_snr=control(self.form,'Minimum response S/N',5.,1.,100.)
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
        self.use_matrix=QCheckBox('Use matrix with current beam');self.use_matrix.setEnabled(False)
        self.use_matrix.toggled.connect(self.toggle_matrix);matrix_layout.addWidget(self.use_matrix)
        self.controls.insertWidget(1,matrix_box)
        self.add_button('Check BPMs / current limits',self.check_positions)
        self.calibrate_button=self.add_button('1. Calibrate and close bump',self.calibrate)
        self.scan_button=self.add_button('2. Scan → fit → set optimum',self.scan)
        single_layout.addWidget(self.calibrate_button)
        single_layout.addWidget(self.scan_button)
        self.controls.insertWidget(1,single)
        self.reset_button=self.add_button('Restore baseline currents',self.reset)
        for action in (self.queue_button,self.calibrate_button,self.scan_button):action.setProperty('primary',True)
        self.scan_button.setEnabled(False);self.reset_button.setEnabled(False)
        for widget in (self.amplitude,self.refine,self.probe,self.bump_snr,self.step,self.leak,self.rms,self.settle):widget.valueChanged.connect(self.settings_changed)
        self.target.currentIndexChanged.connect(self.invalidate)
        self.status.setText('Choose a bump position, then calibrate its preset BPMs / correctors.\nTarget mm uses saved nominal transport; corrector response is measured here.')
        self.preview_preset();self.redraw()
    def toggle_matrix(self,enabled):
        if enabled:self.service.use_response_matrix(self.matrix,current_beam=True)
        else:self.service.response_matrix=None
        self.invalidate()

    def matrix_ready(self,matrix,path):
        self.matrix=matrix
        self.use_matrix.setText(f"Use matrix with current beam ({len(matrix['columns'])} correctors)")
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
        self.run(work,lambda matrix:self.matrix_ready(matrix,path),'Measure shared bump response',pausable=True)

    def load_matrix(self):
        path,_=QFileDialog.getOpenFileName(self,'Load measured bump response',
            str(self.run_data_directory/f'{datetime.now():%Y}'),'Bump response (bump_response_*.json);;JSON (*.json)')
        if not path:return
        self.run(lambda _:self.read_matrix(path),lambda matrix:self.matrix_ready(matrix,path),'Load shared bump response')

    def read_matrix(self,path):
        with Path(path).open(encoding='utf-8') as stream:matrix=json.load(stream)
        self.service.validate_response_matrix(matrix)
        return matrix

    def options(self):
        return BumpOptions(amplitude_mm=self.amplitude.value()/2,refine_mm=self.refine.value(),
            calibration_mm=min(.1,self.amplitude.value()/2),probe_A=self.probe.value(),target_snr=self.bump_snr.value(),max_step_A=self.step.value(),
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

    def run(self,action,done,label,*,pausable=False):
        recovery_data=None
        def start():
            self.pause_requested=False
            self.window.start(work,finished,label=label,pausable=pausable)

        target=self.queue[0] if self.running_queue else self.target.currentData() or self.target.currentText()
        def work(progress):
            try:
                if recovery_data and recovery_data.get('restoration_error'):
                    self.service.reset(recovery_data)
                    self.service.verify_geometry(recovery_data.get('pending_geometry') or recovery_data.get('calibration',recovery_data))
                    recovery_data.pop('restoration_error',None)
                self.service.last_result=None
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
            if not isinstance(value,dict) or 'bump_error' not in value:
                if isinstance(value,dict) and 'skip_target' in value:self.finish_skipped(value)
                else:done(value)
                # A request can arrive after the worker has committed the point or target.
                if self.pause_requested and self.after_task is not None:
                    next_action=self.after_task
                    self.after_task=lambda:self.recovery('Paused before the next operation.',next_action)
                else:self.pause_requested=False
                return
            partial=value['partial']
            recovery_data=partial
            self.on_update(dict(kind='failure',data=partial))
            message=value['bump_error']
            if self.pause_requested and value['cancelled'] and not (partial and partial.get('restoration_error')):
                points=len((partial or {}).get('points',()))
                message=f'Operator pause · {points} completed XSR points retained.'
                if partial and partial.get('restored'):message+=' Baseline restore commands completed.'
                self.activity_label.setText(message)
                self.window.operation_status.setText(message)
                self.window.log(message)
            else:
                self.window.error(message)
            if value['cancelled'] and not self.pause_requested:
                self.running_queue=False;self.queue=[];self.refresh_queue_button();self.invalidate();return
            if partial and partial.get('schema')=='dr-coupling-bump-1' and (partial.get('restored') or partial.get('restoration_error')):
                self.scan_resume=partial
            self.after_worker(lambda:self.recovery(message,start))
        start()

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
        self.pause_requested=False
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

    def finish_scan(self,result,*,stamp=None):
        from matplotlib.figure import Figure
        stamp=stamp or datetime.now()
        target=result['calibration']['target']
        try:
            figure=Figure(figsize=(8,5),layout='constrained')
            self.draw_xsr(figure.subplots(),target,result['points'],result['fits'],
                          [(p['amplitude_mm'],p['xsr']) for p in result['points']])
            self.tempfiles_directory.mkdir(parents=True,exist_ok=True)
            dest=self.tempfiles_directory/f'bump_{target}_{stamp:%Y%m%d_%H%M%S_%f}.png'
            figure.savefig(dest,dpi=150)
            if result['fits'] and result['fits'][-1].get('point_count'):
                points=result['points'];fit=result['fits'][-1]
                summary=dict(schema='dr-bump-xsr-history-1',target=target,machine=self.window.backend.identity,
                    median_sem_um=float(np.median([p['xsr']['sem'] for p in points[:-1]])),
                    verification_error_um=result['final']['xsr']['mean']-fit['predicted_size_um'],
                    points=[dict(amplitude_mm=p['amplitude_mm'],requested_amplitude_mm=p['requested_amplitude_mm'],
                                 phase=p['phase'],mean_um=p['xsr']['mean'],sem_um=p['xsr']['sem']) for p in points],
                    fits=result['fits'],sem_floor_um=result['sem_floor_um'],xsr_bounds_mm=result['xsr_bounds_mm'])
                history=self.run_data_directory/f'{stamp:%Y}'/dest.with_suffix('.xsr.json').name
                history.parent.mkdir(parents=True,exist_ok=True)
                save_checkpoint(history,summary)
        except Exception as exc:
            message='Optimum applied. XSR result export failed: '+str(exc)
            self.after_worker(lambda:self.recovery(message,lambda:self.finish_scan(result,stamp=stamp)))
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
        self.run(lambda p:self.service.calibrate_target(target,opt,options,p,self.update.emit),done,'Calibrate preset bump',pausable=True)
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
                self.service.xsr_history_floor_um=historical_sem_floor(self.run_data_directory,self.window.backend.identity,legacy_directory=self.tempfiles_directory)
                self.update.emit(dict(kind='scan_restart',target=g['target']))
            return self.service.scan(g,opt,p,self.update.emit,resume=self.scan_resume)
        self.run(measure,done,'Local bump / XSR minimization',pausable=True)
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
    matrix_activity=pyqtSignal(object)
    def __init__(self,window):
        super().__init__(window);self.service=DispersionService(window.backend,window.service.cancel)
        self.tempfiles_directory=OP_TEMPFILES_DIRECTORY
        self.plot.dispersion_plane='x'
        self.table.setMaximumHeight(16777215)
        self.result_splitter=plot_and_results(self.plot,self.table)
        self.right.addWidget(self.result_splitter,1)
        self.matrix_progress=MatrixProgress()
        self.matrix_activity.connect(self.matrix_progress.show_activity)
        self.service.reader.matrix_activity=self.matrix_activity.emit
        self.right.insertWidget(0,self.matrix_progress)
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
        self.step=control(correction,'Max change / Apply [A]',defaults.step_A,.01,np.finfo(float).max,2)
        self.step.setSingleStep(.01)
        self.step.setToolTip('Maximum absolute current change per Apply. Absolute current limits come from parameter A/B.')
        self.selection=QComboBox();self.selection.addItem('SVD — all QMs','all');self.selection.addItem('Greedy — select QMs','greedy')
        correction.addRow('Solver',self.selection)
        self.coil=QComboBox();self.coil.addItem('Trim','trim');self.coil.addItem('Main','main')
        correction.addRow('QM coil',self.coil)
        self.coil_matrices={}
        self.active_coil='trim'
        self.matrix=None;self.matrix_path=None;self.observation=None
        self.coil_exclusions={'trim':', '.join(defaults.fixed_actuators),'main':''}
        self.maximum=control(correction,'Maximum QMs',defaults.maximum,1,16,0)
        correction.setRowVisible(self.maximum,False)
        self.excluded=QLineEdit(', '.join(defaults.fixed_actuators));correction.addRow('Exclude QMs',self.excluded)
        self.dispersion_weight=control(correction,'Straight dispersion weight',defaults.dispersion_weight,0.,1000.,4)
        self.orbit_weight=control(correction,'COD change weight',defaults.orbit_weight,0.,1000.,4)
        self.orbit_weight.setToolTip('COD X/Y change at MB19R, MB22R–MB24R, MB65R and MB66R.')
        self.arc_weight=control(correction,'Arc dispersion RMS weight',defaults.arc_weight,0.,1000.,4)
        for field in (self.dispersion_weight,self.orbit_weight,self.arc_weight):field.setSingleStep(.01)
        self.arc_weight.setToolTip('Zero disables the arc target. Recalculate fits the changed weights using the same measurements.')
        self.arc_parity=QComboBox()
        self.arc_parity.addItem('Odd MB numbers','odd');self.arc_parity.addItem('Even MB numbers','even')
        correction.addRow('Arc BPMs',self.arc_parity)
        self.arc_parity.currentIndexChanged.connect(self.invalidate)
        self.ridge=control(correction,'Regularization',defaults.regularization,0.,1.,4)
        self.ridge.setToolTip('Larger values reduce correction from weak responses.')
        def button_row(items):
            row=QHBoxLayout();self.buttons.addLayout(row);buttons=[]
            for label,slot in items:
                button=QPushButton(label);button.setMinimumHeight(32);button.clicked.connect(slot)
                row.addWidget(button);buttons.append(button)
            return buttons
        self.calc_button,self.load_button=button_row([
            ('Measure QM matrix',self.calculate),('Select matrix…',self.load_scan)])
        self.matrix_label=QLabel('No matrix selected');self.matrix_label.setWordWrap(True)
        self.buttons.addWidget(self.matrix_label)
        self.update_dispersion_button,self.recalculate_button=button_row([
            ('Update dispersion',self.update_dispersion),('Recalculate',self.recalculate)])
        self.controls.removeItem(self.buttons)
        self.controls.insertLayout(0,self.buttons)
        self.set_button,self.reset_button=button_row([
            ('Apply',self.apply),('Restore original QM / RF',self.reset)])
        self.set_button.setProperty('applyAction',True)
        for action in (self.calc_button,self.recalculate_button,self.set_button):action.setProperty('primary',True)
        self.bpm_button=QPushButton('Select BPMs…')
        self.bpm_button.clicked.connect(lambda:self.window.open_tool('selection'))
        correction.addRow(self.bpm_button)
        for button in (self.set_button,self.recalculate_button,self.reset_button):button.setEnabled(False)
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
        self.resume_button=QPushButton('Resume interrupted scan…')
        self.resume_button.clicked.connect(self.resume_interrupted)
        storage_layout.addWidget(self.resume_button)
        row=QHBoxLayout();storage_layout.addLayout(row)
        self.folder_button=QPushButton('Change folder…');self.folder_button.clicked.connect(self.choose_autosave_directory)
        row.addWidget(self.folder_button);row.addWidget(self.save_button)
        self.controls.insertWidget(self.controls.count()-1,storage)
        self.status.setText('Ready to measure.')
        self.status.setStyleSheet('padding: 6px; font-weight: 600;')
        self.table.setMaximumHeight(260)
        self.show_initial_probes()
        self.coil.currentIndexChanged.connect(self.change_coil)
        for w in (self.maximum,self.probe,self.step,self.target_snr,self.rf,self.ridge,self.dispersion_weight,self.orbit_weight,self.arc_weight):
            w.setMaximumWidth(105);w.valueChanged.connect(self.invalidate)
        self.selection.currentIndexChanged.connect(lambda _:correction.setRowVisible(self.maximum,self.selection.currentData()=='greedy'))
        self.selection.currentIndexChanged.connect(self.invalidate)
        self.excluded.textChanged.connect(self.invalidate)
    def show_initial_probes(self):
        opt=self.options()
        self.set_table(['QM '+opt.coil,'Initial probe [A]'],[(n,f'{QM_PROBE_PRESETS_A.get(n,opt.probe_A):.2f}')
                       for n in opt.actuators()])
    def change_coil(self):
        self.matrix_progress.hide()
        self.coil_exclusions[self.active_coil]=self.excluded.text()
        self.active_coil=self.coil.currentData()
        self.observation=None;self.matrix=None;self.matrix_path=None;self.matrix_label.setText('No matrix selected')
        self.excluded.setText(self.coil_exclusions[self.active_coil])
        if self.result and self.result['options'].get('coil','trim')==self.active_coil and self.result.get('scan_state',{}).get('status')!='complete':
            self.display(self.result)
        elif self.active_coil in self.coil_matrices:
            self.select_matrix(*self.coil_matrices[self.active_coil])
        else:
            self.result=None;self.plot.clear_data();self.show_initial_probes()
            for button in (self.set_button,self.recalculate_button,self.reset_button,self.save_button):
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
        saved=asdict(DispersionOptions.from_saved(data['options']));current=asdict(self.options())
        for key in ('excluded_bpms','fixed_actuators'):
            saved[key]=sorted(saved.get(key,()));current[key]=sorted(current[key])
        return saved==current
    def choose_autosave_directory(self):
        path=QFileDialog.getExistingDirectory(self,'Automatic save folder',str(self.autosave_directory))
        if path:
            if Path(path).resolve().is_relative_to(self.tempfiles_directory.resolve()):
                self.save_status.setText('Choose a data folder for QM measurements. The tempfiles folder is for plots.')
                return
            self.autosave_directory=Path(path);self.save_directory.setText(path);self.save_directory.setToolTip(path)
            self.save_status.setText('Save folder updated.')
    def qm_save_path(self,coil,*,checkpoint=False,stamp=None):
        stamp=stamp or datetime.now().astimezone()
        prefix='qm_scan' if checkpoint else 'qm_dispersion'
        suffix='.checkpoint.json' if checkpoint else '.json'
        return Path(self.autosave_directory)/f'{prefix}_{coil}_{stamp:%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}{suffix}'
    def suggested_save_path(self,data):
        return self.qm_save_path(data['options'].get('coil','trim'))
    def load_scan(self):
        path,_=QFileDialog.getOpenFileName(self,'Select measured QM matrix',str(self.autosave_directory),QM_FILES)
        if not path:return
        read=self.matrix_reader()
        self.window.start(lambda _:read(path),lambda data:self.select_matrix(data,path),label='Load measured QM matrix')

    def matrix_reader(self):
        opt=self.options()
        existing={**self.coil_exclusions,self.active_coil:self.excluded.text()}
        shared=set(self.window.panels['dispersion'].options.fixed_actuators)
        def read_matrix(path):
            with Path(path).open(encoding='utf-8') as stream:data=self.service.matrix_from_scan(json.load(stream))
            if (data['backend_id']!=self.window.backend.identity or data['names']!=list(self.window.backend.bpm_names)
                    or data['profile_signature']!=self.service.profile_signature()):
                raise ValueError('Machine, BPM order or hardware profile differs from the matrix')
            if data.get('restoration_error'):raise ValueError('Unresolved restoration error in the matrix')
            coil=data['acquisition_options'].get('coil','trim')
            # Match the columns actually present. Preserve the current solver controls.
            available=set(data['actuators'])
            missing=set(DispersionOptions(coil=coil,fixed_actuators=()).actuators())-available
            exclusions=sorted(missing|{n.strip() for n in existing[coil].split(',') if n.strip()})
            opt.coil=coil
            opt.fixed_actuators=tuple(shared|set(exclusions))
            self.service.check_reuse_options(data,opt,current_beam=True)
            return data
        return read_matrix

    def select_matrix(self,data,path):
        coil=data['acquisition_options'].get('coil','trim')
        self.coil_matrices[coil]=(data,path)
        if self.active_coil!=coil:
            self.coil.setCurrentIndex(self.coil.findData(coil))
            return
        self.matrix=data;self.matrix_path=path;self.result=None
        self.matrix_label.setText(matrix_caption(data,'QM '+coil,path,self.observation))
        self.plot.clear_data()
        if self.observation is not None and 'names' in self.observation:self.plot.correction(self.observation)
        self.set_table(['QM '+coil,'Measured column'],[(n,'Skipped' if rb.get('skipped') else 'Available') for n,rb in zip(data['actuators'],data['probe_readbacks'])])
        for button in (self.set_button,self.reset_button,self.save_button):button.setEnabled(False)
        self.recalculate_button.setEnabled(True)
        self.status.setText('Matrix selected. Recalculate for the current beam.')


    def update_dispersion(self):
        opt=self.options();self.observation=None
        self.set_button.setEnabled(False)
        def done(obs):
            self.observation=obs
            self.matrix_label.setText(matrix_caption(self.matrix,'QM '+self.active_coil,self.matrix_path,obs))
            self.plot.correction(obs);self.table.setRowCount(0);self.save_button.setEnabled(False)
            self.status.setText('Dispersion measured '+local_time(obs['measured_at']))
            self.export_plot('measured')
        self.window.start(lambda p:self.service.measure_current_dispersion(opt),done,label='Measure QM dispersion')

    def resume_interrupted(self):
        if self.result and self.result.get('scan_state',{}).get('status') in ('interrupted','probing','running'):
            self.resume_scan()
        else:
            if self.load_interrupted_checkpoint():self.resume_scan()

    def load_interrupted_checkpoint(self):
        path,_=QFileDialog.getOpenFileName(self,'Load QM scan checkpoint',str(self.autosave_directory),'QM checkpoint (*.checkpoint.json)')
        if not path:return
        try:
            data=self.service.validate_checkpoint(load_checkpoint(path))
            if data['scan_state']['status']=='complete':raise ValueError('Use Select matrix for a completed scan')
            saved=data['options'];self.coil.setCurrentIndex(self.coil.findData(saved.get('coil','trim')))
            shared=set(self.window.panels['dispersion'].options.fixed_actuators)
            self.excluded.setText(', '.join(n for n in saved['fixed_actuators'] if n not in shared))
            self.service.restore_width_cache(data);self.service.last_result=data;self.display(data)
            self.save_status.setText('Loaded: '+Path(path).name)
            self.matrix=None;self.matrix_path=None;self.observation=None
            self.matrix_label.setText('Interrupted scan')
            self.window.log('Loaded QM scan: '+path)
            return True
        except Exception as exc:self.window.error(str(exc))
    def resume_scan(self):
        if self.result is None:return
        data=self.result;opt=self.options();path=self.qm_save_path(opt.coil,checkpoint=True);self.set_button.setEnabled(False)
        self.run(lambda p:self.service.resume(data,opt,p,self.update.emit,checkpoint_path=path),
                 self.matrix_measured,'Resume QM scan')
    def export_plot(self,stage):
        save_dispersion_png(self,f'qm_dispersion_{self.active_coil}_{stage}')

    def matrix_measured(self,data):
        self.display(data)
        self.coil_matrices[self.active_coil]=(self.service.matrix_from_scan(data),self.matrix_path)
        self.export_plot('matrix')

    def applied(self,data):
        self.display(data)
        self.export_plot('applied')
        # File errors must never roll back an already verified machine correction.
        try:
            stamp=datetime.now().astimezone()
            dest=self.qm_save_path(data['options'].get('coil','trim'),stamp=stamp)
            dest.parent.mkdir(parents=True,exist_ok=True)
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
        fixed=set(shared.fixed_actuators)|{n.strip() for n in self.excluded.text().split(',') if n.strip()}
        if self.matrix is not None and self.matrix.get('matrix_only'):
            fixed.update(set(DispersionOptions(coil=self.active_coil,fixed_actuators=()).actuators())-set(self.matrix['actuators']))
        return DispersionOptions(coil=self.coil.currentData(),probe_A=self.probe.value(),step_A=self.step.value(),frequency_change_khz=self.rf.value(),
            use_probe_presets=True,adaptive_probe=True,reuse_probe_widths=True,characterize_first_scan=False,
            target_snr=self.target_snr.value(),
            momentum_compaction=shared.momentum_compaction,measure_tunes=False,constrain_cod=False,
            dispersion_weight=self.dispersion_weight.value(),orbit_weight=self.orbit_weight.value(),
            arc_weight=self.arc_weight.value(),arc_parity=self.arc_parity.currentData(),
            regularization=self.ridge.value(),selection=self.selection.currentData(),maximum=int(self.maximum.value()),
            excluded_bpms=shared.excluded_bpms,fixed_actuators=tuple(sorted(fixed)))
    def calculate(self):
        self.matrix=None;self.matrix_path=None;self.observation=None;self.matrix_label.setText('Measuring matrix…')
        opt=self.options();path=self.qm_save_path(opt.coil,checkpoint=True);self.set_button.setEnabled(False)
        self.run(lambda p:self.service.calculate(opt,p,self.update.emit,checkpoint_path=path,group_rf=True),self.matrix_measured,'QM dispersion')
    def recalculate(self):
        data=self.matrix
        if data is None:return
        opt=self.options();obs=self.observation
        self.set_button.setEnabled(False)
        keep_iterations=data is self.result
        def done(result):
            self.display(result)
            self.export_plot('recalculated')
        self.window.start(lambda p:self.service.reuse_on_current_beam(data,opt,p,observation=obs,keep_iterations=keep_iterations),done,
                          label='Recalculate QM correction')

    def apply(self):
        if self.result is None:return
        if not self.proposal_matches_options(self.result):
            self.invalidate();self.window.error('Selection / options changed; recalculate before Apply');return
        if not self.set_button.isEnabled():return
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
            elif data.get('baseline_measured_at') and not data.get('iteration_index'):
                stamp=datetime.fromisoformat(data['baseline_measured_at']).astimezone()
                message=f'Current beam measured {stamp:%Y-%m-%d %H:%M:%S}. Ready to apply.'
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
        self.arc_parity.setToolTip(', '.join(data.get('arc_bpms',[])))
        if data.get('error'):
            self.status.setText(('Baseline restored.' if data.get('restored') else 'Operation failed; inspect restoration log.')+'\n'+data['error'])
        elif data.get('proposal_unavailable_reason'):self.status.setText('S/N below target. Run CALC to remeasure.')
        if data.get('skipped_qms'):
            self.status.setText(self.status.text()+'\nSkipped QM: '+', '.join(r['magnet'] for r in data['skipped_qms']))
        state=data.get('scan_state',{})
        if state.get('status')=='complete' and 'response' in data:
            if not data.get('response_reused') and self.matrix is not data:self.matrix_path=data.get('checkpoint_file')
            self.matrix=data
            self.observation=observation_from_result(data)
            self.matrix_label.setText(matrix_caption(data,'QM '+self.active_coil,self.matrix_path,self.observation))
        self.reset_button.setEnabled(bool(data.get('applied') or data.get('iteration_origin') or data.get('error') or state.get('active_magnet')))
        self.recalculate_button.setEnabled('response' in data and not data.get('restoration_error') and
                                           (not data.get('error') or bool(data.get('restored'))))
