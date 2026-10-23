"""PyQt6 ports of the op-base DR correction panels with embedded plots."""
from dataclasses import asdict
import copy
import json
from pathlib import Path
import traceback
import numpy as np
from PyQt6 import uic
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,
    QFormLayout,QTabWidget,QPushButton,QLabel,QPlainTextEdit,QTableWidget,QTableWidgetItem,
    QFileDialog,QMessageBox,QInputDialog,QDialog,QDialogButtonBox,QListWidget,QListWidgetItem,
    QDoubleSpinBox,QSpinBox,QComboBox,QCheckBox,QSplitter,QProgressBar,QScrollArea,QAbstractItemView,QHeaderView)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg,NavigationToolbar2QT
from matplotlib.figure import Figure
from .core import Options,CorrectionService,Cancelled,serial,save_session,load_session,export_curves
from .backends import HERE,DemoBackend,profile_issues
from .tbt import analyze_tbt
from .assessment import changed_indices,assess


class Task(QThread):
    result=pyqtSignal(object)
    failure=pyqtSignal(str)
    progress=pyqtSignal(int,str)
    def __init__(self,action):
        super().__init__();self.action=action
    def run(self):
        try:self.result.emit(self.action(self.progress.emit))
        except Exception:self.failure.emit(traceback.format_exc())


class Plot(QWidget):
    def __init__(self):
        super().__init__();layout=QVBoxLayout(self)
        self.figure=Figure(figsize=(8,5),layout='constrained')
        self.canvas=FigureCanvasQTAgg(self.figure)
        layout.addWidget(NavigationToolbar2QT(self.canvas,self));layout.addWidget(self.canvas)
    def correction(self,data):
        full='full_measured' in data
        prefix='full_' if full else ''
        labels=data['full_signal_labels'] if full else data['signal_labels']
        self.figure.clear();count=len(labels)
        self.figure.set_layout_engine(None)
        # Keep the original X-above-Y convention, adding the joint COD fit
        # beside the dispersion panels instead of squeezing four rows.
        axes=self.figure.subplots(2,2).T.ravel() if count==4 else self.figure.subplots(count,1,squeeze=False).ravel()
        s=np.asarray(data['positions_m'],float)
        known=np.isfinite(s).all()
        if not known:s=np.arange(1,len(data['names'])+1)
        order=np.argsort(s)
        valid=np.asarray(data['valid'],bool)
        plane=data.get('options',{}).get('plane','y')
        legends={}
        for row,(ax,label) in enumerate(zip(axes,labels)):
            y=np.asarray(data[prefix+'measured'])[row];ax.scatter(s[valid],y[valid],s=14,c='black',label='Measured before SET')
            # Excluded/invalid BPMs are not constrained by the fit. Never let
            # their arbitrary response predictions set the orbit plot scale.
            # NaNs also break lines across excluded BPMs instead of bridging.
            for key,color,style,title in [('predicted','C2','--','Predicted after SET'),('after','C3','-','Measured after SET')]:
                if prefix+key in data:
                    mask=valid.copy()
                    if key=='after':mask &= np.asarray(data.get('after_valid',mask),bool)
                    values=np.where(mask,np.asarray(data[prefix+key])[row],np.nan)
                    ax.plot(s[order],values[order],style,color=color,label=title)
            target_row=row
            fitted_plane=True
            if full and data['mode'] in ('orbit','dispersion'):
                fitted_plane=(row%2==int(plane=='y'));target_row=row//2
            if 'target' in data and fitted_plane:
                target=np.where(valid,np.asarray(data['target'])[target_row],np.nan)
                ax.plot(s[order],target[order],':',color='gray',label='Target (fit plane)')
            unit='mm / (dp/p)' if label.startswith('Dispersion') else 'mm'
            ylabel={'Dispersion X':'ηx [mm]','Dispersion Y':'ηy [mm]'}.get(label,f'{label} [{unit}]')
            ax.set(xlabel='s [m]' if known else 'BPM index (measurement order)',ylabel=ylabel)
            ax.set_title(label+(' — fitted plane' if fitted_plane else ' — observation only'),fontsize=9)
            ax.ticklabel_format(axis='y',style='plain',useOffset=False)
            ax.grid(alpha=.2);ax.tick_params(labelsize=8);ax.xaxis.label.set_size(9);ax.yaxis.label.set_size(9)
            handles,titles=ax.get_legend_handles_labels();legends.update(zip(titles,handles))
        self.figure.legend(list(legends.values()),list(legends),loc='upper center',bbox_to_anchor=(.55,.93),ncol=2,fontsize=8)
        self.figure.subplots_adjust(left=.12,right=.98,bottom=.12,top=.77,hspace=.65,wspace=.45)
        state='SET applied; remeasured' if data.get('applied') else 'CALC proposal only; SET not applied'
        self.figure.suptitle(f"{data['mode'].upper()} — {state}\n"
            f"{int(valid.sum())}/{len(valid)} BPMs shown; invalid/excluded BPMs hidden",fontsize=10,y=.99)
        self.canvas.draw_idle()
    def save(self,path):self.figure.savefig(path,dpi=160)


