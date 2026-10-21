"""BPM DATA TO SAD and SR-monitor fit companions to the correction panels."""
import copy
import csv
import json
from pathlib import Path
import numpy as np
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,
    QComboBox,QFileDialog,QInputDialog)
from .core import checked_orbit,save_session,load_session
from .dataio import aligned_difference,parse_sad_orbits,fit_sr
from .gui import Plot


class BufferDialog(QDialog):
    def __init__(self,window):
        super().__init__(window);self.window=window
        self.buffers=window.orbit_buffers;self.setWindowTitle('BPM DATA TO SAD — A / B / standard buffers');self.resize(940,640)
        layout=QVBoxLayout(self);row=QHBoxLayout();layout.addLayout(row)
        self.slot=QComboBox();self.slot.addItems(['A','B','Standard','B - A']);self.slot.currentTextChanged.connect(self.draw);row.addWidget(self.slot)
        for title,action in [('READ',self.capture),('LOAD FILE',self.load),('COPY to Standard',self.copy_standard),('B - A',self.subtract),('SAVE data/figure',self.save),('SELECT MONITOR',window.selection)]:
            button=QPushButton(title);button.clicked.connect(action);row.addWidget(button)
        note=QLabel('Orbit units: mm. Legacy SAD ATFBPM values are converted from m. Import requires a confirmed M.i → BPM name map.');note.setWordWrap(True);layout.addWidget(note)
        self.plot=Plot();layout.addWidget(self.plot);self.draw()
    def store(self,data):
        checked_orbit(data,data['names'])
        if data.get('position_unit')!='mm':raise ValueError('Expected orbit in mm')
        self.buffers[self.slot.currentText()]=dict(data,schema='dr-orbit-1');self.draw()
    def capture(self):self.window.start(lambda _:self.window.backend.orbit(),self.store)
    def load(self):
        path,_=QFileDialog.getOpenFileName(self,'Load BPM buffer','','Orbit (*.json *.dat *.DAT);;All files (*)')
        if not path:return
        try:
            if Path(path).suffix.lower()=='.json':self.store(load_session(path));return
            mapping_path,_=QFileDialog.getOpenFileName(self,'Confirmed SAD M.i to BPM name mapping','','JSON (*.json)')
            if not mapping_path:return
            frames=parse_sad_orbits(Path(path).read_text(),json.loads(Path(mapping_path).read_text()))
            for slot,frame in zip(('A','B'),frames):self.buffers[slot]=frame
            self.slot.setCurrentText('A');self.draw()
        except Exception as exc:self.window.error(str(exc))
    def copy_standard(self):
        if self.slot.currentText() in self.buffers:self.buffers['Standard']=copy.deepcopy(self.buffers[self.slot.currentText()]);self.slot.setCurrentText('Standard');self.draw()
    def subtract(self):
        try:self.buffers['B - A']=aligned_difference(self.buffers['A'],self.buffers['B']);self.slot.setCurrentText('B - A');self.draw()
        except Exception as exc:self.window.error(str(exc))
    def draw(self,*_):
        self.plot.figure.clear();axes=self.plot.figure.subplots(2,1)
        data=self.buffers.get(self.slot.currentText())
        if data:
            positions=dict(zip(self.window.backend.bpm_names,self.window.backend.positions_m))
            if not set(data['names'])<=set(positions):self.window.error('Buffer includes unknown BPM positions');return
            s=[positions[n] for n in data['names']];x,y,valid=checked_orbit(data,data['names'])
            for ax,plane,values in zip(axes,('X','Y'),(x,y)):
                ax.scatter(np.array(s)[valid],values[valid],s=12);ax.set(xlabel='s [m]',ylabel=plane+' [mm]');ax.grid(alpha=.2)
        self.plot.figure.suptitle('BPM buffer '+self.slot.currentText());self.plot.canvas.draw_idle()
    def save(self,path=None):
        data=self.buffers.get(self.slot.currentText())
        if data is None:return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save BPM buffer and graphs')
        if not path:return
        root=Path(path);root.mkdir(parents=True,exist_ok=True);save_session(root/'orbit_buffer.json',data)
        with (root/'orbit_buffer.csv').open('w',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['BPM','x_mm','y_mm','intensity','valid'])
            writer.writerows(zip(data['names'],data['x'],data['y'],data['tmit'],data['valid']))
        for ext in ('png','pdf'):self.plot.save(root/f'orbit_buffer.{ext}')


