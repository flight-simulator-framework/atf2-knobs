"""DRTuning launcher: one shared interface, serialized machine operations."""
import json
from pathlib import Path
from PyQt6 import uic
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,
    QComboBox,QLineEdit,QPushButton,QFileDialog,QSpinBox,QCheckBox,QScrollArea,QMessageBox,QDialog)
from Knobs.DRCorrection.gui import DRCorrectionWindow,TBTPanel,Task
from Knobs.DRCorrection.backends import DemoBackend,StoredBackend
from Knobs.DRCorrection.core import serial
from .qmod_gui import QModPanel,HERE,FAMILIES
from .optics import phase_beta


class TuningTBTPanel(TBTPanel):
    def __init__(self,window):
        super().__init__(window);self.phase_model=None;self.phase_result=None
        self.legacy=uic.loadUi(str(HERE/'forms/tbt.ui'));self.legacy.setFixedSize(self.legacy.size())
        self.legacy.label_19.setText('Excitation: simulation pulse / external hardware exciter')
        self.layout().removeWidget(self.plot)
        plot_layout=QVBoxLayout(self.legacy.widget);plot_layout.setContentsMargins(0,0,0,0);plot_layout.addWidget(self.plot)
        scroll=QScrollArea();scroll.setWidget(self.legacy);scroll.setMinimumHeight(260)
        self.layout().insertWidget(0,scroll)
        self.fresh=QCheckBox('Acquire new shot (start / completion / restore)')
        self.fresh.setChecked(hasattr(window.backend,'tbt_acquire'))
        self.start_acquisition=QSpinBox();self.start_acquisition.setRange(1,10000000);self.start_acquisition.setValue(1)
        row=QHBoxLayout();row.addWidget(self.fresh);row.addWidget(QLabel('First turn'));row.addWidget(self.start_acquisition)
        load=QPushButton('Load phase optics');load.clicked.connect(self.load_phase);row.addWidget(load)
        phase=QPushButton('Three-BPM phase beta');phase.clicked.connect(self.phase_calculate);row.addWidget(phase)
        self.layout().insertLayout(1,row)
        actions={'pushButtonUpdate':self.acquire,'pushButtonSaveAllBPMDATA':self.save,'pushButtonCALCAveStv':self.statistics,
            'pushButtonFFTcalc':self.analyze,'pushButtonShowDRbetaPlot':self.show_plot,'pushButtonUpdateDRbetaPlot':self.draw,
            'pushButtonShowDRTBTplot':self.show_plot,'pushButtonBetaCalcFILE':self.load,'pushButtonShowDRTBTstatusMonitor':self.status}
        for name,action in actions.items():getattr(self.legacy,name).clicked.connect(lambda _,action=action:action())
        for name in ('pushButtoncalcFITBETA','pushButtonBETAcalcFit','pushButtoncalcFITBETA_Kubo','pushButtonBetaCalcFILE_Kubo'):
            getattr(self.legacy,name).clicked.connect(self.sr_beta)
        self.legacy.pushButtonNext.clicked.connect(lambda:self.bpm.setCurrentIndex(min(self.bpm.count()-1,self.bpm.currentIndex()+1)))
        self.legacy.pushButtonPre.clicked.connect(lambda:self.bpm.setCurrentIndex(max(0,self.bpm.currentIndex()-1)))
        self.legacy.pushButtonUp.clicked.connect(lambda:self.step_turn(1))
        self.legacy.pushButtonDown.clicked.connect(lambda:self.step_turn(-1))
        self.legacy.lineEditTurn_2.setText('1024')
        self.legacy.lineEditTurn.setText('1')
        self.legacy.lineEditTurn.editingFinished.connect(self.set_turn)
        self.legacy.lineEditBPMNum_2.editingFinished.connect(self.set_bpm)
        for name,index in [('radioButtonBPMX',0),('radioButtonBPMY',1),('radioButtonBPMINT',2)]:
            getattr(self.legacy,name).clicked.connect(lambda _,i=index:self.axis.setCurrentIndex(i))
        # The old external-shot service and SAD fitting executables are independent
        # controls, not commands to run shell scripts on a remote machine.
        self.legacy.checkBoxEXTshot.clicked.connect(self.extraction_turn)
        self.legacy.checkBoxTESTMODE.setEnabled(False);self.legacy.checkBoxTESTMODE.setToolTip('Select DEMO in DRTuning to use synthetic data')
        self.legacy.checkBoxSCANDOWN.clicked.connect(lambda v:self.legacy.checkBoxSCANUP.setChecked(False) if v else None)
        self.legacy.checkBoxSCANUP.clicked.connect(lambda v:self.legacy.checkBoxSCANDOWN.setChecked(False) if v else None)
        self.legacy.checkBoxAutoSave.clicked.connect(self.select_save_directory)
        self.legacy.pushButtonSetFILEname.clicked.connect(self.select_save_directory)
        for name in ('pushButtonUpperLimUp','pushButtonUpperLimDown','pushButtonLowerLimUp','pushButtonLowerLimDown'):
            getattr(self.legacy,name).clicked.connect(lambda _,n=name:self.zoom(n))
        self.legacy.lineEditMaxSet.editingFinished.connect(self.set_plot_limits)
        self.legacy.lineEditMinSet.editingFinished.connect(self.set_plot_limits)
        self.bpm.currentIndexChanged.connect(lambda _:self.legacy.lineEditBPMNum_2.setText(str(self.bpm.currentIndex()+1)))
        self.start_acquisition.valueChanged.connect(lambda v:self.legacy.lineEditTurn.setText(str(v)))
        nominal=HERE/'forms/nominal_phase_optics.json'
        if window.backend.label.startswith('RFTrack') and nominal.exists():self.phase_model=json.loads(nominal.read_text())
    def step_turn(self,direction):
        try:
            step=int(self.legacy.lineEditTurn_2.text())
            if step<=0:raise ValueError()
            self.start_acquisition.setValue(self.start_acquisition.value()+direction*step)
        except ValueError:self.window.error('Turn step must be a positive integer')
    def show_plot(self):
        if self.wave is None:return
        from Knobs.DRCorrection.gui import Plot
        self.plot_dialog=QDialog(self);self.plot_dialog.setWindowTitle('DR TBT / beta');self.plot_dialog.resize(1050,780)
        layout=QVBoxLayout(self.plot_dialog);plot=Plot();layout.addWidget(plot)
        original=self.plot
        try:self.plot=plot;self.draw()
        finally:self.plot=original
        self.plot_dialog.show()
    def set_plot_limits(self):
        if not self.plot.figure.axes:return
        try:
            scale=.001 if self.axis.currentIndex()!=2 else 1.
            lo=float(self.legacy.lineEditMinSet.text())*scale;hi=float(self.legacy.lineEditMaxSet.text())*scale
            if hi<=lo:raise ValueError()
            self.plot.figure.axes[0].set_ylim(lo,hi);self.plot.canvas.draw_idle()
        except ValueError:self.window.error('Invalid plot limits')
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
    def set_turn(self):
        try:self.start_acquisition.setValue(int(self.legacy.lineEditTurn.text()))
        except ValueError:self.window.error('Invalid first turn')
    def set_bpm(self):
        try:self.bpm.setCurrentIndex(max(0,min(self.bpm.count()-1,int(self.legacy.lineEditBPMNum_2.text())-1)))
        except ValueError:self.window.error('Invalid BPM index')
    def zoom(self,name):
        if not self.plot.figure.axes:return
        ax=self.plot.figure.axes[0];low,high=ax.get_ylim();delta=(high-low)*.1*(1 if name.endswith('Up') else -1)
        if 'Upper' in name:high+=delta
        else:low+=delta
        if high>low:ax.set_ylim(low,high);self.plot.canvas.draw_idle()
    def acquire(self):
        if self.legacy.checkBoxSCANUP.isChecked():self.step_turn(1)
        elif self.legacy.checkBoxSCANDOWN.isChecked():self.step_turn(-1)
        b=self.window.backend;turns=self.turns.value();start=self.start_acquisition.value()
        self.phase_result=None;self.legacy.lineEditSCANSTATUS.setText('Acquiring')
        if hasattr(b,'tbt_acquire') and self.fresh.isChecked():
            self.window.start(lambda _:b.tbt_acquire(turns,start,cancel=self.window.service.cancel),self.received)
        else:
            if start!=1 and not hasattr(b,'tbt_acquire'):
                self.window.error('RFTrack currently records from turn 1; select an analysis window after acquisition');return
            if b.label.startswith('RFTrack'):
                pulse=None if self.exciter.currentIndex()==0 else dict(corrector=self.exciter.currentText(),delta_A=self.pulse_A.value(),turn=1)
                self.window.start(lambda _:b.tbt(turns,pulse,cancel=self.window.service.cancel),self.received)
            else:super().acquire()
    def received(self,wave):
        super().received(wave);self.phase_result=None
        first=wave['turns'][0];last=wave['turns'][-1]
        self.start_turn.setRange(first,max(first,last-15));self.start_turn.setValue(min(first+2,last-15))
        self.legacy.lineEditSCANSTATUS.setText('Complete' if wave.get('fresh_acquisition') else 'Updated; shot ID unverified' if wave.get('readback_update_verified') else 'Read / simulated')
        if self.legacy.checkBoxAutoSave.isChecked():
            path=self.legacy.lineEditANALYSEFILEPATH.text().strip()
            if path:
                from datetime import datetime
                self.save(str(Path(path)/datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    def analyze(self):
        self.estimate=None
        super().analyze()
    def statistics(self):
        super().statistics()
        if self.wave is None:return
        import numpy as np
        i=self.bpm.currentIndex()
        for key,prefix in [('x','X'),('y','Y'),('tmit','BInt')]:
            data=np.asarray(self.wave[key])[:,i];factor=1000 if key!='tmit' else 1
            getattr(self.legacy,'lineEdit'+prefix+'Ave').setText(f'{np.nanmean(data)*factor:.5g}')
            getattr(self.legacy,'lineEdit'+prefix+'Std').setText(f'{np.nanstd(data)*factor:.5g}')
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
            axes=self.plot.figure.axes
            ax=axes[2];ax.clear()
            for axis,rows in self.phase_result['planes'].items():
                good=[r for r in rows if r['valid']];ax.scatter([self.wave['names'].index(r['names'][0]) for r in good],[r['beta_m'] for r in good],label=axis)
            ax.set(xlabel='BPM index',ylabel='Phase beta [m]');ax.legend();self.plot.canvas.draw_idle()
        except Exception as e:self.window.error(str(e))
    def sr_beta(self):
        from .sr_beta import SRBetaDialog
        if self.estimate is None:self.analyze()
        if self.estimate is None:return
        self.sr_dialog=SRBetaDialog(self.window,self.estimate,self.phase_result);self.sr_dialog.show()
    def save(self,path=None):
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
        self.owned_by_launcher=False
    def closeEvent(self,event):
        if self.task is not None and self.task.isRunning():
            self.service.cancel.set();self.log('Waiting for restoration');event.ignore();return
        if self.owned_by_launcher:self.hide();event.ignore()
        else:super().closeEvent(event)


class DRTuningWindow(QMainWindow):
    def __init__(self):
        super().__init__();self.session=None;self.task=None;self.setWindowTitle('DRTuning');self.resize(720,540)
        widget=QWidget();self.setCentralWidget(widget);layout=QVBoxLayout(widget)
        form=QHBoxLayout();layout.addLayout(form);form.addWidget(QLabel('Interface'))
        self.interface=QComboBox();self.interface.addItems(['RFTrack','実機 (EPICS)','Stored DR (200 ms)','DEMO']);form.addWidget(self.interface)
        self.connect_button=QPushButton('Connect / initialize');self.connect_button.clicked.connect(self.connect_backend);form.addWidget(self.connect_button)
        self.disconnect_button=QPushButton('Disconnect');self.disconnect_button.clicked.connect(self.disconnect);form.addWidget(self.disconnect_button)
        self.profile=QLineEdit();self.profile.setPlaceholderText('実機用 hardware profile JSON');layout.addWidget(self.profile)
        browse=QPushButton('Select hardware profile');browse.clicked.connect(self.browse);layout.addWidget(browse)
        self.directory=QLineEdit('/tmp/drtuning-session');layout.addWidget(self.directory)
        self.status=QLabel('Interfaceを選択して初期化してください。');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.buttons=QWidget();grid=QGridLayout(self.buttons);layout.addWidget(self.buttons);self.buttons.setEnabled(False)
        tools=[('DR COD Correction','orbit'),('DR Dispersion Correction','dispersion'),('DR Skew Correction','skew'),
            ('Turn1024 / TBT beta','tbt'),('Beta: QF1R.1–28','qf1'),('Beta: QF2R.1–26','qf2'),
            ('Beta: QM1R–23R.1','qm1'),('Beta: QM1R–23R.2','qm2'),('BPM DATA TO SAD','buffers'),
            ('FIX DR STEER','selection'),('Delta-f Ramp','frequency_dialog'),('SR / SR2 dispersion fit','sr')]
        for i,(title,key) in enumerate(tools):
            button=QPushButton(title);button.clicked.connect(lambda _,k=key:self.open_tool(k));grid.addWidget(button,i//2,i%2)
    def browse(self):
        path,_=QFileDialog.getOpenFileName(self,'Hardware profile','','JSON (*.json)')
        if path:self.profile.setText(path)
    def connect_backend(self):
        if self.session or (self.task and self.task.isRunning()):return
        selection=self.interface.currentText();profile=self.profile.text();directory=self.directory.text()
        self.connect_button.setEnabled(False);self.interface.setEnabled(False);self.status.setText('Initializing '+selection)
        def initialize(_):
            if selection=='RFTrack':
                from .rftrack import RFTrackBackend
                return RFTrackBackend()
            if selection.startswith('実機'):
                from .epics import LiveBackend
                if not profile:raise ValueError('Select the confirmed hardware profile JSON')
                return LiveBackend(json.loads(Path(profile).read_text()))
            if selection.startswith('Stored'):return StoredBackend(Path(directory))
            return DemoBackend()
        self.task=Task(initialize);self.task.result.connect(self.connected);self.task.failure.connect(self.failed);self.task.start()
    def connected(self,backend):
        try:self.session=TuningSession(backend)
        except Exception as e:
            backend.close();self.failed(str(e));return
        self.session.owned_by_launcher=True;self.buttons.setEnabled(True);self.status.setText(backend.label)
    def failed(self,error):
        self.status.setText(error);self.connect_button.setEnabled(True);self.interface.setEnabled(True)
    def open_tool(self,key):
        if self.session is None:return
        s=self.session
        if s.task and s.task.isRunning():self.status.setText('測定中です。完了またはCancel後に操作してください。');return
        s.show();s.raise_()
        if key in s.panels:s.tabs.setCurrentWidget(s.panels[key])
        elif key in s.qmod:s.tabs.setCurrentWidget(s.qmod[key])
        elif key=='tbt':s.tabs.setCurrentWidget(s.tbt)
        elif key=='sr':
            data=s.panels['dispersion'].data
            if data is None:s.tabs.setCurrentWidget(s.panels['dispersion']);s.error('Measure dispersion with CALC before SR fit')
            else:s.sr_fit(data)
        else:getattr(s,key)()
    def disconnect(self):
        if self.task and self.task.isRunning():return False
        if self.session:
            if self.session.task and self.session.task.isRunning():
                self.session.service.cancel.set();self.status.setText('キャンセル・設定復帰の完了を待っています。');return False
            self.session.owned_by_launcher=False;self.session.close();self.session.deleteLater();self.session=None
        self.buttons.setEnabled(False);self.interface.setEnabled(True);self.connect_button.setEnabled(True);return True
    def closeEvent(self,event):
        if self.disconnect():event.accept()
        else:event.ignore()
