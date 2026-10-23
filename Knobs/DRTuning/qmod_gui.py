"""Resizable five-point Q modulation workspace with a selected-magnet table."""
import json
from pathlib import Path
import numpy as np
from PyQt6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QScrollArea,QPushButton,QComboBox,QLabel,QDoubleSpinBox,QInputDialog,QFileDialog
from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.core import serial
from .optics import fit_qmod,QModService

HERE=Path(__file__).resolve().parent
FAMILIES={'qf1':('QF1R',28),'qf2':('QF2R',26),'qm1':('QMTRIM1',23),'qm2':('QMTRIM2',23)}


class QModPanel(QWidget):
    def __init__(self,window,family):
        super().__init__();self.window=window;self.family=family;self.sessions={};self.rows={};self.enabled={};self.points={}
        from types import SimpleNamespace
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QTableWidget,QSplitter,QFormLayout,QTabWidget
        from .presentation import button,group,table_style
        self.form=SimpleNamespace()
        layout=QVBoxLayout(self)
        box,v=group('1. Magnet and scan range');layout.addWidget(box)
        row=QHBoxLayout();v.addLayout(row)
        self.selected=QComboBox();self.selected.setMinimumWidth(130);row.addWidget(QLabel('Magnet'));row.addWidget(self.selected)
        self.span=QDoubleSpinBox();self.span.setRange(.0001,2.);self.span.setDecimals(4);self.span.setValue(.02);self.span.setSuffix(' A');row.addWidget(QLabel('Full span'));row.addWidget(self.span)
        self.scan_button=button('Scan 5 points / restore',self.scan,True);row.addWidget(self.scan_button);row.addStretch()
        self.note=QLabel('Scan changes the selected magnet current. Editing table values changes recorded data only.');self.note.setWordWrap(True);v.addWidget(self.note)
        split=QSplitter();layout.addWidget(split,1)
        self.summary=QTableWidget(0,4);self.summary.setHorizontalHeaderLabels(['Magnet','Use','βx [m]','βy [m]']);table_style(self.summary);self.summary.verticalHeader().hide()
        split.addWidget(self.summary)
        detail=QWidget();right=QVBoxLayout(detail);split.addWidget(detail);split.setSizes([320,750]);split.setStretchFactor(1,1)
        self.result_tabs=QTabWidget();right.addWidget(self.result_tabs)
        box,v=group('2. Five-point data');self.result_tabs.addTab(box,'Five-point data')
        self.point_table=QTableWidget(5,5);self.point_table.setHorizontalHeaderLabels(['Use','Current [A]','Tune X','Tune Y','Read']);table_style(self.point_table)
        self.point_table.setVerticalHeaderLabels([str(i) for i in range(1,6)]);self.point_table.setMinimumHeight(240);v.addWidget(self.point_table)
        self.point_table.cellDoubleClicked.connect(self.edit_selected)
        hint=QLabel('Double-click a value to edit it. Read captures the current and tunes.');hint.setWordWrap(True);v.addWidget(hint)
        row=QHBoxLayout();v.addLayout(row)
        self.form.pushButtonCALC=button('Fit beta (CALC)',self.calculate);row.addWidget(self.form.pushButtonCALC)
        self.form.pushButtonCALC_2=button('Save results',self.save);row.addWidget(self.form.pushButtonCALC_2)
        row.addWidget(button('Open results',self.load))
        row.addWidget(button('SR beta fit from Q modulation',self.sr_beta))
        self.plot=Plot();self.result_tabs.addTab(self.plot,'Tune fits')
        key,count=FAMILIES[family];catalog=getattr(window.backend,'quadrupoles',{})
        names=[n for n,p in catalog.items() if p['family']==key]
        for r,name in enumerate(names,1):
            self.rows[r]=name;self.enabled[r]=True
            self.points[r]=[dict(used=True,current_A=None,tunes=[None,None]) for _ in range(5)]
        self.summary.setRowCount(len(names));self.selected.addItems(names)
        self.selected.currentIndexChanged.connect(self.select_magnet)
        self.summary.cellClicked.connect(lambda r,c:self.selected.setCurrentIndex(r))
        self.scan_button.setEnabled(bool(names))
        if not names:self.note.setText('No quadrupole trims are configured for this interface.')
        self.refresh()
    def selected_row(self):return self.selected.currentIndex()+1
    def sr_beta(self):
        from .sr_beta import SRBetaDialog
        sessions={}
        for panel in self.window.qmod.values():
            for r,n in panel.rows.items():
                if panel.enabled[r] and n in panel.sessions:sessions[n]=panel.sessions[n]
        if len(sessions)<3:self.window.error('At least three measured quadrupole betas are required');return
        estimate=dict(names=list(sessions),planes={a:dict(beta_m=[s['fit']['beta_m'][i] for s in sessions.values()]) for i,a in enumerate(('x','y'))})
        self.sr_dialog=SRBetaDialog(self.window,estimate);self.sr_dialog.show()
    def edit_selected(self,row,column):
        if self.selected_row() not in self.rows:return
        field={1:'Curr',2:'NX',3:'NY'}.get(column)
        if field:self.edit(self.selected_row(),row,field)
    def select_magnet(self,*_):
        self.refresh()
        result=self.sessions.get(self.selected.currentText())
        if result:self.draw(result)
        else:self.plot.figure.clear();self.plot.canvas.draw_idle()
    def refresh(self):
        from PyQt6.QtWidgets import QTableWidgetItem
        from .presentation import button
        for r,name in self.rows.items():
            self.summary.setItem(r-1,0,QTableWidgetItem(name))
            self.summary.setCellWidget(r-1,1,button('USE' if self.enabled[r] else 'OFF',lambda r=r:self.toggle(r)))
            result=self.sessions.get(name)
            for i in range(2):self.summary.setItem(r-1,i+2,QTableWidgetItem('—' if result is None else f"{result['fit']['beta_m'][i]:.5g}"))
        r=self.selected_row()
        if r not in self.rows:return
        self.summary.selectRow(r-1)
        for i,point in enumerate(self.points[r]):
            self.point_table.setCellWidget(i,0,button('USE' if point['used'] else 'OFF',lambda r=r,i=i:self.toggle(r,i)))
            for col,value in enumerate([point['current_A'],*point['tunes']],1):
                self.point_table.setItem(i,col,QTableWidgetItem('—' if value is None else f'{value:.7g}'))
            self.point_table.setCellWidget(i,4,button('Read',lambda r=r,i=i:self.get_point(r,i)))
    def toggle(self,r,p=None):
        if p is None:self.enabled[r]=not self.enabled[r]
        else:self.points[r][p]['used']=not self.points[r][p]['used']
        self.sessions.pop(self.rows[r],None);self.select_magnet()
    def edit(self,r,p,field):
        point=self.points[r][p];old=point['current_A'] if field=='Curr' else point['tunes'][int(field=='NY')]
        value,ok=QInputDialog.getDouble(self,'Edit measured '+field,'Recorded value (does not write magnet)',old or 0.,-10000,10000,8)
        if ok:
            if field=='Curr':point['current_A']=value
            else:point['tunes'][int(field=='NY')]=value
            point.pop('k1l',None);self.sessions.pop(self.rows[r],None);self.select_magnet()
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
        self.points[r]=[dict(v,used=True) for v in result['rows']];self.selected.setCurrentText(name);self.refresh();self.draw(result)
        if any(v.get('measurement',{}).get('same_shot_verified') is False for v in result['rows']):
            self.note.setText('Readback updates checked; common hardware shot ID unavailable. This qualification is saved with the result.')
    def calculate(self):
        if not any(p['current_A'] is not None for points in self.points.values() for p in points):
            self.window.error('Read, scan or load measurement points before fitting beta');return
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
        if not self.sessions:self.window.error('Calculate or load beta results before saving');return
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
