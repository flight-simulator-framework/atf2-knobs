"""Vertical dispersion correction with RFTrack selection and measured ZV response."""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                             QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from Knobs.DRCorrection.assessment import changed_indices
from Knobs.DRCorrection.gui import CorrectionPanel
from .correction_gui import DispersionPlot, control
from .presentation import scroll, table_style


class VerticalDispersionPanel(CorrectionPanel):
    plot_type = DispersionPlot
    baseline_observed = pyqtSignal(object)

    def __init__(self, window, mode):
        super().__init__(window, mode)
        self.baseline_observed.connect(self.plot.correction)
        self.options.count_v = 5
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
        self.set_screening_options()
        self.screening.currentIndexChanged.connect(self.change_screening)
        form.addRow('ZV selection model', self.screening)
        maximum = QSpinBox()
        maximum.setRange(1, max(1, len(window.backend.vcorrectors)))
        maximum.setValue(self.options.count_v)
        form.addRow('Maximum ZVs', maximum)
        maximum.valueChanged.connect(lambda v: self.set_option('count_v', v))
        for key, label, low, high in (
                ('frequency_change_khz', 'RF span [kHz]', .01, 10.),
                ('corrector_probe_A', 'Initial probe [A]', .001, 10.),
                ('step_A', 'Max correction [A]', .001, 10.)):
            field = control(form, label, getattr(self.options, key), low, high)
            field.valueChanged.connect(lambda value, k=key: self.set_option(k, value))

        left.addWidget(self.proposal_summary)
        self.find_button = QPushButton('1. Find ZVs (RFTrack)')
        self.measure_button = QPushButton('2. Measure selected ZVs')
        self.apply_button = QPushButton('3. Apply / remeasure')
        for widget, action in ((self.find_button, self.calculate),
                               (self.measure_button, self.measure_candidates),
                               (self.apply_button, self.apply)):
            left.addWidget(widget)
            widget.clicked.connect(lambda _, slot=action: slot())
        row = QHBoxLayout()
        left.addLayout(row)
        select = QPushButton('Select BPMs / magnets…')
        select.clicked.connect(window.selection)
        row.addWidget(select)
        row.addWidget(self.recalculate_button)
        self.recalculate_button.setText('Recalculate')
        self.undo_button = QPushButton('Undo last SET')
        self.undo_button.clicked.connect(self.reset)
        left.addWidget(self.undo_button)
        row = QHBoxLayout()
        left.addLayout(row)
        for label, action in [('Load target…', self.load_target), ('Save data + plots…', self.save_bundle)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _, slot=action: slot())
            row.addWidget(button)
        left.addStretch()
        right = QWidget()
        layout = QVBoxLayout(right)
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
            self.plot.clear_data()
        super().invalidate(clear_model_response)

    def change_screening(self):
        self.response_cache.clear()
        self.set_screening_options()
        self.invalidate()

    def set_screening_options(self):
        choice = self.screening.currentData()
        self.options.model_match_tunes = choice == 'fit'
        self.options.response_source = 'rftrack_nominal' if choice == 'nominal' else 'rftrack'

    def calculate(self, dataset=None):
        self.set_screening_options()
        self.response_cache.clear()
        super().calculate(dataset)

    def measure_candidates(self):
        preview = self.data
        if not preview or preview['options']['response_source'] not in ('rftrack', 'rftrack_nominal') or not preview['selected']:
            return
        self.window.start(lambda p: self.window.service.measure_selected_zvs(preview, p),
                          self.display, label='Measure selected ZVs', pausable=True)

    def display(self, data):
        self.options.response_source = data['options']['response_source']
        if self.options.response_source in ('rftrack', 'rftrack_nominal') and not data.get('set_blocked_reason'):
            data['set_blocked_reason'] = 'Measure the selected ZVs before Apply.'
        super().display(data)
        if data.get('applied'):
            self.response_cache.clear()
            self.refresh()

    def refresh(self):
        super().refresh()
        if not hasattr(self, 'undo_button'):
            return
        data = self.data
        model = bool(data and data['options']['response_source'] in ('rftrack', 'rftrack_nominal'))
        measured = bool(data and data.get('rftrack_candidates') and
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
            message = 'Ready to select ZVs.'
        if model and reference:
            unmapped = reference['unmapped_bpms'] + reference['unmapped_zvs']
            if unmapped:
                message += '\nOutside reference: ' + ', '.join(unmapped)
        self.proposal_summary.setText(message)

    def apply(self):
        data = self.data
        if data is not None and self.apply_button.isEnabled():
            self.window.start(lambda progress: self.window.service.apply(data, progress), self.display)
