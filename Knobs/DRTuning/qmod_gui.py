"""Original op-base five-point Q modulation layouts, with local measured data."""
import json
from pathlib import Path
import numpy as np
from PyQt6 import uic
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QScrollArea,QPushButton,QComboBox,QLabel,QDoubleSpinBox,QInputDialog,QFileDialog
from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.core import serial
from .optics import fit_qmod,QModService

HERE=Path(__file__).resolve().parent
FAMILIES={'qf1':('QF1R',28),'qf2':('QF2R',26),'qm1':('QMTRIM1',23),'qm2':('QMTRIM2',23)}


class QModPanel(QWidget):
    def __init__(self,window,family):
        super().__init__();self.window=window;self.family=family;self.sessions={};self.rows={};self.enabled={};self.points={}
        self.form=uic.loadUi(str(HERE/'forms'/f'{family}.ui'));self.form.setFixedSize(self.form.size())
        layout=QVBoxLayout(self);row=QHBoxLayout();layout.addLayout(row)
        self.selected=QComboBox();row.addWidget(self.selected)
        self.span=QDoubleSpinBox();self.span.setRange(.0001,2.);self.span.setDecimals(4);self.span.setValue(.02);self.span.setSuffix(' A full span');row.addWidget(self.span)
        self.scan_button=QPushButton('Scan 5 points / restore');self.scan_button.clicked.connect(self.scan);row.addWidget(self.scan_button)
        load=QPushButton('Load measurement');load.clicked.connect(self.load);row.addWidget(load)
        for title,action in [('CALC',self.calculate),('SAVE',self.save)]:
            button=QPushButton(title);button.clicked.connect(lambda _,action=action:action());row.addWidget(button)
        self.note=QLabel('GET reads current + tune. CALC fits the selected points. Auto scan restores the current.');layout.addWidget(self.note)
        scroll=QScrollArea();scroll.setWidget(self.form);layout.addWidget(scroll,3)
        self.plot=Plot();layout.addWidget(self.plot,2)
        key,count=FAMILIES[family];catalog=getattr(window.backend,'quadrupoles',{})
        names=[n for n,p in catalog.items() if p['family']==key]
        self.selected.addItems(names)
        for r in range(1,count+1):
            name=names[r-1] if r<=len(names) else None;self.rows[r]=name;self.enabled[r]=bool(name)
            self.points[r]=[dict(used=True,current_A=None,tunes=[None,None]) for _ in range(5)]
            self.widget(f'qelabelQMagName_{r}').setText(name or 'Not configured')
            for w in self.form.findChildren(QWidget):
                if 'QMag' in w.objectName() and w.objectName().endswith('_'+str(r)) and name is None:w.setEnabled(False)
            self.widget(f'pushButtonQMagUse_{r}').clicked.connect(lambda _,r=r:self.toggle(r))
            for p in range(1,6):
                self.widget(f'qepushbuttonQMagReadBeta{p}_{r}').clicked.connect(lambda _,r=r,p=p:self.get_point(r,p-1))
                self.widget(f'pushButtonQMagCurrUseBeta{p}_{r}').clicked.connect(lambda _,r=r,p=p:self.toggle(r,p-1))
                for field in ('Curr','NX','NY'):
                    self.widget(f'pushButtonQMag{field}Beta{p}_{r}').clicked.connect(lambda _,r=r,p=p,f=field:self.edit(r,p-1,f))
            for plane in ('x','y'):
                w=self.widget(f'pushButtonQMagB{plane}_{r}')
                if w:w.setEnabled(False);w.setToolTip('Calculated measurement result')
        self.form.pushButtonCALC.clicked.connect(self.calculate)
        self.form.pushButtonCALC_2.clicked.connect(self.save)
        self.refresh()
    def widget(self,name):return self.form.findChild(QWidget,name)
    def refresh(self):
        for r,name in self.rows.items():
            self.widget(f'qelabelQMagUse_{r}').setText('USE' if self.enabled[r] else 'OFF')
            self.widget(f'pushButtonQMagUse_{r}').setText('USE' if self.enabled[r] else 'OFF')
            for p,point in enumerate(self.points[r],1):
                self.widget(f'qelabelQMagCurrUseBeta{p}_{r}').setText('USE' if point['used'] else 'OFF')
                self.widget(f'pushButtonQMagCurrUseBeta{p}_{r}').setText('USE' if point['used'] else 'OFF')
                for field,value in [('Curr',point['current_A']),('NX',point['tunes'][0]),('NY',point['tunes'][1])]:
                    self.widget(f'qelabelQMag{field}Beta{p}_{r}').setText('—' if value is None else f'{value:.7g}')
                    self.widget(f'pushButtonQMag{field}Beta{p}_{r}').setText('—' if value is None else f'{value:.5g}')
            result=self.sessions.get(name)
            for i,plane in enumerate(('x','y')):
                self.widget(f'qelabelQMagB{plane}_{r}').setText('—' if result is None else f"{result['fit']['beta_m'][i]:.5g}")
                self.widget(f'pushButtonQMagB{plane}_{r}').setText('—' if result is None else f"{result['fit']['beta_m'][i]:.4g}")
    def toggle(self,r,p=None):
        if p is None:self.enabled[r]=not self.enabled[r]
        else:self.points[r][p]['used']=not self.points[r][p]['used']
        self.sessions.pop(self.rows[r],None);self.refresh()
    def edit(self,r,p,field):
        point=self.points[r][p];old=point['current_A'] if field=='Curr' else point['tunes'][int(field=='NY')]
        value,ok=QInputDialog.getDouble(self,'Edit measured '+field,'Recorded value (does not write magnet)',old or 0.,-10000,10000,8)
        if ok:
            if field=='Curr':point['current_A']=value
            else:point['tunes'][int(field=='NY')]=value
            point.pop('k1l',None);self.sessions.pop(self.rows[r],None);self.refresh()
    def get_point(self,r,p):
        name=self.rows[r];b=self.window.backend
        def action(_):
            current=b.quadrupole_current(name);reading=b.measure_tunes(cancel=self.window.service.cancel)
            return dict(used=True,current_A=current,k1l=b.quadrupole_k1l(name,current),tunes=reading['tunes'],measurement=reading)
        def done(point):self.points[r][p]=point;self.sessions.pop(name,None);self.refresh()
        self.window.start(action,done)
    def scan(self):
        name=self.selected.currentText()
        if not name:self.window.error('No configured quadrupoles');return
        span=self.span.value();service=QModService(self.window.backend,self.window.service.cancel)
        def action(progress):
            origin=self.window.backend.quadrupole_current(name)
            return service.scan(name,origin+np.linspace(-span/2,span/2,5),progress)
        self.window.start(action,self.received)
    def received(self,result):
        name=result['name'];self.sessions[name]=result
        r=next(r for r,n in self.rows.items() if n==name)
        self.points[r]=[dict(v,used=True) for v in result['rows']];self.refresh();self.draw(result)
        if any(v.get('measurement',{}).get('same_shot_verified') is False for v in result['rows']):
            self.note.setText('Readback updates checked; common hardware shot ID unavailable. This qualification is saved with the result.')
    def calculate(self):
        for r,name in self.rows.items():
            if not self.enabled[r] or not name:continue
            points=self.points[r]
            if not any(p['current_A'] is not None for p in points):continue
            try:
                chosen=[p for p in points if p['used']]
                kval=[p['k1l'] if 'k1l' in p else self.window.backend.quadrupole_k1l(name,p['current_A']) for p in chosen]
                result=dict(schema='dr-qmod-1',name=name,source=self.window.backend.label,rows=points,
                            fit=fit_qmod(kval,[p['tunes'] for p in chosen]))
                self.sessions[name]=result;self.draw(result)
            except Exception as e:self.window.error(f'{name}: {e}')
        self.refresh()
    def draw(self,result):
        self.plot.figure.clear();axes=self.plot.figure.subplots(1,2);fit=result['fit']
        rows=[r for r in result['rows'] if r.get('used',True) and r.get('current_A') is not None]
        for i,ax in enumerate(axes):
            # The fit may have been computed from manually supplied current/calibration.
            x=np.array([r['k1l'] if 'k1l' in r else self.window.backend.quadrupole_k1l(result['name'],r['current_A']) for r in rows])
            ax.scatter(x,[r['tunes'][i] for r in rows]);ax.plot(x,fit['intercept_at_mean'][i]+fit['slope'][i]*(x-fit['mean_k1l']))
            ax.set(xlabel='K1L [1/m]',ylabel='Tune '+('X' if i==0 else 'Y'),title=f"β = {fit['beta_m'][i]:.4g} m")
        self.plot.canvas.draw_idle()
    def save(self,path=None):
        if not path:path=QFileDialog.getExistingDirectory(self,'Save Q modulation data / figures')
        if not path:return
        root=Path(path);root.mkdir(parents=True,exist_ok=True)
        (root/f'qmod_{self.family}.json').write_text(json.dumps(serial(list(self.sessions.values())),indent=2,allow_nan=False)+'\n')
        for name,result in self.sessions.items():
            self.draw(result);stem='qmod_'+name.replace(':','_')
            for ext in ('png','pdf'):self.plot.save(root/f'{stem}.{ext}')
            rows=result['rows'];np.savetxt(root/f'{stem}.csv',[[r.get('current_A',np.nan),*r['tunes']] for r in rows],delimiter=',',header='current_A,tune_x,tune_y')
        self.window.log('Saved Q modulation: '+str(root))
    def load(self):
        path,_=QFileDialog.getOpenFileName(self,'Q modulation measurements','','JSON (*.json)')
        if not path:return
        try:
            values=json.loads(Path(path).read_text())
            for result in values if isinstance(values,list) else [values]:
                if result['name'] in self.rows.values():self.received(result)
        except Exception as e:self.window.error(str(e))
