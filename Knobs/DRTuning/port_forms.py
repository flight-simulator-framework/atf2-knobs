"""Reproduce legacy Qt geometries without QE plugins or implicit PV writes."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

HERE=Path(__file__).resolve().parent
SOURCES={
 'qf1':'/home/motokisato/Projects/op-base261019/sad_beta/BETA_QF1R/beta_qf1r.ui',
 'qf2':'/home/motokisato/Projects/op-base261019/sad_beta/BETA_QF2R/beta_qf2r.ui',
 'qm1':'/home/motokisato/Projects/op-base261019/sad_beta/BETA_QTRIM1/beta_qtrim1.ui',
 'qm2':'/home/motokisato/Projects/op-base261019/sad_beta/BETA_QTRIM2/beta_qtrim2.ui',
 'select':'/home/motokisato/Projects/op-base261019/sad_beta/BETA_SELECT_WIN/beta_select_win.ui',
 'tbt':'/home/motokisato/Projects/op-base/monitor/TBT/Turn1024/turn1024.ui'}

def main():
    inventory={}
    keep={'geometry','text','windowTitle','font','styleSheet','alignment','minimumSize','maximumSize',
          'wordWrap','readOnly','checked','checkable','value','minimum','maximum','singleStep','orientation',
          'title','currentIndex','enabled','frameShape','frameShadow','horizontalScrollBarPolicy','verticalScrollBarPolicy',
          'widgetResizable','sizePolicy','decimals','suffix','prefix'}
    for key,path in SOURCES.items():
        data=Path(path).read_bytes();tree=ET.fromstring(data);widgets=[]
        for parent in tree.iter():
            for child in list(parent):
                if child.tag in ('customwidgets','connections','resources','tabstops'):parent.remove(child)
        for w in tree.iter('widget'):
            old=w.attrib['class'];name=w.attrib['name']
            widgets.append(dict(name=name,qt_class=old,pvs=[''.join(p.itertext()).strip() for p in w.findall('property') if 'variable' in p.attrib['name'].lower()]))
            if old.startswith('QE'):w.set('class','QPushButton' if 'Button' in old else 'QLabel')
            elif old=='LineEditInput':w.set('class','QLineEdit')
            elif old not in ('QWidget','QLabel','QPushButton','QLineEdit','QComboBox','QCheckBox','QRadioButton','QSpinBox','QDoubleSpinBox','QFrame','QGroupBox','QScrollArea','QTabWidget','QTextEdit','QPlainTextEdit'):
                w.set('class','QWidget')
            for p in list(w):
                if p.tag=='property' and p.attrib['name'] not in keep:w.remove(p)
                elif p.tag=='property' and w.attrib['class']=='QWidget' and p.attrib['name'] not in {'geometry','minimumSize','maximumSize','font','styleSheet','enabled','windowTitle','sizePolicy'}:w.remove(p)
        ET.indent(tree);ET.ElementTree(tree).write(HERE/'forms'/f'{key}.ui',encoding='utf-8',xml_declaration=True)
        inventory[key]=dict(source=path,sha256=hashlib.sha256(data).hexdigest(),widgets=widgets)
    (HERE/'forms/inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')

if __name__=='__main__':main()
