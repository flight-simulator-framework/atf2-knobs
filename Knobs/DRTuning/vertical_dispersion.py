"""Vertical dispersion correction with RFTrack selection and measured ZV response."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from Knobs.DRCorrection.assessment import changed_indices
from Knobs.DRCorrection.core import load_session
from Knobs.DRCorrection.gui import CorrectionPanel
from .dispersion_source import ZV_FILES,local_time,matrix_caption,measured_time,observation_from_result
from .correction_gui import DispersionPlot, control
from .presentation import MatrixProgress, scroll, table_style
from .qm_checkpoint import save_checkpoint
from .zv_rf_scan import eligible_zv, measure_all_zvs


class VerticalDispersionPanel(CorrectionPanel):
    plot_type = DispersionPlot
    matrix_activity = pyqtSignal(object)
    baseline_observed = pyqtSignal(object)

    def __init__(self, window, mode):
        super().__init__(window, mode)
        self.matrix=None;self.matrix_path=None;self.observation=None
        self.baseline_observed.connect(self.plot.correction)
        self.options.count_v = 5
        self.options.bpm_samples = 10
        self.options.preserve_vertical_dispersion_orbit = True
        self.options.dispersion_weight = 1.
        self.options.orbit_weight = .05
        old = self.layout().takeAt(0).widget()
        old.hide()
        self._legacy_container = old

        controls = QWidget()
        left = QVBoxLayout(controls)
        settings = QGroupBox('Measurement')
        form = QFormLayout(settings)
        left.addWidget(settings)
        self.screening = QComboBox()
        self.screening.addItem('Current optics (no tune fit)', 'imported')
        self.screening.addItem('Current optics + tune fit', 'fit')
        self.screening.addItem('Saved nominal RFTrack', 'nominal')
        self.screening.addItem('Full measured ZV matrix', 'all')
        self.screening.setToolTip('Eligible ZVs: 1–8, 17–32, 43–51. Unchecked magnets are skipped.')
        self.set_screening_options()
        self.screening.currentIndexChanged.connect(self.change_screening)
        form.addRow('Response measurement', self.screening)
        maximum = QSpinBox()
        maximum.setRange(1, max(1, sum(map(eligible_zv, window.backend.vcorrectors))))
        maximum.setValue(self.options.count_v)
        form.addRow('Maximum ZVs', maximum)
        self.measurement_form = form
        self.maximum = maximum
        maximum.valueChanged.connect(lambda v: self.set_option('count_v', v))
        for key, label, low, high in (
                ('frequency_change_khz', 'RF span [kHz]', .01, 10.),
                ('corrector_probe_A', 'Initial probe [A]', .001, 10.),
                ('dispersion_target_snr', 'Minimum response S/N', 1., 100.),
                ('step_A', 'Max correction [A]', .001, 10.)):
            field = control(form, label, getattr(self.options, key), low, high)
            field.valueChanged.connect(lambda value, k=key: self.set_option(k, value))

        fit_box=QGroupBox('Correction weights')
        fit=QFormLayout(fit_box)
        left.addWidget(fit_box)
        for key,label in (('dispersion_weight','Vertical dispersion'),('orbit_weight','Vertical COD change')):
            spin=control(fit,label,getattr(self.options,key),0.,1000.,4)
            spin.setSingleStep(.01)
            spin.valueChanged.connect(lambda value,k=key:self.set_option(k,value))
        left.addWidget(self.proposal_summary)
        self.find_button = QPushButton('1. Find ZVs (RFTrack)')
        self.measure_button = QPushButton('2. Measure selected ZVs')
        self.measure_button.setToolTip('Probe all selected ZVs at each RF setting, then restore the initial RF.')
        self.apply_button = QPushButton('3. Apply / remeasure')
        for widget,action in ((self.find_button,self.calculate),(self.measure_button,self.measure_candidates)):
            left.addWidget(widget)
            widget.clicked.connect(lambda _,slot=action:slot())
        row=QHBoxLayout();left.addLayout(row)
        load_response=QPushButton('Select ZV matrix…');load_response.clicked.connect(self.load_measured_response)
        row.addWidget(load_response)
        select=QPushButton('Select BPMs / magnets…');select.clicked.connect(window.selection);row.addWidget(select)
        self.matrix_label=QLabel('No matrix selected');self.matrix_label.setWordWrap(True);left.addWidget(self.matrix_label)
        row=QHBoxLayout();left.addLayout(row)
        self.update_dispersion_button=QPushButton('Update dispersion');self.update_dispersion_button.clicked.connect(self.update_dispersion)
        row.addWidget(self.update_dispersion_button)
        self.recalculate_button.setText('Recalculate');row.addWidget(self.recalculate_button)
        row=QHBoxLayout();left.addLayout(row)
        self.apply_button.setText('Apply / remeasure');self.apply_button.clicked.connect(self.apply);row.addWidget(self.apply_button)
        self.undo_button=QPushButton('Undo last SET');self.undo_button.clicked.connect(self.reset);row.addWidget(self.undo_button)
        row = QHBoxLayout()
        left.addLayout(row)
        for label, action in [('Load target…', self.load_target), ('Save data + plots…', self.save_bundle)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _, slot=action: slot())
            row.addWidget(button)
        left.addStretch()
        right = QWidget()
        layout = QVBoxLayout(right)
        self.matrix_progress=MatrixProgress()
        self.matrix_activity.connect(self.matrix_progress.show_activity)
        layout.addWidget(self.matrix_progress)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.result_tabs)
        self.result_tabs.setMaximumHeight(170)
        table_style(self.table)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        split = QSplitter()
        self.splitter = split
        area = scroll(controls)
        area.setMinimumWidth(340)
        split.addWidget(area)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([355, 900])
        self.layout().addWidget(split)
        self.refresh()

    def invalidate(self, clear_model_response=False):
        changed = self.plot.excluded != frozenset(self.options.excluded_bpms)
        if changed:
            self.plot.set_excluded(self.options.excluded_bpms)
        elif self.plot.data is not None:
            if getattr(self,'observation',None):self.plot.correction(self.observation)
            else:self.plot.clear_data()
        super().invalidate(clear_model_response)

    def change_screening(self):
        self.matrix_progress.hide()
        self.matrix=None;self.matrix_path=None;self.matrix_label.setText('No matrix selected')
        self.response_cache.clear()
        self.set_screening_options()
        self.invalidate()

    def set_screening_options(self):
        choice = self.screening.currentData()
        self.options.model_match_tunes = choice == 'fit'
        self.options.response_source = {'all': 'measured', 'nominal': 'rftrack_nominal'}.get(choice, 'rftrack')

    def calculation_options(self):
        options = super().calculation_options()
        options.plane = 'y'
        options.selection_method = 'all' if self.screening.currentData() == 'all' else 'greedy'
        outside = {n for n in self.window.backend.vcorrectors if not eligible_zv(n)}
        if self.matrix is not None and self.matrix.get('matrix_only') and options.response_source=='measured':
            outside.update(set(self.window.backend.vcorrectors)-set(self.matrix['actuators']))
        options.fixed_actuators = tuple(sorted(set(options.fixed_actuators) | outside))
        return options

    def calculate(self, dataset=None):
        self.set_screening_options()
        self.matrix=None;self.matrix_path=None;self.observation=None
        self.matrix_label.setText('No matrix selected');self.response_cache.clear()
        if self.screening.currentData() == 'all' and dataset is None:
            options = self.calculation_options()
            self.invalidate()
            self.measure_and_save(lambda p:measure_all_zvs(self.window.service,options,p),'Measure full ZV matrix')
        else:
            super().calculate(dataset)

    def measure_candidates(self):
        preview = self.data
        if not preview or preview['options']['response_source'] not in ('rftrack', 'rftrack_nominal') or not preview['selected']:
            return
        self.measure_and_save(lambda p:self.window.service.measure_selected_zvs(preview,p,group_rf=True),'Measure selected ZVs')

    def measure_and_save(self, acquisition, label):
        directory=Path(self.data_directory())
        def work(progress):
            data=acquisition(progress)
            path=directory/f'vertical_dispersion_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}.json'
            try:
                directory.mkdir(parents=True,exist_ok=True)
                save_checkpoint(path,data)
                return data,f'Saved ZV matrix: {path}',path
            except OSError as exc:
                return data,f'Matrix measured. Automatic save failed: {exc}. Use Save data + plots.',None
        def done(result):
            data,message,self.matrix_path=result
            self.display(data)
            self.proposal_summary.setText(self.proposal_summary.text()+'\n'+message)
            self.window.log(message)
        self.window.start(work,done,label=label,pausable=True)

    def data_directory(self):
        return f'/mnt/nas1/atf-data/flight-simulator/DRTuning/{datetime.now():%Y}'

    def save_bundle(self, path=None):
        stem = f'vertical_dispersion_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}'
        return super().save_bundle(path, stem=stem)

    def load_measured_response(self):
        path,_=QFileDialog.getOpenFileName(self,'Select measured vertical ZV matrix',self.data_directory(),ZV_FILES)
        if not path:return
        def read_matrix(progress):
            data=load_session(path)
            opts=data.get('options',{});full=bool(data.get('full_zv_candidates'))
            if (data.get('mode')!='dispersion' or opts.get('plane')!='y' or opts.get('response_source')!='measured'
                    or not (full or data.get('rftrack_candidates')) or data.get('kind')!='correctors'
                    or data.get('backend_id')!=self.window.backend.identity or list(data['names'])!=list(self.window.backend.bpm_names)
                    or data.get('actuator_unit')!=self.window.backend.unit('correctors')
                    or not data.get('actuators') or any(not eligible_zv(n) or n not in self.window.backend.vcorrectors for n in data['actuators'])):
                raise ValueError('Select a measured vertical ZV matrix for this machine')
            if data.get('set_blocked_reason'):raise ValueError(data['set_blocked_reason'])
            choice='all' if full else ('nominal' if data.get('reference_matrix') else 'imported')
            n=len(data['names']);m=len(data['actuators'])
            valid=np.asarray(data.get('response_valid',data.get('acquisition_valid',data['valid'])),bool)
            matrix=np.asarray(data['response'],float)
            if (len(set(data['actuators']))!=m or valid.shape!=(n,) or matrix.shape!=(2*n,m)
                    or not np.isfinite(matrix[np.tile(valid,2)]).all() or np.shape(data.get('full_response'))!=(4*n,m)):
                raise ValueError('Invalid ZV matrix dimensions or BPM data')
            keep=('schema','mode','options','acquisition_options','actuators','actuator_unit','backend_id','kind',
                  'names','positions_m','response','full_response','full_signal_labels','signal_labels','source',
                  'rftrack_candidates','full_zv_candidates','scan_errors','ignored_readbacks','constraints',
                  'model_settings','reference_matrix','matrix_measured_at','selection_model_sha256')
            stamp=measured_time(data)
            data={k:data[k] for k in keep if k in data}
            data['matrix_measured_at']=stamp
            data.update(matrix_only=True,valid=valid,response_valid=valid,target=np.zeros((2,n)))
            return choice,data
        def selected(result):
            choice,data=result
            self.screening.blockSignals(True);self.screening.setCurrentIndex(self.screening.findData(choice));self.screening.blockSignals(False)
            self.matrix=data;self.matrix_path=path
            self.options.response_source='measured'
            self.response_cache={('measured','y'):data}
            self.invalidate()
            self.matrix_label.setText(matrix_caption(data,'Vertical ZV',path,self.observation))
            if self.observation is not None and 'names' in self.observation:self.plot.correction(self.observation)
            self.proposal_summary.setText('Matrix selected. Recalculate for the current beam.')
        self.window.start(read_matrix,selected,label='Load measured ZV matrix')

    def update_dispersion(self):
        options=self.calculation_options();self.observation=None
        matrix=self.matrix or {}
        self.invalidate()
        def done(obs):
            self.observation=obs;self.plot.correction(obs)
            self.matrix_label.setText(matrix_caption(self.matrix,'Vertical ZV',self.matrix_path,obs))
            self.proposal_summary.setText('Dispersion measured '+local_time(obs['measured_at']))
        self.window.start(lambda p:self.window.service.observe_current_beam('dispersion',options,matrix.get('ignored_readbacks')),done,
                          label='Measure vertical dispersion')

    def recalculate(self):
        if self.matrix is None:return
        data=self.matrix;obs=self.observation;options=self.calculation_options();options.response_source='measured'
        self.invalidate()
        self.window.start(lambda p:self.window.service.reuse_on_current_beam(data,options,p,observation=obs),
                          self.display,label='Recalculate vertical correction')

    def display(self, data):
        full = bool(data.get('full_zv_candidates'))
        if full or (data.get('rftrack_candidates') and self.screening.currentData() == 'all'):
            choice = 'all' if full else ('nominal' if data.get('reference_matrix') else 'imported')
            self.screening.blockSignals(True)
            self.screening.setCurrentIndex(self.screening.findData(choice))
            self.screening.blockSignals(False)
        self.options.response_source = data['options']['response_source']
        if self.options.response_source in ('rftrack', 'rftrack_nominal') and not data.get('set_blocked_reason'):
            data['set_blocked_reason'] = 'Measure the selected ZVs before Apply.'
        if self.options.response_source=='measured' and (full or data.get('rftrack_candidates')):
            self.matrix=data;self.observation=observation_from_result(data)
            self.matrix_label.setText(matrix_caption(data,'Vertical ZV',self.matrix_path,self.observation))
        super().display(data)
        if data.get('applied'):
            self.response_cache.clear()
            if full or data.get('rftrack_candidates'):
                self.response_cache[('measured', 'y')] = data
            self.refresh()

    def refresh(self):
        super().refresh()
        if not hasattr(self, 'matrix_label'):
            return
        full = self.screening.currentData() == 'all'
        self.measurement_form.setRowVisible(self.maximum, not full)
        self.measure_button.setVisible(not full)
        self.find_button.setText('1. Measure all enabled ZVs' if full else '1. Find ZVs (RFTrack)')
        data = self.data
        model = bool(data and data['options']['response_source'] in ('rftrack', 'rftrack_nominal'))
        measured = bool(data and (data.get('rftrack_candidates') or data.get('full_zv_candidates')) and
                        data['options']['response_source'] == 'measured')
        matches = bool(data and set(data['options']['excluded_bpms']) == self.plot.excluded)
        missing = set(self.window.backend.bpm_names) - self.plot.excluded - set(data['names']) if model else set()
        reference = data.get('reference_matrix', {}) if model else {}
        missing.difference_update(reference.get('unmapped_bpms', ()))
        candidates = data['selected'] if model and matches and not missing else []
        self.measure_button.setEnabled(bool(candidates))
        ready = measured and matches and not data.get('applied') and not data.get('set_blocked_reason') and bool(len(changed_indices(data)))
        self.apply_button.setEnabled(ready)
        self.undo_button.setEnabled(bool(self.window.service.undo))
        if not self.response_cache.get(('measured', 'y')) or model:
            self.recalculate_button.setEnabled(False)

        if model and (not matches or missing):
            message = 'BPM selection changed. Find ZVs again.'
        elif measured and not matches and not data.get('applied'):
            message = 'BPM selection changed. Recalculate before Apply.'
        elif model:
            message = 'Selected ZVs: ' + ', '.join(candidates) if candidates else 'No ZV correction proposed.'
        elif measured:
            message = data.get('set_blocked_reason') or (
                'Applied. Dispersion remeasured.' if data.get('applied') else
                'Measured response ready to apply.' if ready else 'No correction proposed.')
        elif self.recalculate_button.isEnabled():
            message = 'Measured response available. Recalculate after changing BPMs.'
        else:
            message = 'Eligible ZVs: 1–8, 17–32, 43–51.' if full else 'Ready to select ZVs.'
        if full and measured and data.get('applied'):
            message = 'Applied. Recalculate uses the measured after-Apply dispersion and saved matrix.'
        if model and reference:
            unmapped = reference['unmapped_bpms'] + reference['unmapped_zvs']
            if unmapped:
                message += '\nOutside reference: ' + ', '.join(unmapped)
        self.proposal_summary.setText(message)

    def apply(self):
        data = self.data
        if data is not None and self.apply_button.isEnabled():
            self.window.start(lambda progress: self.window.service.apply(data, progress), self.display)
