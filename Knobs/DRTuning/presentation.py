"""Resizable layouts shared by the DR tuning screens."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QGroupBox,
    QScrollArea,QSplitter,QPushButton,QLabel,QComboBox,QDoubleSpinBox,QHeaderView,
    QAbstractItemView,QSpinBox)


STYLE = '''
QWidget { font-size: 11pt; }
QMainWindow { background: #f4f6f8; }
QGroupBox { font-weight: bold; border: 1px solid #cbd5e1; border-radius: 5px;
            margin-top: 12px; padding: 12px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; }
QPushButton { padding: 6px 10px; min-height: 20px; }
QPushButton[primary="true"] { background: #1764a0; color: white; border-radius: 4px; }
QPushButton[primary="true"]:disabled { background: #94a3b8; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { padding: 4px; min-height: 20px; }
QTableWidget { background: white; gridline-color: #dbe1e8; }
QHeaderView::section { padding: 6px; background: #e8edf3; color: #172b4d; }
QListWidget { background: white; border: 1px solid #cbd5e1; }
QListWidget::item { padding: 10px 6px; }
QListWidget::item:selected { background: #dbeafe; color: #123b66; }
'''


def button(text,action,primary=False):
    w=QPushButton(text);w.clicked.connect(lambda _:action());w.setProperty('primary',primary)
    return w


def group(title):
    box=QGroupBox(title);layout=QVBoxLayout(box);layout.setSpacing(8)
    return box,layout


def scroll(widget):
    area=QScrollArea();area.setWidgetResizable(True);area.setWidget(widget)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    return area


def table_style(table):
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setDefaultSectionSize(36)


def reflow_correction(panel):
    """Retain the tested actions, replace the legacy absolute-position form."""
    old=panel.layout().takeAt(0).widget();old.hide()
    panel._legacy_container=old
    controls=QWidget();left=QVBoxLayout(controls);panel.modern_fields={}
    box,layout=group('1. Measurement settings');left.addWidget(box);form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows);layout.addLayout(form)
    plane=QComboBox();plane.addItems(['Horizontal (X)','Vertical (Y)']);plane.setCurrentIndex(0 if panel.options.plane=='x' else 1)
    plane.currentIndexChanged.connect(lambda i:panel.set_option('plane','x' if i==0 else 'y'))
    if panel.mode!='skew':form.addRow('Plane',plane)
    if panel.mode!='skew':
        for key,label in [('count_h','Maximum H correctors for SET'),('count_v','Maximum V correctors for SET')]:
            spin=QSpinBox();spin.setRange(1,500);spin.setValue(getattr(panel.options,key))
            spin.valueChanged.connect(lambda value,k=key:panel.set_option(k,value))
            form.addRow(label,spin);panel.modern_fields[key]=spin
    source=QComboBox();source.addItem('Measured magnet response','measured');source.addItem('RFTrack model response','rftrack')
    source.setCurrentIndex(source.findData(panel.options.response_source))
    source.currentIndexChanged.connect(lambda i:panel.set_option('response_source',source.itemData(i)))
    form.addRow('Response source',source);panel.response_source_box=source
    from PyQt6.QtWidgets import QCheckBox
    for key,label in [('model_match_tunes','Match model tunes to input targets'),
                      ('model_regional_targets','SAD straight-section targets (H)'),
                      ('model_fix_extraction_orbit','Preserve predicted extraction orbit'),
                      ('model_fix_extraction_dispersion','Preserve predicted extraction dispersion')]:
        if panel.mode=='skew' and key!='model_match_tunes':continue
        if key=='model_fix_extraction_dispersion' and panel.mode!='dispersion':continue
        check=QCheckBox(label);check.setChecked(getattr(panel.options,key))
        check.toggled.connect(lambda v,k=key:panel.set_option(k,v));form.addRow(check)
        check.setEnabled(source.currentData()=='rftrack')
        source.currentIndexChanged.connect(lambda _,w=check:w.setEnabled(source.currentData()=='rftrack'))
    samples=QSpinBox();samples.setRange(1,1000);samples.setValue(panel.options.bpm_samples)
    samples.valueChanged.connect(lambda value:panel.set_option('bpm_samples',value))
    form.addRow('BPM samples per setting',samples);panel.modern_fields['bpm_samples']=samples
    interval=QDoubleSpinBox();interval.setRange(0.,60.);interval.setDecimals(2);interval.setValue(panel.options.bpm_interval_s)
    interval.setToolTip('Wait before every BPM read, including the first. Average samples; save their spread.')
    interval.valueChanged.connect(lambda value:panel.set_option('bpm_interval_s',value))
    form.addRow('BPM interval [s]',interval);panel.modern_fields['bpm_interval_s']=interval
    keys=[('corrector_probe_A','Initial probe ± [A]'),('step_A','Max correction [A]')]
    if panel.mode=='dispersion':
        keys += [('frequency_change_khz','RF full span [kHz]')]
        panel.compaction_label=QLabel();panel.compaction_label.setWordWrap(True)
        form.addRow('Momentum compaction',panel.compaction_label)
    if panel.mode=='skew':
        keys=[('skew_probe','Initial skew probe ± [A]'),('skew_step','Max correction [A]'),('change1_A','H probe 1 span [A]'),('change2_A','H probe 2 span [A]')]
        from PyQt6.QtWidgets import QCheckBox
        for key in ('sd','sf'):
            check=QCheckBox(key.upper());check.setChecked(getattr(panel.options,key))
            check.toggled.connect(lambda v,k=key:panel.set_option(k,v));form.addRow('Supply family',check)
        for key in ('probe1','probe2'):
            combo=QComboBox();combo.addItems(panel.window.backend.hcorrectors);combo.setCurrentText(getattr(panel.options,key))
            combo.currentTextChanged.connect(lambda v,k=key:panel.set_option(k,v));form.addRow(key,combo)
    for key,label in keys:
        spin=QDoubleSpinBox();spin.setDecimals(5);spin.setRange(.00001,1000.);spin.setValue(getattr(panel.options,key))
        if key in ('corrector_probe_A','skew_probe'):
            spin.setToolTip('Starting amplitude for adaptive scans; fixed amplitude when adaptation is off. Supply limits always apply.')
        spin.valueChanged.connect(lambda v,k=key:panel.set_option(k,v));form.addRow(label,spin)
        panel.modern_fields[key]=spin
    adaptive=QCheckBox('Adapt probe to BPM response (SysID rule)')
    adaptive.setChecked(panel.options.adaptive_probe)
    adaptive.toggled.connect(lambda v:panel.set_option('adaptive_probe',v))
    form.addRow(adaptive);panel.modern_fields['adaptive_probe']=adaptive
    adaptive_widgets=[]
    unit=panel.window.backend.unit('skews' if panel.mode=='skew' else 'correctors')
    for key,label,factor,low,high in [
        ('adaptive_target_mm','Target BPM response [mm]',1.,.00001,10.),
        ('adaptive_target_tolerance','Target tolerance [%]',100.,1.,99.),
        ('adaptive_max_probe',f'Max probe ± [{unit}]',1.,.000001,5.)]:
        spin=QDoubleSpinBox();spin.setDecimals(6 if key=='adaptive_max_probe' else 4)
        spin.setProperty('option_scale',factor)
        spin.setRange(low,high);spin.setValue(getattr(panel.options,key)*factor)
        spin.valueChanged.connect(lambda v,k=key,f=factor:panel.set_option(k,v/f))
        form.addRow(label,spin);panel.modern_fields[key]=spin;adaptive_widgets.append(spin)
    pairs=QSpinBox();pairs.setRange(1,100);pairs.setValue(panel.options.adaptive_max_pairs)
    pairs.valueChanged.connect(lambda v:panel.set_option('adaptive_max_pairs',v))
    form.addRow('Max pairs per magnet',pairs);panel.modern_fields['adaptive_max_pairs']=pairs;adaptive_widgets.append(pairs)
    helptext=QLabel('Response = max |BPM(+) − BPM(−)| / 2 on enabled valid BPMs.\n'
        'Dispersion uses nominal COD; skew uses the vertical probe-response change.\n'
        'Each pair restores the magnet. Limits / unreached targets are reported.')
    helptext.setWordWrap(True);form.addRow(helptext)
    def update_adaptive_controls(*_):
        measured=source.currentData()=='measured';adaptive.setEnabled(measured)
        for widget in adaptive_widgets:widget.setEnabled(measured and adaptive.isChecked())
        helptext.setEnabled(measured)
    source.currentIndexChanged.connect(update_adaptive_controls)
    adaptive.toggled.connect(update_adaptive_controls);update_adaptive_controls()
    panel.refresh_adaptive_controls=update_adaptive_controls
    layout.addWidget(button('Select BPMs / magnets',panel.window.selection))
    panel.selection_label=QLabel();panel.selection_label.setWordWrap(True);layout.addWidget(panel.selection_label)
    layout.addWidget(button('SVD / advanced settings',panel.advanced))
    box,layout=group('2. Measure and correct');left.addWidget(box)
    note=QLabel('CALC reads the beam. Measured response also scans correction magnets.\nModel response scans magnets only in RFTrack; dispersion/skew still use real diagnostic excitation.\nSET applies the proposal separately.');note.setWordWrap(True);layout.addWidget(note)
    prefix='pushButtonDispersion' if panel.mode=='dispersion' else 'pushButton'
    for suffix,title in [('Calc','Measure response (CALC)'),('CalcFile','Calculate from file'),('Set','Apply correction (SET)'),('Reset','Undo correction (RESET)')]:
        w=getattr(panel.form,prefix+suffix);w.setMinimumSize(0,0);w.setMaximumSize(16777215,16777215)
        w.setText(title);layout.addWidget(w)
    box,layout=group('3. Save and analyze');left.addWidget(box)
    layout.addWidget(button('Save data and plots',panel.save_bundle))
    layout.addWidget(button('Save plot',panel.save_figure))
    if panel.mode=='dispersion':
        layout.addWidget(button('Load target dispersion',panel.load_target))
        layout.addWidget(button('SR / SR2 dispersion fit',lambda:panel.window.sr_fit(panel.data)))
        layout.addWidget(button('Delta-f Ramp',panel.window.frequency_dialog))
    left.addStretch()
    result=QWidget();right=QVBoxLayout(result);right.addWidget(panel.proposal_summary);right.addWidget(panel.plot,3);right.addWidget(panel.result_tabs,2)
    table_style(panel.table)
    split=QSplitter();area=scroll(controls);area.setMinimumWidth(290);split.addWidget(area);split.addWidget(result)
    split.setSizes([310,800]);split.setStretchFactor(1,1);panel.layout().addWidget(split)
    panel.refresh()