# Original QE variables are retained in the source inventory/tooltips. They are
# now explicit local calculation parameters, not silently written SAD PVs.
PV_FIELDS={
 'SAD:DR:BEAM_ENERGY':'energy_gev','SAD:DR_TUNE_X':'tune_x','SAD:DR_TUNE_Y':'tune_y',
 'SAD:COD:DR:N_STH':'count_h','SAD:COD:DR:N_STV':'count_v',
 'SAD:DISPERSION:DR:ZH_N':'count_h','SAD:DISPERSION:DR:ZV_N':'count_v',
 'SAD:DISPERSION:FREQ_CHANGE':'frequency_change_khz','SAD:DISPERSION:ALPHA':'momentum_compaction',
 'SAD:SKEWCOR_STEERNAME1':'probe1','SAD:SKEWCOR_STEERNAME2':'probe2',
 'SAD:SKEWCOR_STEERCHANGE1':'change1_A','SAD:SKEWCOR_STEERCHANGE2':'change2_A',
 'SAD:SKEWCOR_SDCALIB':'sd_calibration','SAD:SKEWCOR_SFCALIB':'sf_calibration'}


class CorrectionPanel(QWidget):
    def __init__(self,window,mode):
        super().__init__();self.window=window;self.mode=mode;self.data=None
        backend=window.backend
        self.options=Options(probe1=backend.hcorrectors[0],probe2=backend.hcorrectors[len(backend.hcorrectors)//2])
        if backend.identity.startswith('epics:'):
            self.options.bpm_samples=3;self.options.bpm_interval_s=1.
        if backend.unit('skews')=='A':self.options.skew_probe=.05;self.options.skew_step=.1
        if getattr(backend,'ramp',None):self.options.frequency_change_khz=2.
        self.form=uic.loadUi(str(HERE/'forms'/f'{mode}.ui'))
        self.form.setFixedSize(self.form.size())
        self.form.setStyleSheet('QLabel {font-size:10pt;} QPushButton {font-size:9pt;}')
        self.inventory=json.loads((HERE/'forms/legacy_inventory.json').read_text())[mode]['widgets']
        left=QWidget();left_layout=QVBoxLayout(left);left_layout.addWidget(self.form)
        self.detail=QLabel('');self.detail.setWordWrap(True);left_layout.addWidget(self.detail)
        advanced=QPushButton('Response / limits / SVD settings');advanced.clicked.connect(self.advanced);left_layout.addWidget(advanced)
        select=QPushButton('BPM selection / FIX DR STEER');select.clicked.connect(window.selection);left_layout.addWidget(select)
        left_layout.addStretch()
        self.plot=Plot();self.table=QTableWidget(0,6)
        self.table.setHorizontalHeaderLabels(['Magnet','Before','Delta','Proposed','Unit','SET action'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.proposal_summary=QLabel();self.proposal_summary.setWordWrap(True)
        self.proposal_summary.setStyleSheet('font-weight: bold; padding: 6px; background: #e8edf3; color: #172b4d;')
        self.assessment_summary=QLabel('SET has not run.');self.assessment_summary.setWordWrap(True)
        self.assessment_table=QTableWidget(0,8)
        self.assessment_table.setHorizontalHeaderLabels(['Signal\n(unit)','BPMs','Before\nRMS','Predicted\nRMS','After\nRMS','Improve\n(%)','Mismatch\nRMS','Max diff\nBPM'])
        self.assessment_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.assessment_table.verticalHeader().setDefaultSectionSize(52)
        self.assessment_table.verticalHeader().hide()
        self.assessment_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        verification=QWidget();v=QVBoxLayout(verification);v.addWidget(self.assessment_summary);v.addWidget(self.assessment_table)
        self.result_tabs=QTabWidget();self.result_tabs.addTab(self.table,'Magnet settings');self.result_tabs.addTab(verification,'SET verification')
        right=QWidget();right_layout=QVBoxLayout(right);right_layout.addWidget(self.proposal_summary);right_layout.addWidget(self.plot,3);right_layout.addWidget(self.result_tabs,1)
        row=QHBoxLayout()
        for text,action in [('Save data + figures',self.save_bundle),('Save figure…',self.save_figure),('Load nominal dispersion…',self.load_target)]:
            b=QPushButton(text);b.clicked.connect(action);row.addWidget(b)
        right_layout.addLayout(row)
        split=QSplitter();split.addWidget(left);split.addWidget(right);split.setStretchFactor(1,1)
        layout=QVBoxLayout(self);layout.addWidget(split)
        self.bind();self.refresh()

    def invalidate(self):
        self.data=None;self.table.setRowCount(0);self.assessment_table.setRowCount(0)
        self.assessment_summary.setText('No current proposal. Run CALC.');self.refresh()

    def bind(self):
        for entry in self.inventory:
            name=entry['name'];widget=getattr(self.form,name,None);pv=entry['pv']
            if pv and widget:widget.setToolTip('Original op-base PV: '+pv)
            if pv in PV_FIELDS:
                field=PV_FIELDS[pv]
                button=getattr(self.form,name.replace('qelabel','pushButton',1),None)
                if button:
                    button.setText('…');button.clicked.connect(lambda _,f=field:self.edit(f))
            if entry['qt_class']=='QEPushButton':
                widget.setCheckable(True)
                if 'SWSD' in name:widget.clicked.connect(lambda value:self.set_option('sd',value))
                elif 'SWSF' in name:widget.clicked.connect(lambda value:self.set_option('sf',value))
                elif name.endswith(('SWH','ZHSW')):widget.clicked.connect(lambda _:self.set_option('plane','x'))
                elif name.endswith(('SWV','ZVSW')):widget.clicked.connect(lambda _:self.set_option('plane','y'))
            if name=='qeanalogprogressbar':
                widget.setDecimals(6);widget.setRange(.000001,1.);widget.setSingleStep(.00001)
                widget.setValue(self.options.tolerance_m)
                widget.valueChanged.connect(lambda v:self.set_option('tolerance_m',v))
        prefix='pushButtonDispersion' if self.mode=='dispersion' else 'pushButton'
        getattr(self.form,prefix+'Calc').clicked.connect(lambda:self.calculate())
        getattr(self.form,prefix+'CalcFile').clicked.connect(self.calculate_file)
        getattr(self.form,prefix+'Set').clicked.connect(self.apply)
        getattr(self.form,prefix+'Reset').clicked.connect(self.reset)
        for name in ('pushButton_DataToSad','pushButtonOpenMenu'):
            if hasattr(self.form,name):getattr(self.form,name).clicked.connect(self.window.buffers)
        if hasattr(self.form,'pushButton_DRSTEER'):self.form.pushButton_DRSTEER.clicked.connect(self.window.selection)
        self.form.pushButtonHelp.clicked.connect(lambda:QMessageBox.information(self,'op-base operation help',(HERE/'forms'/f'{self.mode}_help.txt').read_text()+'\n\nPython: CALC restores all probe settings. SET applies the displayed proposal. RESET restores the pre-SET snapshot. Figures and numerical curves are saved together.'))
        if self.mode=='dispersion':
            self.form.pushButtonDeltaF.clicked.connect(self.window.frequency_dialog)
            self.form.pushButtonSRCalc.clicked.connect(lambda:self.window.sr_fit(self.data))
            self.form.pushButtonSRCalcFile.clicked.connect(lambda:self.window.sr_fit(None))

    def set_option(self,name,value):
        setattr(self.options,name,value);self.invalidate()

    def edit(self,name):
        current=getattr(self.options,name)
        if name.startswith('probe'):
            value,ok=QInputDialog.getItem(self,'Horizontal steering',name,self.window.backend.hcorrectors,0,False)
        elif isinstance(current,int):value,ok=QInputDialog.getInt(self,'Calculation parameter',name,current,1,500)
        else:value,ok=QInputDialog.getDouble(self,'Calculation parameter',name,current,-1e6,1e6,7)
        if ok:self.set_option(name,value)

    def refresh(self):
        if hasattr(self,'compaction_label'):
            self.compaction_label.setText(f'{self.options.momentum_compaction:g} (fixed for these optics)')
        if hasattr(self,'selection_label'):
            b=self.window.backend;o=self.options
            if self.mode=='skew':names=[n for n in b.skews if (o.sd and n.startswith('SD')) or (o.sf and n.startswith('SF'))]
            else:names=b.hcorrectors if o.plane=='x' else b.vcorrectors
            count=sum(n not in o.fixed_actuators for n in names)
            bpms=sum(n not in o.excluded_bpms for n in b.bpm_names)
            self.selection_label.setText((f'RFTrack will calculate {count} magnet responses; {bpms} BPMs enabled.\nMachine: '+('one RF scan pair.' if self.mode=='dispersion' else 'two horizontal probe scans.' if self.mode=='skew' else 'orbit read only.')) if o.response_source=='rftrack' else f'CALC will scan {count} magnets; {bpms} BPMs enabled for fitting.'+
                ((f' RF scan pairs: up to {1+2*count*int(o.adaptive_max_pairs)}.' if o.adaptive_probe else f' RF scan pairs: {1+2*count}.') if self.mode=='dispersion' and count else '')+
                (f' Up to {o.adaptive_max_pairs} probe pairs per magnet.' if o.adaptive_probe else '')+
                '\nMaximum correctors limits SET selection, not the CALC scan count.')
        for key,widget in getattr(self,'modern_fields',{}).items():
            widget.blockSignals(True)
            if isinstance(widget,QCheckBox):widget.setChecked(getattr(self.options,key))
            else:widget.setValue(getattr(self.options,key)*(widget.property('option_scale') or 1))
            widget.blockSignals(False)
        if hasattr(self,'refresh_adaptive_controls'):self.refresh_adaptive_controls()
        for entry in self.inventory:
            widget=getattr(self.form,entry['name'],None);pv=entry['pv']
            if not widget:continue
            if pv in PV_FIELDS:
                value=getattr(self.options,PV_FIELDS[pv]);text=f'{value:g}' if isinstance(value,(int,float)) else str(value)
                button=getattr(self.form,entry['name'].replace('qelabel','pushButton',1),None)
                if button:button.setText(text);button.setToolTip(text+'\nOriginal op-base PV: '+pv);widget.hide()
                else:widget.setText(text)
            elif pv=='SAD:DR_TOLERANCE' and isinstance(widget,QLabel):widget.setText(f'{self.options.tolerance_m:g}')
            elif entry['qt_class']=='QEPushButton':
                name=entry['name']
                state=self.options.sd if 'SWSD' in name else self.options.sf if 'SWSF' in name else self.options.plane==('x' if name.endswith(('SWH','ZHSW')) else 'y')
                widget.setChecked(state);widget.setText('ON' if state else 'OFF')
            elif pv and ('MESSAGE' in pv):widget.setText('Ready' if self.data is None else 'Calculation ready')
        prefix='pushButtonDispersion' if self.mode=='dispersion' else 'pushButton'
        set_button=getattr(self.form,prefix+'Set')
        count=len(changed_indices(self.data)) if self.data is not None else 0
        set_button.setEnabled(bool(count) and not self.data.get('applied',False))
        set_button.setText(f'Apply {count} magnets (SET)' if self.data is not None else 'Apply correction (SET)')
        if self.data is None:self.proposal_summary.setText('No current proposal. CALC will show how many magnets SET changes.')
        else:
            data=self.data;o=data['options'];plane='H' if o['plane']=='x' else 'V'
            maximum=o['count_h'] if plane=='H' else o['count_v']
            scope='skew supplies' if self.mode=='skew' else plane+' correctors'
            bound='' if self.mode=='skew' else f'; maximum {maximum}'
            verb='SET changed' if data.get('applied') else 'SET will change'
            self.proposal_summary.setText(f'{verb} {count} {scope}{bound}; {len(data["actuators"])} response candidates.\n'
                'Only nonzero setting changes are written. Details: Magnet settings tab.')
            probes=[p for p in data.get('probe_history',[]) if p['used_for_response']]
            if probes:
                reached=sum(p['target_reached'] for p in probes)
                self.proposal_summary.setText(self.proposal_summary.text()+
                    f'\nAdaptive probes: {reached}/{len(probes)} within target; '
                    f'{len(probes)-reached} outside target (limits / max pairs). See detailed log and saved probe_history.')
        getattr(self.form,prefix+'Reset').setEnabled(bool(self.window.service.undo))
        self.detail.setText('CALC restores diagnostic probes. SET is separate.\nCorrector: '+self.window.backend.unit('correctors')+'; skew: '+self.window.backend.unit('skews')+ ('\nRFTrack settings/tune source and fit are saved with the proposal.' if self.options.response_source=='rftrack' else '\nEnergy / tune / SK1/I fields are reference metadata for measured-response mode.'))

    def advanced(self):
        dialog=QDialog(self);dialog.setWindowTitle('Response acquisition and correction limits')
        form=QFormLayout(dialog);fields={}
        for key,label in [('count_h','Maximum H correctors'),('count_v','Maximum V correctors'),('bpm_samples','BPM samples per setting')]:
            spin=QSpinBox();spin.setRange(1,500);spin.setValue(getattr(self.options,key));form.addRow(label,spin);fields[key]=spin
        for key in ('corrector_probe_A','skew_probe','step_A','skew_step','svd_cut','dispersion_weight','orbit_weight','bpm_interval_s','model_extraction_weight'):
            spin=QDoubleSpinBox();spin.setDecimals(7);spin.setRange(0.,1000.);spin.setValue(getattr(self.options,key));form.addRow(key,spin);fields[key]=spin
        if self.mode=='dispersion':
            spin=QDoubleSpinBox();spin.setDecimals(7);spin.setRange(.0000001,1.)
            spin.setValue(self.options.momentum_compaction)
            form.addRow('Momentum compaction (optics setting)',spin);fields['momentum_compaction']=spin
        tolerance=QDoubleSpinBox();tolerance.setDecimals(7);tolerance.setRange(.0000001,1.);tolerance.setValue(self.options.tolerance_m)
        form.addRow('COD tolerance [m]',tolerance);fields['tolerance_m']=tolerance
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        if dialog.exec():
            for key,w in fields.items():setattr(self.options,key,w.value())
            self.invalidate()

    def calculate(self,dataset=None):
        options=copy.deepcopy(self.options)
        self.invalidate()
        self.window.start(lambda progress:self.window.service.calculate(self.mode,options,progress,dataset),self.display)

    def calculate_file(self):
        path,_=QFileDialog.getOpenFileName(self,'CALC (FILE): measured response session','','JSON (*.json)')
        if path:
            try:self.calculate(load_session(path))
            except Exception as e:self.window.error(str(e))

    def display(self,data):
        self.data=data;self.plot.correction(data)
        if not data.get('applied'):
            for probe in data.get('probe_history',[]):
                if probe['used_for_response']:
                    self.window.log(f'Probe {probe["magnet"]}: {probe["pair"]} pairs; '
                        f'±{probe["amplitude"]:.6g} {probe["unit"]}; response {probe["observed_mm"]:.6g} / '
                        f'target {probe["target_mm"]:g} mm; {probe["status"]}')
        changed=set(changed_indices(data))
        self.table.setRowCount(len(data['actuators']))
        for i,name in enumerate(data['actuators']):
            values=[name,f"{data['centers'][i]:.7g}",f"{data['delta'][i]:+.7g}",f"{data['centers'][i]+data['delta'][i]:.7g}",data['actuator_unit'],('Changed' if data.get('applied') else 'Will change') if i in changed else 'No write']
            for j,value in enumerate(values):self.table.setItem(i,j,QTableWidgetItem(value))
        self.show_assessment(data)
        self.table.resizeColumnsToContents();self.refresh()
        self.window.log(f'{self.mode}: rank {data["rank"]}, {len(changed)} magnets will change; SET has not run' if not data.get('applied') else f'{self.mode}: {len(changed)} magnets changed; SET and remeasurement completed')

    def show_assessment(self,data):
        self.assessment_table.setRowCount(0)
        if not data.get('applied'):
            self.assessment_summary.setText('SET will remeasure and compare against the prediction.');self.result_tabs.setCurrentIndex(0);return
        report=data.get('assessment') or assess(data);data['assessment']=report
        self.assessment_table.setRowCount(len(report['rows']));messages=[]
        fmt=lambda x:'—' if x is None else f'{x:.5g}'
        for i,r in enumerate(report['rows']):
            values=[r['signal']+'\n['+r['unit'].replace(' ','')+']',f'{r["valid_bpms"]}/{r["enabled_bpms"]}',
                    *[fmt(r[k]) for k in ('before_rms','predicted_rms','after_rms','improvement_pct','mismatch_rms')],
                    fmt(r['mismatch_max'])+('\n'+r['worst_bpm'] if r['worst_bpm'] else '')]
            for j,value in enumerate(values):
                item=QTableWidgetItem(value);item.setToolTip(report['note']);self.assessment_table.setItem(i,j,item)
            message=f'{r["signal"]}: {r["status"]}; improvement {fmt(r["improvement_pct"])}%; prediction mismatch RMS {fmt(r["mismatch_rms"])} {r["unit"]}'
            if r['excluded_from_comparison']:message+=f'; {r["excluded_from_comparison"]} BPMs unavailable after SET'
            messages.append(message);self.window.log(message)
        self.assessment_summary.setText('\n'.join(messages)+'\nRMS is relative to target. Comparison uses common valid BPMs; changes include measurement noise and drift.')
        self.result_tabs.setCurrentIndex(1)

    def apply(self):
        if self.data:self.window.start(lambda _:self.window.service.apply(self.data),self.display)

    def reset(self):
        def finished(_):
            for panel in self.window.panels.values():panel.invalidate()
            self.window.log('RESET: pre-SET currents and RF restored')
        self.window.start(lambda _:self.window.service.reset(),finished)

    def save_bundle(self,path=None):
        if not self.data:self.window.error('CALC first');return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save data + graphs')
        if path:
            root=Path(path);root.mkdir(parents=True,exist_ok=True)
            save_session(root/f'{self.mode}.json',self.data)
            export_curves(root/f'{self.mode}.csv',self.data)
            for ext in ('png','pdf'):self.plot.save(root/f'{self.mode}.{ext}')
            self.window.log('Saved '+str(root))

    def save_figure(self):
        path,_=QFileDialog.getSaveFileName(self,'Save graph',self.mode+'.png','PNG (*.png);;PDF (*.pdf);;SVG (*.svg)')
        if path:self.plot.save(path)

    def load_target(self):
        path,_=QFileDialog.getOpenFileName(self,'Public nominal dispersion: names, x, y in mm','','JSON (*.json)')
        if path:
            try:
                data=json.loads(Path(path).read_text());backend=self.window.backend
                index=[data['names'].index(n) for n in backend.bpm_names]
                target={axis:np.asarray(data[axis],float)[index] for axis in ('x','y')}
                if not all(np.isfinite(v).all() for v in target.values()):raise ValueError('Nonfinite target')
                backend._target=target;backend.has_horizontal_dispersion_target=True
                backend.target_dispersion=lambda plane:target[plane].copy()
                self.invalidate();self.window.log('Loaded public target '+path)
            except Exception as e:self.window.error(str(e))


class TBTPanel(QWidget):
    def __init__(self,window):
        super().__init__();self.window=window;self.wave=None;self.estimate=None
        layout=QVBoxLayout(self);controls=QHBoxLayout();layout.addLayout(controls)
        self.turns=QSpinBox();self.turns.setRange(16,1024);self.turns.setValue(1024)
        self.start_turn=QSpinBox();self.start_turn.setRange(1,1008);self.start_turn.setValue(3)
        self.bpm=QComboBox();self.bpm.addItems(window.backend.bpm_names)
        self.axis=QComboBox();self.axis.addItems(['BPM_X','BPM_Y','BPM_INTENSITY'])
        for label,w in [('Turns',self.turns),('Fit start turn',self.start_turn),('DR bpm No.',self.bpm),('Signal',self.axis)]:controls.addWidget(QLabel(label));controls.addWidget(w)
        actions=QHBoxLayout();layout.addLayout(actions)
        for label,action in [('Update',self.acquire),('Load TBT file',self.load),('CALC AvE. Std.',self.statistics),('Beta at BPM CALC',self.analyze),('show DR beta plot',self.draw),('Save BPMdata/Fig',self.save)]:
            b=QPushButton(label);b.clicked.connect(action);actions.addWidget(b)
        legacy=QPushButton('Import legacy X / Y / intensity text');legacy.clicked.connect(self.load_text);layout.addWidget(legacy)
        self.reference=QCheckBox('Absolute beta from independent reference')
        self.reference_planes=QComboBox();self.reference_planes.addItems(['X and Y','X only','Y only'])
        self.beta_x=QDoubleSpinBox();self.beta_y=QDoubleSpinBox()
        for w in (self.beta_x,self.beta_y):w.setRange(.001,1000.);w.setValue(8.)
        row=QHBoxLayout();row.addWidget(self.reference);row.addWidget(self.reference_planes);row.addWidget(QLabel('Selected BPM beta X / Y [m]'));row.addWidget(self.beta_x);row.addWidget(self.beta_y);layout.addLayout(row)
        self.note=QLabel('Sine-fit tune/phase and relative beta. Absolute beta requires an independent reference; no simulator Twiss is read.');self.note.setWordWrap(True);layout.addWidget(self.note)
        excitation=QHBoxLayout();self.exciter=QComboBox();self.exciter.addItems(['External / no pulse']+window.backend.hcorrectors+window.backend.vcorrectors)
        if not window.backend.label.startswith('EPICS'):self.exciter.setCurrentIndex(1)
        self.pulse_A=QDoubleSpinBox();self.pulse_A.setRange(-5.,5.);self.pulse_A.setDecimals(3);self.pulse_A.setValue(.1)
        excitation.addWidget(QLabel('Simulation one-turn excitation (live uses acquired waveform)'));excitation.addWidget(self.exciter);excitation.addWidget(QLabel('Delta [A]'));excitation.addWidget(self.pulse_A);layout.addLayout(excitation)
        if window.backend.label.startswith('EPICS'):self.exciter.setEnabled(False);self.pulse_A.setEnabled(False)
        self.plot=Plot();layout.addWidget(self.plot)
        self.bpm.currentIndexChanged.connect(self.draw);self.axis.currentIndexChanged.connect(self.draw)
    def acquire(self):
        backend=self.window.backend
        pulse=None if self.exciter.currentIndex()==0 else dict(corrector=self.exciter.currentText(),delta_A=self.pulse_A.value(),turn=1)
        turns=self.turns.value()
        self.window.start(lambda _:backend.tbt(turns,pulse),self.received)
    def received(self,wave):
        self.wave=None;self.estimate=None;self.bpm.clear();self.bpm.addItems(wave['names']);self.wave=wave;self.draw()
    def load(self):
        path,_=QFileDialog.getOpenFileName(self,'TBT JSON session / measured waveform','','JSON (*.json)')
        if path:
            try:
                data=json.loads(Path(path).read_text());wave=data.get('waveform',data.get('measurement',data))
                from .tbt import waveform_arrays
                waveform_arrays(wave);self.bpm.clear();self.bpm.addItems(wave['names']);self.received(wave)
            except Exception as e:self.window.error(str(e))
    def analyze(self):
        if self.wave is None:self.window.error('Acquire or load a waveform first');return
        reference=None
        planes=(('x','y'),('x',),('y',))[self.reference_planes.currentIndex()]
        if self.reference.isChecked():reference={axis:dict(bpm=self.bpm.currentText(),beta_m=w.value()) for axis,w in [('x',self.beta_x),('y',self.beta_y)] if axis in planes}
        try:
            self.estimate=analyze_tbt(self.wave,start_turn=self.start_turn.value(),reference=reference)
            states=[]
            for axis,result in self.estimate['planes'].items():
                states.append(f'{axis.upper()}: '+(f"tune {result['tune']:.6f}, {sum(result['valid_bpms'])} valid BPMs" if result['valid'] else 'INVALID — '+result.get('reason','oscillation below signal/noise threshold')))
            self.note.setText('; '.join(states)+'\nSine fit; real-waveform tune alias in [0.02, 0.48]. Absolute beta uses only supplied references.');self.draw()
        except Exception as e:self.window.error(str(e))
    def load_text(self):
        from .dataio import load_tbt_text
        paths={}
        for axis in ('x','y','tmit'):
            path,_=QFileDialog.getOpenFileName(self,'Legacy '+axis+' matrix: [turn, BPM]','','Text files (*.txt *.dat);;All files (*)')
            if not path:return
            paths[axis]=path
        scale,ok=QInputDialog.getDouble(self,'Position calibration','mm per input unit',1.,1e-9,1e6,9)
        if not ok:return
        noise,ok=QInputDialog.getDouble(self,'Noise calibration','BPM noise RMS [mm]',.005,1e-9,100.,9)
        if not ok:return
        try:
            wave=load_tbt_text(paths,self.window.backend.bpm_names,mm_per_unit=scale,noise_mm=noise)
            self.bpm.clear();self.bpm.addItems(wave['names']);self.received(wave)
        except Exception as exc:self.window.error(str(exc))
    def statistics(self):
        if self.wave is None:return
        j=self.bpm.currentIndex()
        text='; '.join(f'{axis}: mean={np.nanmean(np.asarray(self.wave[axis],float)[:,j]):.6g}, std={np.nanstd(np.asarray(self.wave[axis],float)[:,j]):.6g}' for axis in ('x','y','tmit'))
        self.window.log(text)
    def draw(self,*_):
        if self.wave is None or self.bpm.currentIndex()<0:return
        figure=self.plot.figure;figure.clear();axes=figure.subplots(2,2)
        j=self.bpm.currentIndex();axis=['x','y','tmit'][self.axis.currentIndex()]
        values=np.asarray(self.wave[axis],float)[:,j];turns=self.wave['turns']
        axes[0,0].plot(turns,values);axes[0,0].set(title=self.bpm.currentText(),xlabel='Turn',ylabel='electrons / intensity' if axis=='tmit' else axis+' [mm]')
        centered=np.nan_to_num(values-np.nanmean(values));spectrum=abs(np.fft.rfft(centered*np.hanning(len(values))))
        axes[0,1].plot(np.fft.rfftfreq(len(values)),spectrum);axes[0,1].set(xlabel='Fractional tune',ylabel='FFT amplitude')
        if self.estimate:
            nominal=dict(zip(self.window.backend.bpm_names,self.window.backend.positions_m))
            tbt_profile=getattr(self.window.backend,'profile',{}).get('tbt',{})
            nominal.update(zip(tbt_profile.get('names') or [],tbt_profile.get('positions_m') or []))
            known=all(n in nominal and nominal[n] is not None and np.isfinite(nominal[n]) for n in self.wave['names'])
            positions=[nominal[n] for n in self.wave['names']] if known else list(range(len(self.wave['names'])))
            absolute=False
            for plane,color in [('x','black'),('y','red')]:
                result=self.estimate['planes'][plane]
                if not result['valid']:continue
                beta=result['beta_m'] if result['beta_m'] is not None else result['relative_beta']
                absolute |= result['beta_m'] is not None
                axes[1,0].scatter(positions,beta,s=12,color=color,label='TBT-beta'+plane.upper()+(' [m]' if result['beta_m'] is not None else ' relative'))
                axes[1,1].scatter(positions,result['phase_rad'],s=12,color=color,label=plane+f' tune {result["tune"]:.6f}')
            units=[r.get('beta_m') is not None for r in self.estimate['planes'].values() if r['valid']]
            mixed=bool(units) and any(units) and not all(units)
            axes[1,0].set(xlabel='s [m], nominal geometry' if known else 'BPM index',ylabel='Beta (units in legend)' if mixed else ('beta [m]' if absolute else 'Relative beta [1]'))
            axes[1,1].set(xlabel='s [m], nominal geometry' if known else 'BPM index',ylabel='Phase [rad]')
            for ax in axes[1]:
                if ax.has_data():ax.legend()
        for ax in axes.ravel():ax.grid(alpha=.2)
        self.plot.canvas.draw_idle()
    def save(self,path=None):
        if self.wave is None:return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save TBT data and graphs')
        if path:
            root=Path(path);root.mkdir(parents=True,exist_ok=True)
            save_session(root/'tbt.json',dict(schema='dr-tbt-1',waveform=self.wave,estimate=self.estimate))
            np.savez_compressed(root/'tbt.npz',**{k:np.asarray(self.wave[k]) for k in ('x','y','tmit','turns','valid')},names=self.wave['names'])
            for ext in ('png','pdf'):self.plot.save(root/f'tbt.{ext}')
            self.window.log('Saved TBT '+str(root))


class DRCorrectionWindow(QMainWindow):
    def __init__(self,backend=None):
        super().__init__();self.backend=backend or DemoBackend();self.service=CorrectionService(self.backend);self.task=None;self.orbit_buffers={}
        from .input_guard import protect_settings_from_wheel
        protect_settings_from_wheel(self)
        self.setWindowTitle('DR Corrections — Python op-base');self.resize(1320,880)
        body=QWidget();self.setCentralWidget(body);layout=QVBoxLayout(body)
        self.banner=QLabel(self.backend.label);self.banner.setStyleSheet('font-weight:bold;padding:8px;background:#dbeafe;color:#12243b;');layout.addWidget(self.banner)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1);self.panels={}
        for mode,title in [('orbit','DR COD Correction'),('dispersion','DR Dispersion Correction'),('skew','DR Skew Correction')]:
            panel=CorrectionPanel(self,mode);self.panels[mode]=panel;self.tabs.addTab(panel,title)
        self.tbt=TBTPanel(self);self.tabs.addTab(self.tbt,'Turn1024 / TBT beta')
        row=QHBoxLayout();self.progress=QProgressBar();row.addWidget(self.progress)
        self.cancel_button=QPushButton('Cancel / restore probes');self.cancel_button.clicked.connect(self.service.cancel.set);row.addWidget(self.cancel_button)
        profile=QPushButton('Hardware PV checklist');profile.clicked.connect(self.hardware_checklist);row.addWidget(profile)
        layout.addLayout(row)
        self.messages=QPlainTextEdit();self.messages.setReadOnly(True);self.messages.setMaximumHeight(105);layout.addWidget(self.messages)
        self.log('No real-machine connection is made unless the EPICS backend is explicitly configured.')
    def log(self,text):self.messages.appendPlainText(str(text))
    def error(self,text):self.log('ERROR: '+text)
    def start(self,action,finished):
        if self.task is not None and self.task.isRunning():self.error('A task is already running');return
        self.service.cancel.clear();self.tabs.setEnabled(False);self.progress.setValue(0)
        self.task=Task(action);self.task.result.connect(finished);self.task.failure.connect(self.error)
        self.task.progress.connect(lambda n,text:(self.progress.setValue(n),self.log(text)))
        self.task.finished.connect(self.task_finished);self.task.start()
    def task_finished(self):
        self.tabs.setEnabled(True)
        for panel in self.panels.values():panel.refresh()
    def selection(self):
        from .selection import SelectionDialog
        dialog=SelectionDialog(self)
        if dialog.exec():dialog.apply_to(self)
    def buffers(self):
        from .dialogs import BufferDialog
        self.buffer_dialog=BufferDialog(self);self.buffer_dialog.show()
    def frequency_dialog(self):
        dialog=QDialog(self);dialog.setWindowTitle('Delta-f Ramp');layout=QVBoxLayout(dialog)
        spin=QDoubleSpinBox();spin.setRange(-5000,5000);spin.setSuffix(' Hz offset');spin.setValue(self.backend.frequency());layout.addWidget(spin)
        label=QLabel('RF is restored when this dialog closes. Active dispersion scans restore RF automatically.');label.setWordWrap(True);layout.addWidget(label)
        button=QPushButton('SET temporary RF offset');layout.addWidget(button)
        origin=self.backend.frequency_state()
        def apply():
            try:self.backend.set_frequency(spin.value());self.log('Temporary RF offset '+str(spin.value()))
            except Exception as e:self.error(str(e))
        button.clicked.connect(apply)
        dialog.exec()
        try:self.backend.restore_frequency(origin)
        except Exception as e:self.error('RF restoration failed: '+str(e))
    def sr_fit(self,data):
        from .dialogs import SRFitDialog
        self.sr_dialog=SRFitDialog(self,data);self.sr_dialog.show()
    def hardware_checklist(self):
        profile=json.loads((HERE/'hardware_profile.example.json').read_text())
        QMessageBox.information(self,'Hardware PV checklist','Known SAD parameter PVs: forms/legacy_inventory.json\nDevice profile: hardware_profile.example.json\n\n'+'\n'.join(profile_issues(profile)))
    def closeEvent(self,event):
        if self.task is not None and self.task.isRunning():
            self.service.cancel.set();self.log('Waiting for active scan restoration; close again after it finishes');event.ignore();return
        self.backend.close();event.accept()
