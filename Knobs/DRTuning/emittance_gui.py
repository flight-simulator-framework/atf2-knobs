"""Read-only XSR / measured-beta emittance worksheet."""
import copy
import json
from pathlib import Path
import time
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QDoubleSpinBox,QComboBox,QCheckBox,QSplitter,QFileDialog,QTableWidget,QTableWidgetItem,QSizePolicy)
from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.core import serial
from .presentation import button,group,scroll,table_style
from .emittance import estimate_emittance,sr_beta_values,sr_dispersion_values


class EmittancePanel(QWidget):
    def __init__(self,window):
        super().__init__();self.window=window;self.result=None;self.provenance={};self.fields={};self.errors={};self.corrections={}
        layout=QVBoxLayout(self);split=QSplitter();layout.addWidget(split)
        controls=QWidget();left=QVBoxLayout(controls)
        box,v=group('1. Source point and measured optics');left.addWidget(box)
        self.monitor=QComboBox();self.monitor.addItems(['SR','SR2']);v.addWidget(self.monitor)
        note=QLabel('Vertical emittance: use beta Y at the XSR emission point from the SR beta fit.\nXSR vertical RMS size: um, resolution already corrected. No second resolution subtraction.');note.setWordWrap(True);v.addWidget(note)
        v.addWidget(button('Use latest SR beta fit',self.latest_beta));v.addWidget(button('Load SR beta fit',self.load_beta))
        v.addWidget(button('Use latest SR dispersion fit',self.latest_dispersion));v.addWidget(button('Load SR dispersion fit',self.load_dispersion))
        self.same_optics=QCheckBox('Size and optics share this source point\nand the same machine state')
        v.addWidget(self.same_optics);self.same_optics.toggled.connect(self.invalidate)
        box,v=group('2. Vertical size and calculation inputs');left.addWidget(box)
        self.read_button=button('Read XSR vertical size [um]',self.read_sizes,True)
        self.read_button.setEnabled(callable(getattr(window.backend,'read_xsr_sizes',None)));v.addWidget(self.read_button)
        self.read_status=QLabel('Manual input is available on every interface. PV read does not trigger a new image.');self.read_status.setWordWrap(True);v.addWidget(self.read_status)
        specs=[('sigma_um','Vertical RMS size [um]',0.,1.e6,6),('beta_m','Local beta Y [m]',0.,10000.,6),
               ('dispersion_mm','Vertical dispersion [mm / (dp/p)]',-1.e6,1.e6,6),('sigma_delta','RMS dp/p (fraction)',0.,1.,8),
               ('resolution_um','Additional resolution [um]',0.,1.e5,6)]
        for axis in ('y',):
            page=QWidget();pv=QVBoxLayout(page);pv.setContentsMargins(0,0,0,0);v.addWidget(page)
            form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows);pv.addLayout(form)
            self.fields[axis]={};self.errors[axis]={}
            for key,label,lo,hi,decimals in specs:
                row=QWidget();h=QHBoxLayout(row);h.setContentsMargins(0,0,0,0)
                value=QDoubleSpinBox();value.setDecimals(decimals);value.setRange(lo,hi)
                error=QDoubleSpinBox();error.setDecimals(decimals);error.setRange(0.,hi);error.setPrefix('± ')
                error.setToolTip('Independent 1-sigma uncertainty, in the same unit');error.setEnabled(False)
                for w in (value,error):w.setMinimumWidth(0);w.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
                error.hide()
                value.valueChanged.connect(lambda _,a=axis,k=key:self.edited(a,k));error.valueChanged.connect(self.invalidate)
                self.fields[axis][key]=value;self.errors[axis][key]=error;h.addWidget(value);h.addWidget(error);form.addRow(label,row)
            self.fields[axis]['resolution_um'].setToolTip('Keep zero for the corrected XSR PV. Only subtract an additional known uncorrected source-plane resolution.')
            correction=QCheckBox('Remove known dispersion contribution');pv.addWidget(correction);self.corrections[axis]=correction
            correction.toggled.connect(self.invalidate)
        self.propagate=QCheckBox('Propagate supplied independent\n1-sigma errors');v.addWidget(self.propagate)
        self.propagate.toggled.connect(self.toggle_errors)
        hint=QLabel('Without dispersion correction, the result is apparent emittance. RMS dp/p is a fraction, e.g. 0.001 means 0.1%.\nA coupled beam gives an effective estimate, not separated normal-mode emittances.');hint.setWordWrap(True);v.addWidget(hint)
        left.addStretch();area=scroll(controls);area.setMinimumWidth(380);split.addWidget(area)
        result=QWidget();right=QVBoxLayout(result)
        self.status=QLabel('No estimate yet. Supply a measured size and absolute local beta.');self.status.setWordWrap(True);right.addWidget(self.status)
        actions=QHBoxLayout();actions.addWidget(button('Estimate vertical emittance',self.calculate,True));actions.addWidget(button('Save result and plot',self.save));right.addLayout(actions)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['Plane','Apparent\n[pm rad]','Estimate\n[pm rad]','1-sigma\n[pm rad]','Status']);table_style(self.table);right.addWidget(self.table,1)
        self.table.setMaximumHeight(180)
        self.plot=Plot();right.addWidget(self.plot,3);split.addWidget(result);split.setSizes([430,800]);split.setStretchFactor(1,1)
        self.monitor.currentTextChanged.connect(self.location_changed)
    def invalidate(self,*_):
        self.result=None
        if hasattr(self,'status'):
            self.status.setText('Inputs changed. Calculate again to update the estimate.');self.table.setRowCount(0)
            self.plot.figure.clear();self.plot.canvas.draw_idle()
    def edited(self,axis,key):
        self.provenance[axis+':'+key]={'source':'manual input','edited_unix_s':time.time()};self.invalidate()
    def toggle_errors(self,on):
        for fields in self.errors.values():
            for widget in fields.values():widget.setEnabled(on);widget.setVisible(on)
        self.invalidate()
    def location_changed(self,*_):
        self.same_optics.setChecked(False)
        for axis in ('y',):
            self.fields[axis]['beta_m'].setValue(0.);self.fields[axis]['dispersion_mm'].setValue(0.);self.corrections[axis].setChecked(False)
        self.invalidate()
    def set_values(self,key,values,source):
        if 'y' not in values:raise ValueError('Vertical measurement required')
        for axis,value in values.items():
            if axis!='y':continue
            widget=self.fields[axis][key];widget.blockSignals(True);widget.setValue(value);widget.blockSignals(False)
            if widget.value()!=round(value,widget.decimals()):raise ValueError('Imported value outside display range')
            self.provenance[axis+':'+key]=copy.deepcopy(source)
        self.invalidate()
    def import_beta(self,results,source='Measured SR beta fit'):
        if 'y' not in results:raise ValueError('Vertical SR beta fit required')
        vertical={'y':results['y']}
        self.set_values('beta_m',sr_beta_values(vertical,self.monitor.currentText()),dict(source=source,fit=serial(vertical)))
    def import_dispersion(self,result,source='Measured SR dispersion fit'):
        if 'y' not in result['planes']:raise ValueError('Vertical SR dispersion fit required')
        vertical=dict(result,planes={'y':result['planes']['y']})
        self.set_values('dispersion_mm',sr_dispersion_values(vertical,self.monitor.currentText()),dict(source=source,fit=serial(vertical)))
    def latest_beta(self):
        try:self.import_beta(self.window.latest_sr_beta)
        except Exception as e:self.window.error('Complete an SR beta fit first: '+str(e))
    def latest_dispersion(self):
        try:self.import_dispersion(self.window.latest_sr_dispersion)
        except Exception as e:self.window.error('Complete an SR dispersion fit first: '+str(e))
    def load_beta(self):self.load_fit('beta')
    def load_dispersion(self):self.load_fit('dispersion')
    def load_fit(self,kind):
        path,_=QFileDialog.getOpenFileName(self,'Load SR '+kind+' fit','','JSON (*.json)')
        if not path:return
        try:
            value=json.loads(Path(path).read_text())
            (self.import_beta if kind=='beta' else self.import_dispersion)(value,str(path))
        except Exception as e:self.window.error(str(e))
    def read_sizes(self):
        def done(reading):
            self.set_values('sigma_um',reading['sigma_um'],reading)
            # The live PV is already resolution corrected; discard a previous
            # manual additional-PSF setting to prevent double subtraction.
            self.set_values('resolution_um',dict(y=0.),dict(source='XSR PV already resolution corrected'))
            self.read_status.setText('Read corrected XSR vertical size [um]. Exposure freshness is not verified.')
        self.window.start(lambda _:self.window.backend.read_xsr_sizes(),done,label='Read XSR vertical size (no writes)')
    def calculate(self):
        self.result=None
        try:
            if not self.same_optics.isChecked():raise ValueError('Confirm that size and optics refer to the same source point and machine state')
            results={}
            for axis in ('y',):
                values={k:w.value() for k,w in self.fields[axis].items()}
                errors={k:w.value() for k,w in self.errors[axis].items()} if self.propagate.isChecked() else None
                results[axis]=estimate_emittance(**values,correct_dispersion=self.corrections[axis].isChecked(),uncertainties=errors)
            self.result=dict(schema='dr-xsr-emittance-1',created_unix_s=time.time(),monitor=self.monitor.currentText(),
                             same_source_and_optics_confirmed=True,planes=results,provenance=copy.deepcopy(self.provenance))
            self.table.setRowCount(len(results));self.plot.figure.clear();axes=self.plot.figure.subplots(len(results),1,squeeze=False).ravel()
            fmt=lambda value:'unknown' if value is None else f'{value:.5g}'
            for i,(axis,r) in enumerate(results.items()):
                for j,value in enumerate([axis.upper(),fmt(r['apparent_pm_rad']),fmt(r['emittance_pm_rad']),fmt(r['uncertainty_pm_rad']),r['status']]):
                    item=QTableWidgetItem(value);item.setToolTip(r['estimate_kind']+'; '+r['status']);self.table.setItem(i,j,item)
                axes[i].bar(['Size²','Dispersion','Extra resolution','Betatron'],[r['inputs']['sigma_um']**2,r['dispersive_variance_um2'],r['resolution_variance_um2'],r['betatron_variance_um2']])
                axes[i].set(ylabel='Variance [um²]',title=axis.upper()+': '+r['estimate_kind']);axes[i].axhline(0,color='gray',lw=.7)
            self.plot.canvas.draw_idle()
            self.status.setText('\n'.join(a.upper()+': '+r['estimate_kind']+'; '+r['status'] for a,r in results.items())+'\nGeometric emittance. Unknown errors are not treated as zero uncertainty.')
        except Exception as e:self.window.error(str(e))
    def save(self,path=None):
        if self.result is None:self.window.error('Calculate an estimate first');return
        if not path:path=QFileDialog.getExistingDirectory(self,'Save XSR emittance estimate')
        if not path:return
        root=Path(path);root.mkdir(parents=True,exist_ok=True)
        (root/'xsr_emittance.json').write_text(json.dumps(serial(self.result),indent=2,allow_nan=False)+'\n')
        self.plot.save(root/'xsr_emittance.png');self.window.log('Saved XSR emittance estimate: '+str(root))
