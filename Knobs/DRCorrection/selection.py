"""Offline selection controls shared by the correction GUIs."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView,QDialog,QDialogButtonBox,QGroupBox,
    QHBoxLayout,QVBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,QPushButton)


class SelectionList(QGroupBox):
    def __init__(self,title,names,excluded,parent=None):
        super().__init__(title,parent)
        layout=QVBoxLayout(self)
        self.search=QLineEdit();self.search.setPlaceholderText('Filter names (space-separated alternatives)')
        layout.addWidget(self.search)
        self.items=QListWidget();self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for name in names:
            item=QListWidgetItem(name)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if name in excluded else Qt.CheckState.Checked)
            self.items.addItem(item)
        self.actions={}
        for specs in (
            [('All',lambda:self.choose(names)),('None',lambda:self.choose([])),
             ('Only highlighted',self.only_highlighted)],
            [('Check matches',lambda:self.set_matches(True)),
             ('Uncheck matches',lambda:self.set_matches(False))]):
            row=QHBoxLayout();layout.addLayout(row)
            for title,action in specs:
                button=QPushButton(title);button.clicked.connect(lambda _,f=action:f())
                row.addWidget(button);self.actions[title]=button
        layout.addWidget(self.items)
        self.count=QLabel();self.count.setWordWrap(True);layout.addWidget(self.count)
        self.search.textChanged.connect(self.filter)
        self.items.itemChanged.connect(self.refresh)
        self.items.itemSelectionChanged.connect(self.refresh)
        self.refresh()

    def rows(self):return [self.items.item(i) for i in range(self.items.count())]
    def excluded(self):return tuple(item.text() for item in self.rows() if item.checkState()!=Qt.CheckState.Checked)
    def choose(self,names):
        names=set(names);self.items.blockSignals(True)
        for item in self.rows():item.setCheckState(Qt.CheckState.Checked if item.text() in names else Qt.CheckState.Unchecked)
        self.items.blockSignals(False);self.refresh()
    def filter(self,text):
        terms=text.casefold().split()
        for item in self.rows():item.setHidden(bool(terms) and not any(term in item.text().casefold() for term in terms))
        self.refresh()
    def set_matches(self,checked):
        self.items.blockSignals(True)
        for item in self.rows():
            if not item.isHidden():item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.items.blockSignals(False);self.refresh()
    def only_highlighted(self):
        names=[item.text() for item in self.items.selectedItems() if not item.isHidden()]
        if names:self.choose(names)
    def refresh(self,*_):
        rows=self.rows();checked=sum(item.checkState()==Qt.CheckState.Checked for item in rows)
        visible=sum(not item.isHidden() for item in rows)
        hidden_checked=sum(item.isHidden() and item.checkState()==Qt.CheckState.Checked for item in rows)
        self.count.setText(f'{checked} / {len(rows)} checked; {visible} shown; {hidden_checked} checked outside filter')
        self.actions['Only highlighted'].setEnabled(any(not item.isHidden() for item in self.items.selectedItems()))


class SelectionDialog(QDialog):
    def __init__(self,window):
        super().__init__(window);self.setWindowTitle('BPM / magnet selection');self.resize(1020,650)
        layout=QVBoxLayout(self)
        note=QLabel('Checked BPMs are used in the fit. Checked magnets are available to scan.\n'
            'Selections are shared by COD / Dispersion / Skew. Filtering alone does not change checks.\n'
            'For a one-magnet test: click its name, then Only highlighted. Ctrl/Shift selects multiple names.')
        note.setWordWrap(True);layout.addWidget(note)
        b=window.backend;o=window.panels['orbit'].options
        self.bpms=SelectionList('BPMs',b.bpm_names,o.excluded_bpms)
        self.magnets=SelectionList('Magnets',b.hcorrectors+b.vcorrectors+b.skews,o.fixed_actuators)
        row=QHBoxLayout();layout.addLayout(row);row.addWidget(QLabel('Magnet presets:'))
        self.presets={}
        for title,names in [('H only',b.hcorrectors),('V only',b.vcorrectors),
            ('SD only',[n for n in b.skews if n.startswith('SD')]),
            ('SF only',[n for n in b.skews if n.startswith('SF')])]:
            button=QPushButton(title);button.clicked.connect(lambda _,n=names:self.magnets.choose(n))
            row.addWidget(button);self.presets[title]=button
        row=QHBoxLayout();layout.addLayout(row,1);row.addWidget(self.bpms);row.addWidget(self.magnets)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)

    def apply_to(self,window):
        for panel in window.panels.values():
            panel.options.excluded_bpms=self.bpms.excluded()
            panel.options.fixed_actuators=self.magnets.excluded()
            panel.invalidate()
