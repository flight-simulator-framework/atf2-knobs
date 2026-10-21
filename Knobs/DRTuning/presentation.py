"""Resizable layouts shared by the DR tuning screens."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QGroupBox,
    QScrollArea,QSplitter,QPushButton,QLabel,QComboBox,QDoubleSpinBox,QHeaderView,
    QAbstractItemView)


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
    keys=[('corrector_probe_A','Probe ± [A]'),('step_A','Max correction [A]')]
    if panel.mode=='dispersion':
        keys += [('frequency_change_khz','RF full span [kHz]')]
        panel.compaction_label=QLabel();panel.compaction_label.setWordWrap(True)
        form.addRow('Momentum compaction',panel.compaction_label)
    if panel.mode=='skew':
        keys=[('skew_probe','Skew probe ± [A]'),('skew_step','Max correction [A]'),('change1_A','H probe 1 span [A]'),('change2_A','H probe 2 span [A]')]
        from PyQt6.QtWidgets import QCheckBox
        for key in ('sd','sf'):
            check=QCheckBox(key.upper());check.setChecked(getattr(panel.options,key))
            check.toggled.connect(lambda v,k=key:panel.set_option(k,v));form.addRow('Supply family',check)
        for key in ('probe1','probe2'):
            combo=QComboBox();combo.addItems(panel.window.backend.hcorrectors);combo.setCurrentText(getattr(panel.options,key))
            combo.currentTextChanged.connect(lambda v,k=key:panel.set_option(k,v));form.addRow(key,combo)
    for key,label in keys:
        spin=QDoubleSpinBox();spin.setDecimals(5);spin.setRange(.00001,1000.);spin.setValue(getattr(panel.options,key))
        spin.valueChanged.connect(lambda v,k=key:panel.set_option(k,v));form.addRow(label,spin)
        panel.modern_fields[key]=spin
    layout.addWidget(button('Select BPMs / magnets',panel.window.selection))
    panel.selection_label=QLabel();panel.selection_label.setWordWrap(True);layout.addWidget(panel.selection_label)
    layout.addWidget(button('SVD / advanced settings',panel.advanced))
    box,layout=group('2. Measure and correct');left.addWidget(box)
    note=QLabel('Response acquisition moves magnets / RF.\nApply the proposed correction separately.');note.setWordWrap(True);layout.addWidget(note)
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
    result=QWidget();right=QVBoxLayout(result);right.addWidget(panel.plot,3);right.addWidget(panel.table,1)
    table_style(panel.table)
    split=QSplitter();area=scroll(controls);area.setMinimumWidth(290);split.addWidget(area);split.addWidget(result)
    split.setSizes([310,800]);split.setStretchFactor(1,1);panel.layout().addWidget(split)
    panel.refresh()