class SRFitDialog(QDialog):
    def __init__(self,window,data=None):
        super().__init__(window);self.window=window;self.data=data;self.calibration=None;self.result=None
        self.setWindowTitle('DR SR monitor — dispersion fit');self.resize(960,700)
        layout=QVBoxLayout(self);row=QHBoxLayout();layout.addLayout(row)
        for title,action in [('Load dispersion file',self.load_data),('Load public response calibration',self.load_calibration),('CALC',self.calculate),('SAVE data/figure',self.save)]:
            button=QPushButton(title);button.clicked.connect(action);row.addWidget(button)
        self.note=QLabel('Fit X/Y dispersion to a supplied lattice response basis, then evaluate it at SR/SR2. A calibrated response is required; positions alone do not determine transport.');self.note.setWordWrap(True);layout.addWidget(self.note)
        self.plot=Plot();layout.addWidget(self.plot)
        if callable(getattr(window.backend,'sr_calibration',None)):self.calibration=window.backend.sr_calibration()
    def load_data(self):
        path,_=QFileDialog.getOpenFileName(self,'X/Y dispersion session','','JSON (*.json)')
        if path:
            try:self.data=load_session(path)
            except Exception as exc:self.window.error(str(exc))
    def load_calibration(self):
        path,_=QFileDialog.getOpenFileName(self,'Public response calibration','','JSON (*.json)')
        if path:
            try:self.calibration=json.loads(Path(path).read_text())
            except Exception as exc:self.window.error(str(exc))
    def calculate(self):
        try:
            if self.data is None or self.calibration is None:raise ValueError('Load dispersion data and SR response calibration first')
            self.result=fit_sr(self.data,self.calibration);self.draw()
        except Exception as exc:self.window.error(str(exc))
    def draw(self):
        data=self.result;self.plot.figure.clear();axes=self.plot.figure.subplots(2,1)
        s=np.asarray(data['positions_m']);order=np.argsort(s);valid=np.asarray(data['valid'],bool);lines=[]
        for i,(ax,plane) in enumerate(zip(axes,('x','y'))):
            result=data['planes'][plane];ax.scatter(s[valid],data['measured'][i,valid],s=12,c='black',label='Measured')
            ax.plot(s[order],np.asarray(result['fitted'])[order],label='Response-basis fit')
            for monitor,value in zip(data['monitors'],result['monitor_dispersion']):
                ax.axvline(monitor['s_m'],color='gray',ls=':');ax.scatter([monitor['s_m']],[value],marker='x',label=monitor['name'])
                lines.append(f"{monitor['name']} eta_{plane}={value:.5g} mm/(dp/p)")
            ax.set(xlabel='s [m]',ylabel='eta_'+plane+' [mm/(dp/p)]');ax.legend();ax.grid(alpha=.2)
        self.note.setText('; '.join(lines)+'\nCalibration: '+data['calibration_source']);self.plot.canvas.draw_idle()
    def save(self,path=None):
        if self.result is None:return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save SR fit and graphs')
        if not path:return
        root=Path(path);root.mkdir(parents=True,exist_ok=True);save_session(root/'sr_fit.json',self.result)
        with (root/'sr_fit.csv').open('w',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['BPM','s_m','eta_x_mm','fit_x_mm','eta_y_mm','fit_y_mm','valid'])
            writer.writerows(zip(self.result['names'],self.result['positions_m'],self.result['measured'][0],self.result['planes']['x']['fitted'],self.result['measured'][1],self.result['planes']['y']['fitted'],self.result['valid']))
        for ext in ('png','pdf'):self.plot.save(root/f'sr_fit.{ext}')
