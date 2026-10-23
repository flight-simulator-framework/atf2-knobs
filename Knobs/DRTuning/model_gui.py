"""Operator-visible model preparation, source settings and optics exports."""
import copy
import json
from pathlib import Path
import numpy as np
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QFileDialog,QLineEdit,QFormLayout,QCheckBox,QComboBox
from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.core import serial
from .presentation import button,group,scroll


class ModelPanel(QWidget):
    def __init__(self,window):
        super().__init__();self.window=window;self.result=None
        layout=QVBoxLayout(self)
        note=QLabel('Independent RFTrack model: public settings + tune targets. No real magnet writes when building.\n'
                    'Select RFTrack model response on a correction page to use it. CALC captures settings again.\n'
                    'Optics are model predictions. Tune matching alone does not identify all lattice errors.')
        note.setWordWrap(True);layout.addWidget(note)
        box,v=group('Model inputs (Machine interface)');form=QFormLayout();v.addLayout(form)
        from .model import preparation_config
        self.preparation=QComboBox()
        self.preparation.addItem('SAD COD reference (archived calibration)','sad_cod')
        self.preparation.addItem('All captured currents (no SAD preset)','all_currents')
        self.preparation.setCurrentIndex(self.preparation.findData(preparation_config(window.backend)['preparation']))
        self.preparation.setToolTip('SAD: QM trim except QM7; archived quadrupole factors and Ring BH1R gradient corrections; zero model correctors/skews. Machine currents unchanged. Used also as the reference for dispersion/skew, not exact legacy branch reproduction.')
        form.addRow('Model preparation',self.preparation)
        self.fields={}
        defaults={'energy_pv':'SAD:FILE:DR:ENERGY','tune_x':'SAD:DR_TUNE_X','tune_y':'SAD:DR_TUNE_Y',
                  'skew_sd':'SAD:SKEWCOR_SDCALIB','skew_sf':'SAD:SKEWCOR_SFCALIB'}
        for key,label in [('energy_pv','Energy setting PV [GeV]'),('tune_x','Horizontal tune target PV'),('tune_y','Vertical tune target PV'),
                          ('skew_sd','SD skew K1L/A PV'),('skew_sf','SF skew K1L/A PV')]:
            edit=QLineEdit(defaults[key]);form.addRow(label,edit);self.fields[key]=edit
            edit.editingFinished.connect(self.inputs_changed)
        self.match=QCheckBox('Match QF1R / QF2R in model only');self.match.setChecked(True);v.addWidget(self.match)
        self.match.setToolTip('SAD COD reference: if initial optics are unstable, search model QF1R/QF2R within +/-5% first. Final stability and tune checks still apply; no machine writes.')
        row=QHBoxLayout();v.addLayout(row)
        row.addWidget(button('Load lattice export',self.load_lattice));row.addWidget(button('Load model factors',self.load_factors))
        row.addWidget(button('Exclude unmapped BPMs / magnets',self.exclude_unmapped))
        layout.addWidget(scroll(box),1)
        row=QHBoxLayout();layout.addLayout(row)
        for label,action in [('Build model / optics',self.build),('Save model / optics',self.save),('Compare reference file',self.compare)]:row.addWidget(button(label,action))
        layout.addWidget(button('Save model diagnostics',self.save_diagnostics))
        self.status=QLabel('Model not built. Standard lattice: ATF DR 20111111b.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.plot=Plot();layout.addWidget(self.plot,2)
        self.preparation.currentIndexChanged.connect(self.inputs_changed)
    @property
    def manager(self):return self.window.service.model_manager
    def config(self):
        backend=self.window.backend;c=copy.deepcopy(getattr(backend,'model_config',{}))
        c.update(preparation=self.preparation.currentData(),energy_pv=self.fields['energy_pv'].text().strip(),tune_pvs=[self.fields[k].text().strip() for k in ('tune_x','tune_y')],
                 skew_calibration_pvs={f:self.fields[k].text().strip() for f,k in [('SD','skew_sd'),('SF','skew_sf')]})
        backend.model_config=c
    def inputs_changed(self):
        self.config();self.result=None;self.manager.model=None;self.manager.optics=None
        for panel in self.window.panels.values():panel.invalidate()
        self.status.setText('Model inputs changed; rebuild or CALC before use.')
    def load_lattice(self):
        path,_=QFileDialog.getOpenFileName(self,'Generated ATF lattice export','','JSON (*.json)')
        if path:
            self.config();self.window.backend.model_config['lattice_path']=path
            self.manager.model=None;self.manager.optics=None
            self.status.setText('Selected lattice: '+path+'; rebuild before use.');self.result=None
            for panel in self.window.panels.values():panel.invalidate()
    def load_factors(self):
        path,_=QFileDialog.getOpenFileName(self,'Model calibration: explicit multiplicative factors / bend K1L','','JSON (*.json)')
        if not path:return
        try:
            data=json.loads(Path(path).read_text())
            if data.get('schema')!='dr-model-calibration-1' or not data.get('source'):raise ValueError('Expected versioned dr-model-calibration-1 with source')
            self.config();self.window.backend.model_config.update(data);self.result=None
            self.manager.model=None;self.manager.optics=None
            for panel in self.window.panels.values():panel.invalidate()
            self.status.setText('Loaded model calibration: '+data['source'])
        except Exception as e:self.window.error(str(e))
    def exclude_unmapped(self):
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
        from .model import name
        self.config();ring=build_atf_dr_lattice(lattice_data_path=self.window.backend.model_config.get('lattice_path'))
        names={e.get_name() for e in ring.get_bpms()};missing=[n for n in self.window.backend.bpm_names if name(n) not in names]
        elements={e.get_name() for e in ring['*']};backend=self.window.backend
        magnets=[n for n in backend.hcorrectors+backend.vcorrectors if name(n) not in elements]
        magnets += [n for n in backend.skews if name(n)+'$SKEW' not in elements]
        for panel in self.window.panels.values():
            panel.options.excluded_bpms=tuple(sorted(set(panel.options.excluded_bpms)|set(missing)))
            panel.options.fixed_actuators=tuple(sorted(set(panel.options.fixed_actuators)|set(magnets)));panel.invalidate()
        self.status.setText('Excluded BPMs: '+(', '.join(missing) or 'none')+'; fixed unmapped magnets: '+(', '.join(magnets) or 'none'))
    def build(self):
        self.config();options=copy.deepcopy(self.window.panels['orbit'].options);options.model_match_tunes=self.match.isChecked()
        self.result=None
        def action(progress):
            from .model_optics import transport
            model=self.manager.prepare(options,self.window.service.check_cancel,progress)
            progress(50,'Calculating periodic optics and SR transport')
            table=transport(model);self.manager.optics=table
            return dict(schema='dr-model-export-1',settings=self.manager.settings,tune_match=model.match_report,optics=table)
        self.window.start(action,self.received,label='Build independent RFTrack model')
    def received(self,result):
        self.result=result;t=result['optics'];match=result['tune_match']
        text='Model tunes: '+', '.join(f'{q:.7f}' for q in t['tunes'])
        if match:text+='; QF model changes: '+', '.join(f'{v:+.3%}' for v in match['family_fractional_changes'])
        if match and match.get('initial_stable_point_search'):
            text+='; initial unstable model recovered by bounded QF search'
        self.status.setText(text+'\nModel prediction; machine settings unchanged.')
        self.plot.figure.clear();axes=self.plot.figure.subplots(2,1)
        rows=sorted(t['rows'].items(),key=lambda item:item[1]['s_m'])
        for i,ax in enumerate(axes):
            ax.plot([r['s_m'] for _,r in rows],[r['beta_m'][i] for _,r in rows]);ax.set(xlabel='s [m]',ylabel='Model beta '+('X' if i==0 else 'Y')+' [m]');ax.grid(alpha=.2)
        self.plot.canvas.draw_idle()
    def save(self):
        if self.result is None:self.window.error('Build model first');return
        path=QFileDialog.getExistingDirectory(self,'Save model inputs, tune fit and optics')
        if path:
            root=Path(path);(root/'model.json').write_text(json.dumps(serial(self.result),indent=2,allow_nan=False)+'\n')
            self.plot.save(root/'model_optics.png');self.window.log('Saved model audit: '+str(root))
    def save_diagnostics(self,path=None):
        attempt=self.manager.last_attempt
        if attempt is None:self.window.error('Run Build model / optics or model CALC first');return
        if not path:path,_=QFileDialog.getSaveFileName(self,'Save last model preparation attempt','dr_model_diagnostic.json','JSON (*.json)')
        if not path:return
        try:
            Path(path).write_text(json.dumps(serial(attempt),indent=2,allow_nan=False)+'\n')
            self.window.log('Saved model diagnostics: '+str(path))
        except Exception as exc:self.window.error(str(exc))
    def compare(self):
        if self.result is None:self.window.error('Build model first');return
        path,_=QFileDialog.getOpenFileName(self,'Independent SAD / reference optics table','','JSON (*.json)')
        if not path:return
        try:
            from .model_validation import compare_optics,compare_response
            reference=json.loads(Path(path).read_text())
            if reference.get('schema')=='dr-response-reference-1':
                data=self.window.panels[reference['mode']].data
                if data is None:raise ValueError('CALC the matching correction first')
                report=compare_response(data,reference)
            else:report=compare_optics(self.result,reference)
            self.result['reference_comparison']=report;self.window.log(json.dumps(serial(report),indent=2));self.window.log_toggle.setChecked(True)
        except Exception as e:self.window.error(str(e))
