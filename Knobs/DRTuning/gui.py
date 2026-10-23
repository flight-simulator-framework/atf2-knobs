"""DRTuning launcher: one shared interface, serialized machine operations."""
import json
import time
import traceback
from pathlib import Path
from PyQt6.QtCore import Qt,QTimer
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,
    QComboBox,QLineEdit,QPushButton,QFileDialog,QSpinBox,QCheckBox,QMessageBox,QDialog,QListWidget,QSplitter)
from Knobs.DRCorrection.gui import DRCorrectionWindow,TBTPanel,Task
from Knobs.DRCorrection.backends import DemoBackend,StoredBackend
from Knobs.DRCorrection.core import serial
from .qmod_gui import QModPanel,HERE,FAMILIES
from .optics import phase_beta
from .hardware import load_hardware_profile


class TuningTBTPanel(TBTPanel):
    def __init__(self,window):
        QWidget.__init__(self);self.window=window;self.wave=None;self.estimate=None
        self.phase_model=None;self.phase_result=None
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QFormLayout,QDoubleSpinBox,QSplitter
        from Knobs.DRCorrection.gui import Plot
        from .presentation import button,group,scroll
        self.legacy=SimpleNamespace()
        layout=QVBoxLayout(self);split=QSplitter();layout.addWidget(split)
        controls=QWidget();left=QVBoxLayout(controls)
        box,v=group('1. Waveform acquisition');left.addWidget(box)
        self.fresh=QCheckBox('Acquire a new shot')
        self.fresh.setToolTip('Temporarily selects DRBPM:ORBIT_MODE = 0 (TBT), verifies the readback, and restores the original mode after acquisition, failure or cancellation.')
        self.fresh.setChecked(False)
        self.fresh.setEnabled(hasattr(window.backend,'tbt_acquire'));v.addWidget(self.fresh)
        form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows);v.addLayout(form)
        self.turns=QSpinBox();self.turns.setRange(16,1024);self.turns.setValue(1024)
        self.start_acquisition=QSpinBox();self.start_acquisition.setRange(1,10000000);self.start_acquisition.setValue(1)
        self.start_acquisition.setEnabled(False);self.fresh.toggled.connect(self.start_acquisition.setEnabled)
        form.addRow('Turns',self.turns);form.addRow('First turn (new shot)',self.start_acquisition)
        self.update_button=button('Read waveform (Update)',self.acquire,True);v.addWidget(self.update_button)
        self.legacy.pushButtonUpdate=self.update_button
        self.fresh.toggled.connect(lambda checked:self.update_button.setText('Acquire new shot (Update)' if checked else 'Read waveform (Update)'))
        self.read_note=QLabel('Live: reads existing waveforms by default.\nNew shots use the external exciter.');self.read_note.setWordWrap(True);v.addWidget(self.read_note)
        row=QHBoxLayout();row.addWidget(button('Open file',self.load));row.addWidget(button('Save',self.save));v.addLayout(row)
        self.acquisition_status=QLabel('No data yet. Press Update to read waveforms.');self.acquisition_status.setWordWrap(True);v.addWidget(self.acquisition_status)
        self.legacy.lineEditSCANSTATUS=self.acquisition_status
        box,v=group('2. Display and analysis');left.addWidget(box);form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows);v.addLayout(form)
        self.bpm=QComboBox();self.bpm.addItems(window.backend.bpm_names)
        self.axis=QComboBox();self.axis.addItems(['BPM_X','BPM_Y','BPM_INTENSITY'])
        self.start_turn=QSpinBox();self.start_turn.setRange(1,1008);self.start_turn.setValue(3)
        form.addRow('BPM',self.bpm);form.addRow('Signal',self.axis);form.addRow('Fit start turn',self.start_turn)
        self.stats=QLabel('Mean / std: no data');self.stats.setWordWrap(True);v.addWidget(self.stats)
        self.analysis_button=button('Calculate tune / relative beta',self.analyze,True);v.addWidget(self.analysis_button)
        self.legacy.pushButtonFFTcalc=self.analysis_button
        v.addWidget(button('Calculate phase beta',self.phase_calculate))
        v.addWidget(button('Load phase optics',self.load_phase))
        v.addWidget(button('Use RFTrack model phase optics',self.model_phase))
        v.addWidget(button('SR / SR2 beta fit',self.sr_beta))
        box,v=group('Absolute beta calibration');left.addWidget(box)
        self.reference=QCheckBox('Use beta reference');v.addWidget(self.reference)
        self.reference_planes=QComboBox();self.reference_planes.addItems(['X and Y','X only','Y only']);v.addWidget(self.reference_planes)
        self.beta_x=QDoubleSpinBox();self.beta_y=QDoubleSpinBox();form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows);v.addLayout(form)
        for title,w in [('βx [m]',self.beta_x),('βy [m]',self.beta_y)]:
            w.setRange(.001,1000.);w.setValue(8.);w.setEnabled(False);form.addRow(title,w);self.reference.toggled.connect(w.setEnabled)
        self.reference_planes.setEnabled(False);self.reference.toggled.connect(self.reference_planes.setEnabled)
        hint=QLabel('The initial 8 m is a placeholder, not a calibration.');hint.setWordWrap(True);v.addWidget(hint)
        self.exciter=QComboBox();self.exciter.addItems(['External / no pulse']+window.backend.hcorrectors+window.backend.vcorrectors)
        self.pulse_A=QDoubleSpinBox();self.pulse_A.setRange(-5.,5.);self.pulse_A.setDecimals(3);self.pulse_A.setValue(.1)
        if not window.backend.label.startswith('EPICS'):
            box,v=group('Simulation excitation');left.addWidget(box);v.addWidget(self.exciter);v.addWidget(self.pulse_A);self.exciter.setCurrentIndex(1)
        else:self.exciter.hide();self.pulse_A.hide()
        box,v=group('Files / acquisition');left.addWidget(box)
        self.legacy.checkBoxAutoSave=QCheckBox('Auto-save after acquisition');v.addWidget(self.legacy.checkBoxAutoSave)
        self.legacy.lineEditANALYSEFILEPATH=QLineEdit();self.legacy.lineEditANALYSEFILEPATH.setPlaceholderText('Output folder');v.addWidget(self.legacy.lineEditANALYSEFILEPATH)
        v.addWidget(button('Choose output folder',self.select_save_directory));v.addWidget(button('Import legacy text',self.load_text))
        v.addWidget(button('Show acquisition settings',self.status))
        self.legacy.checkBoxSCANUP=QCheckBox('Advance first turn');self.legacy.checkBoxSCANDOWN=QCheckBox('Rewind first turn')
        v.addWidget(self.legacy.checkBoxSCANUP);v.addWidget(self.legacy.checkBoxSCANDOWN)
        self.legacy.lineEditTurn_2=QLineEdit('1024');form=QFormLayout();form.addRow('Turn step',self.legacy.lineEditTurn_2);v.addLayout(form)
        self.legacy.checkBoxSCANUP.toggled.connect(lambda checked:self.legacy.checkBoxSCANDOWN.setChecked(False) if checked else None)
        self.legacy.checkBoxSCANDOWN.toggled.connect(lambda checked:self.legacy.checkBoxSCANUP.setChecked(False) if checked else None)
        self.legacy.checkBoxEXTshot=QCheckBox('Use extraction turn');v.addWidget(self.legacy.checkBoxEXTshot);self.legacy.checkBoxEXTshot.clicked.connect(self.extraction_turn)
        left.addStretch();area=scroll(controls);area.setMinimumWidth(305);split.addWidget(area)
        result=QWidget();right=QVBoxLayout(result)
        self.note=QLabel('Waveform, FFT, beta and phase appear here.');self.note.setWordWrap(True);right.addWidget(self.note)
        self.graph=QComboBox();self.graph.addItems(['Waveform','FFT spectrum','Beta','Phase','Overview (large window)']);right.addWidget(self.graph)
        self.plot=Plot();right.addWidget(self.plot,1)
        self.plot.figure.text(.5,.5,'No waveform loaded\nPress Update or open a saved file',ha='center',va='center',fontsize=14,color='#64748b')
        split.addWidget(result);split.setSizes([320,800]);split.setStretchFactor(1,1)
        self.bpm.currentIndexChanged.connect(self.draw);self.axis.currentIndexChanged.connect(self.draw)
        self.bpm.currentIndexChanged.connect(lambda _:self.statistics() if self.wave is not None else None)
        self.graph.currentIndexChanged.connect(self.draw)
        nominal=HERE/'forms/nominal_phase_optics.json'
        if window.backend.label.startswith('RFTrack') and nominal.exists():self.phase_model=json.loads(nominal.read_text())
    def step_turn(self,direction):
        try:
            step=int(self.legacy.lineEditTurn_2.text())
            if step<=0:raise ValueError()
            self.start_acquisition.setValue(self.start_acquisition.value()+direction*step)
        except ValueError:self.window.error('Turn step must be a positive integer')
    def select_save_directory(self,*_):
        path=QFileDialog.getExistingDirectory(self,'TBT output directory')
        if path:self.legacy.lineEditANALYSEFILEPATH.setText(path)
        else:self.legacy.checkBoxAutoSave.setChecked(False)
    def extraction_turn(self,checked):
        if not checked:return
        turn=getattr(self.window.backend,'profile',{}).get('tbt',{}).get('extraction_turn')
        if turn is None:
            self.window.error('Set the confirmed extraction_turn in the hardware profile');self.legacy.checkBoxEXTshot.setChecked(False)
        else:self.start_acquisition.setValue(int(turn))
    def acquire(self):
        if self.fresh.isChecked():
            if self.legacy.checkBoxSCANUP.isChecked():self.step_turn(1)
            elif self.legacy.checkBoxSCANDOWN.isChecked():self.step_turn(-1)
        b=self.window.backend;turns=self.turns.value();start=self.start_acquisition.value()
        self.phase_result=None;self.acquisition_status.setText('Reading... Previous data is retained until success.')
        if hasattr(b,'tbt_acquire') and self.fresh.isChecked():
            self.window.start(lambda progress:b.tbt_acquire(turns,start,cancel=self.window.service.cancel,progress=progress),self.received,label='TBT new shot')
        else:
            if hasattr(b,'read_tbt'):
                self.window.start(lambda progress:b.read_tbt(turns,cancel=self.window.service.cancel,progress=progress),self.received,label='TBT waveform read')
                return
            if start!=1 and not hasattr(b,'tbt_acquire'):
                self.window.error('RFTrack currently records from turn 1; select an analysis window after acquisition');return
            if b.label.startswith('RFTrack'):
                pulse=None if self.exciter.currentIndex()==0 else dict(corrector=self.exciter.currentText(),delta_A=self.pulse_A.value(),turn=1)
                self.window.start(lambda _:b.tbt(turns,pulse,cancel=self.window.service.cancel),self.received,label='RFTrack acquisition')
            else:
                self.window.start(lambda _:b.tbt(turns),self.received,label='TBT acquisition')
    def received(self,wave):
        self.graph.blockSignals(True);self.graph.setCurrentIndex(0);self.graph.blockSignals(False)
        super().received(wave);self.phase_result=None
        first=wave['turns'][0];last=wave['turns'][-1]
        self.start_turn.setRange(first,max(first,last-15));self.start_turn.setValue(min(first+2,last-15))
        from datetime import datetime
        quality='Common shot ID unverified' if wave.get('readback_update_verified') else 'Existing waveform / simulation'
        self.acquisition_status.setText(f"Read complete {datetime.now():%H:%M:%S}\n{len(wave['names'])} BPM × {len(wave['turns'])} turns\n{quality}")
        self.statistics()
        if self.legacy.checkBoxAutoSave.isChecked():
            path=self.legacy.lineEditANALYSEFILEPATH.text().strip()
            if path:
                from datetime import datetime
                self.save(str(Path(path)/datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    def analyze(self):
        self.estimate=None
        super().analyze()
    def draw(self,*_):
        if self.wave is None or self.bpm.currentIndex()<0:return
        import numpy as np
        if self.graph.currentIndex()==4:return super().draw()
        figure=self.plot.figure;figure.clear();ax=figure.subplots()
        index=self.bpm.currentIndex();signal=('x','y','tmit')[self.axis.currentIndex()]
        values=np.asarray(self.wave[signal],float)[:,index];view=self.graph.currentIndex()
        if view==0:
            ax.plot(self.wave['turns'],values)
            ax.set(title=self.bpm.currentText(),xlabel='Turn',ylabel='Intensity' if signal=='tmit' else signal+' [mm]')
        elif view==1:
            centered=np.nan_to_num(values-np.nanmean(values));spectrum=abs(np.fft.rfft(centered*np.hanning(len(values))))
            ax.plot(np.fft.rfftfreq(len(values)),spectrum);ax.set(title=self.bpm.currentText(),xlabel='Fractional tune',ylabel='FFT amplitude')
        elif view==2 and self.phase_result:
            for plane,rows in self.phase_result['planes'].items():
                valid=[r for r in rows if r['valid']]
                ax.scatter([self.wave['names'].index(r['names'][0])+1 for r in valid],[r['beta_m'] for r in valid],label=plane.upper())
            ax.set(xlabel='BPM index',ylabel='Phase beta [m]');ax.legend()
        elif self.estimate:
            absolute=[]
            for plane,color in [('x','black'),('y','red')]:
                r=self.estimate['planes'][plane]
                if not r['valid']:continue
                calibrated=r.get('beta_m') is not None;absolute.append(calibrated)
                y=r['phase_rad'] if view==3 else r['beta_m'] if calibrated else r['relative_beta']
                unit=' rad' if view==3 else ' m' if calibrated else ' relative'
                ax.scatter(np.arange(1,len(self.wave['names'])+1),y,s=14,color=color,label=plane.upper()+unit)
            ylabel='Phase [rad]' if view==3 else 'Beta [m]' if absolute and all(absolute) else 'Relative beta' if not any(absolute) else 'Beta (units in legend)'
            ax.set(xlabel='BPM index',ylabel=ylabel)
            if ax.has_data():ax.legend()
        else:ax.text(.5,.5,'Calculate tune / beta first',ha='center',va='center',transform=ax.transAxes)
        ax.grid(alpha=.2);self.plot.canvas.draw_idle()
    def statistics(self):
        super().statistics()
        if self.wave is None:return
        import numpy as np
        i=self.bpm.currentIndex()
        lines=[]
        for key in ('x','y','tmit'):
            data=np.asarray(self.wave[key])[:,i]
            lines.append(f'{key}: {np.nanmean(data):.5g} ± {np.nanstd(data):.5g}'+(' mm' if key!='tmit' else ''))
        self.stats.setText('Mean +/- standard deviation\n'+'\n'.join(lines))
    def status(self):
        p=getattr(self.window.backend,'profile',{}).get('tbt',{})
        QMessageBox.information(self,'DRTBT status',self.window.backend.label+'\n'+json.dumps(p.get('acquisition',{}),indent=2))
    def load_phase(self):
        path,_=QFileDialog.getOpenFileName(self,'Independent nominal phase optics','','JSON (*.json)')
        if path:
            try:
                self.phase_model=json.loads(Path(path).read_text());self.window.log('Loaded phase optics: '+path)
            except Exception as e:self.window.error(str(e))
    def phase_calculate(self):
        if self.phase_model is None:self.window.error('Load independent nominal phase optics first');return
        self.analyze()
        if self.estimate is None:return
        try:
            self.phase_result=phase_beta(self.estimate,self.phase_model)
            self.graph.setCurrentIndex(2);self.draw()
        except Exception as e:self.window.error(str(e))
    def sr_beta(self):
        from .sr_beta import SRBetaDialog
        if self.estimate is None:self.analyze()
        if self.estimate is None:return
        self.sr_dialog=SRBetaDialog(self.window,self.estimate,self.phase_result);self.sr_dialog.show()
    def model_phase(self):
        def action(progress):
            from .model_optics import phase_calibration
            manager=self.window.service.model_manager
            return phase_calibration(manager.require_current(),manager.optics)
        def done(model):self.phase_model=model;self.window.log('Phase optics from independent settings-driven RFTrack model')
        self.window.start(action,done,label='Calculate reference phase transport')
    def save(self,path=None):
        if self.wave is None:self.window.error('Read or load a waveform before saving');return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save TBT data and figures')
        if not path:return
        super().save(path)
        if self.phase_result:(Path(path)/'phase_beta.json').write_text(json.dumps(serial(self.phase_result),indent=2)+'\n')


class TuningSession(DRCorrectionWindow):
    def __init__(self,backend):
        super().__init__(backend);self.setWindowTitle('DRTuning — '+backend.label)
        index=self.tabs.indexOf(self.tbt);old=self.tbt;self.tabs.removeTab(index);old.deleteLater()
        self.tbt=TuningTBTPanel(self);self.tabs.addTab(self.tbt,'Turn1024 / TBT beta')
        self.qmod={}
        for key,(family,_) in FAMILIES.items():
            panel=QModPanel(self,key);self.qmod[key]=panel;self.tabs.addTab(panel,'Q modulation / '+family)
        from .model_gui import ModelPanel
        self.model_panel=ModelPanel(self);self.tabs.addTab(self.model_panel,'RFTrack model / optics')
        from .emittance_gui import EmittancePanel
        self.emittance_panel=EmittancePanel(self);self.tabs.addTab(self.emittance_panel,'XSR emittance')
        from .presentation import STYLE,reflow_correction
        self.setStyleSheet(STYLE);self.resize(1280,820)
        for mode,panel in self.panels.items():
            if backend.identity.startswith('epics:') or backend.label.startswith('RFTrack'):
                panel.options.adaptive_probe=True
            if backend.identity.startswith('epics:'):
                panel.options.response_source='rftrack'
                if mode in ('orbit','dispersion'):
                    panel.options.count_h=5 if mode=='orbit' else 10
                    panel.options.count_v=5
            reflow_correction(panel)
        layout=self.centralWidget().layout();layout.removeWidget(self.tabs)
        self.navigation=QListWidget();self.navigation.setMinimumWidth(160);self.navigation.setMaximumWidth(185)
        self.navigation.addItems(['COD correction','Dispersion','Skew correction','TBT / beta','QF1R beta','QF2R beta','QM trim 1 beta','QM trim 2 beta','RFTrack model','XSR emittance'])
        self.tabs.tabBar().hide();self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.navigation.setCurrentRow);self.navigation.setCurrentRow(0)
        content=QSplitter();content.addWidget(self.navigation);content.addWidget(self.tabs);content.setStretchFactor(1,1)
        from PyQt6.QtWidgets import QSizePolicy
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
        content.setSizes([170,1100])
        layout.insertWidget(1,content,1)
        status=QWidget();row=QHBoxLayout(status);row.setContentsMargins(0,0,0,0)
        self.operation_status=QLabel('Ready. Select a tool from the left.');self.operation_status.setWordWrap(True)
        self.operation_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.elapsed_label=QLabel('');row.addWidget(self.operation_status,1);row.addWidget(self.elapsed_label);layout.insertWidget(1,status)
        self.messages.setMaximumHeight(130);self.messages.hide()
        self.log_toggle=QCheckBox('Show detailed log');self.log_toggle.toggled.connect(self.messages.setVisible);layout.insertWidget(layout.count()-1,self.log_toggle)
        self.cancel_button.setText('Cancel / restore');self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.progress.setRange(0,100);self.progress.setValue(0)
        self.timer=QTimer(self);self.timer.setInterval(250);self.timer.timeout.connect(self.tick)
        self._failed=False;self._active_label=''
        self.tool_actions={}
        for title,tools in (
            ('Measurements',[('TBT waveform / beta','tbt'),('QF1R beta','qf1'),('QF2R beta','qf2'),('QM trim 1 beta','qm1'),('QM trim 2 beta','qm2'),('XSR emittance','emittance')]),
            ('Corrections',[('COD correction','orbit'),('Dispersion','dispersion'),('Skew correction','skew')]),
            ('Utilities',[('RFTrack model / optics','model'),('Orbit buffers','buffers'),('BPM / magnet selection','selection'),('Delta-f Ramp','frequency_dialog'),('SR / SR2 dispersion fit','sr'),('SR / SR2 beta fit','sr_beta')])):
            menu=self.menuBar().addMenu(title)
            for label,key in tools:
                action=menu.addAction(label);action.triggered.connect(lambda checked=False,k=key:self.open_tool(k))
                self.tool_actions[key]=action
    def open_tool(self,key):
        if self.task and self.task.isRunning():
            self.log('Measurement running. Wait or cancel first.');return
        if key in self.panels:self.tabs.setCurrentWidget(self.panels[key])
        elif key in self.qmod:self.tabs.setCurrentWidget(self.qmod[key])
        elif key=='tbt':self.tabs.setCurrentWidget(self.tbt)
        elif key=='model':self.tabs.setCurrentWidget(self.model_panel)
        elif key=='emittance':self.tabs.setCurrentWidget(self.emittance_panel)
        elif key=='sr_beta':self.tabs.setCurrentWidget(self.tbt);self.tbt.sr_beta()
        elif key=='sr':
            data=self.panels['dispersion'].data
            if data is None:self.tabs.setCurrentWidget(self.panels['dispersion']);self.error('Measure dispersion with CALC before SR fit')
            else:self.sr_fit(data)
        else:getattr(self,key)()
    def tick(self):
        self.elapsed_label.setText(f'{time.monotonic()-self._started:.1f} s')
    def cancel_requested(self):
        if self.task is not None and self.task.isRunning():
            self.operation_status.setText('Cancel requested. Waiting for the current read / restoration.')
    def error(self,text):
        super().error(text)
        if hasattr(self,'operation_status'):
            self._failed=True
            message=str(text).strip().splitlines()[-1] if str(text).strip() else 'Unknown error'
            self.operation_status.setText('Failed: '+message)
            self.operation_status.setStyleSheet('color:#b42318;font-weight:bold;')
            self.log_toggle.setChecked(True)
            if self.tabs.currentWidget() is self.tbt:self.tbt.acquisition_status.setText('Failed: '+message+'\nSee the detailed log below.')
    def start(self,action,finished,label=None):
        if self.task is not None and self.task.isRunning():
            self.log('An operation is running. Wait for completion or cancel.');return
        self._failed=False;self._started=time.monotonic();self._active_label=label or self.navigation.currentItem().text()
        self.operation_status.setStyleSheet('color:#175c91;font-weight:bold;')
        self.operation_status.setText(self._active_label+': working...');self.log(self._active_label+': started')
        self.service.cancel.clear();self.tabs.setEnabled(False);self.navigation.setEnabled(False);self.cancel_button.setEnabled(True)
        self.progress.setRange(0,0);self.timer.start();self.tick()
        self.task=Task(action)
        def done(result):
            try:
                finished(result)
                if not self._failed:
                    self.operation_status.setText(self._active_label+': complete');self.operation_status.setStyleSheet('color:#166534;font-weight:bold;')
            except Exception:self.error(traceback.format_exc())
        self.task.result.connect(done);self.task.failure.connect(self.error)
        self.task.progress.connect(self.show_progress);self.task.finished.connect(self.task_finished);self.task.start()
    def show_progress(self,value,message):
        self.progress.setRange(0,100);self.progress.setValue(value)
        self.operation_status.setText(self._active_label+'：'+message)
        if message.startswith('SKIP '):self.log(message)
    def task_finished(self):
        self.timer.stop();self.tick();self.tabs.setEnabled(True);self.navigation.setEnabled(True);self.cancel_button.setEnabled(False)
        self.progress.setRange(0,100);self.progress.setValue(0 if self._failed else 100)
        for panel in self.panels.values():panel.refresh()
    def hardware_checklist(self):
        from Knobs.DRCorrection.backends import profile_issues
        p=getattr(self.backend,'profile',None)
        if p is None:
            QMessageBox.information(self,'Interface settings',self.backend.label+'\nNo live hardware profile is used.');return
        issues=profile_issues(p)
        text='Profile: '+p.get('profile_name',p.get('machine_id','custom'))+'\n'
        text+='\n'.join(issues) if issues else 'Configuration structure is valid. PV connections are checked during measurement.'
        QMessageBox.information(self,'Active hardware profile',text)
    def closeEvent(self,event):
        if self.task is not None and self.task.isRunning():
            self.service.cancel.set();self.log('Waiting for restoration');event.ignore();return
        super().closeEvent(event)


class DRTuningWindow(QDialog):
    """Select an interface first, then open the shared tuning window."""
    def __init__(self):
        super().__init__();self.session=None;self.task=None
        self.setWindowTitle('DRTuning — Select an Interface');self.resize(480,300)
        from Knobs.DRCorrection.input_guard import protect_settings_from_wheel
        from .presentation import STYLE
        from PyQt6.QtWidgets import QRadioButton,QButtonGroup,QDialogButtonBox,QFormLayout
        protect_settings_from_wheel(self);self.setStyleSheet(STYLE)
        layout=QVBoxLayout(self);layout.setSpacing(12)
        layout.addWidget(QLabel('ATF DR — Choose an interface:'))
        self.interface_group=QButtonGroup(self);self.interface_buttons={}
        for name in ('RFTrack','Machine (EPICS)','Stored DR (200 ms)','DEMO'):
            radio=QRadioButton(name);self.interface_group.addButton(radio)
            self.interface_buttons[name]=radio;layout.addWidget(radio)
        self.interface_buttons['RFTrack'].setChecked(True)
        self.directory=QLineEdit('/tmp/drtuning-session')
        self.stored_options=QWidget();form=QFormLayout(self.stored_options)
        form.addRow('Stored DR directory',self.directory);layout.addWidget(self.stored_options)
        self.stored_options.hide()
        self.interface_buttons['Stored DR (200 ms)'].toggled.connect(self.stored_options.setVisible)
        self.status=QLabel('Select an interface and press OK.');self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse);layout.addWidget(self.status)
        self.button_box=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.connect_backend);self.button_box.rejected.connect(self.reject)
        self.connect_button=self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.connect_button.setDefault(True);layout.addWidget(self.button_box)
    def connect_backend(self):
        if self.session or (self.task and self.task.isRunning()):return
        selection=self.interface_group.checkedButton().text();directory=self.directory.text()
        self.connect_button.setEnabled(False)
        for radio in self.interface_buttons.values():radio.setEnabled(False)
        self.directory.setEnabled(False);self.status.setText('Preparing '+selection+'…')
        def initialize(_):
            if selection=='RFTrack':
                from .rftrack import RFTrackBackend
                return RFTrackBackend()
            if selection.startswith('Machine'):
                from .epics import LiveBackend
                return LiveBackend(load_hardware_profile())
            if selection.startswith('Stored'):return StoredBackend(Path(directory))
            return DemoBackend()
        self.task=Task(initialize);self.task.result.connect(self.connected);self.task.failure.connect(self.failed);self.task.start()
    def connected(self,backend):
        try:self.session=TuningSession(backend)
        except Exception as e:
            backend.close();self.failed(str(e));return
        # Show the main window before closing the selector, so Qt always has
        # a visible window and closing the main window exits normally.
        self.session.show();self.accept()
    def failed(self,error):
        self.status.setText(error);self.connect_button.setEnabled(True);self.directory.setEnabled(True)
        for radio in self.interface_buttons.values():radio.setEnabled(True)
    def reject(self):
        if self.task and self.task.isRunning():
            self.status.setText('Preparing the interface. Wait for completion before closing.');return
        super().reject()
    def closeEvent(self,event):
        if self.task and self.task.isRunning():
            self.status.setText('Preparing the interface. Wait for completion before closing.');event.ignore()
        else:super().closeEvent(event)
