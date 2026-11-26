"""Interactive measured dispersion and orbit plots shared by both dispersion tools."""
from datetime import datetime
from pathlib import Path

import numpy as np
from matplotlib.backend_bases import MouseButton
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QToolTip

from Knobs.DRCorrection.gui import Plot
from Knobs.DRCorrection.assessment import rms, improvement_pct
from .dispersion import arc_bpms, straight_bpms


OP_TEMPFILES_DIRECTORY = Path('/mnt/nas1/atf-data/op/tempfiles')


def save_dispersion_png(panel, stem):
    """Export the completed plot independently of the correction and its data archive."""
    destination = panel.tempfiles_directory / f'{stem}_{datetime.now():%Y%m%d_%H%M%S_%f}.png'
    figure = panel.plot.figure
    previous_size = figure.get_size_inches().copy()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        figure.set_size_inches(10, 7, forward=False)
        panel.plot.save(destination)
    except Exception as exc:
        message = f'Dispersion PNG export failed: {destination} ({exc})'
        panel.window.log(message)
        panel.window.statusBar().showMessage(message, 15000)
    else:
        panel.window.log(f'Saved dispersion plot: {destination}')
    finally:
        figure.set_size_inches(previous_size, forward=False)
        panel.plot.canvas.draw_idle()


def dispersion_summary(data,plane,valid,baseline):
    """Compare each measurement with its own correction target on identical BPMs."""
    axis=0 if plane=='x' else 1
    names=data['names']
    baseline=np.asarray(baseline,float)
    opts=data.get('options',{})
    target=np.zeros_like(baseline)
    if plane=='x':
        regions=[('Straight ηx',straight_bpms(names))]
        if opts.get('arc_weight',0)>0:
            parity=opts.get('arc_parity','odd')
            regions.append((f'Arc ηx ({parity})',arc_bpms(names,parity)))
    else:
        if 'target' in data:target=np.asarray(data['target'][0],float)
        elif 'full_target' in data:target=np.asarray(data['full_target'][axis],float)
        label='ηy − target' if np.any(target[valid]!=0) else 'ηy'
        regions=[(label,np.ones(len(names),bool))]

    curves={'Measured':baseline}
    if 'full_predicted' in data:curves['Predicted']=np.asarray(data['full_predicted'][axis],float)
    elif plane=='y' and 'predicted' in data:curves['Predicted']=np.asarray(data['predicted'][0],float)
    if 'full_after' in data:
        if data.get('restored'):after='Trial (restored)'
        elif data.get('error'):after='Trial'
        else:after='Measured after'
        curves[after]=np.asarray(data['full_after'][axis],float)
        valid=valid & np.asarray(data['after_valid'],bool)
    common=valid & np.isfinite(target)
    for curve in curves.values():common &= np.isfinite(curve)
    lines=[]
    for label,region in regions:
        mask=common & region
        if not mask.any():
            lines.append(f'{label} RMS · no common valid BPMs')
            continue
        values={stage:rms((curve-target)[mask]) for stage,curve in curves.items()}
        stages=[f'{stage} {value:.3g}' for stage,value in values.items()]
        if len(values)>1:
            stages[0]=f"Before {values['Measured']:.3g}"
            last=list(values)[-1]
            start=values['Measured'];end=values[last];difference=start-end
            if difference:
                direction='decrease' if difference>0 else 'increase'
                pct=improvement_pct(start,end)
                change=f'{direction} {abs(difference):.3g}'
                if pct is not None:change+=f' ({abs(pct):.1f}%)'
            else:change='unchanged'
            stages[-1]+=f' · {change}'
        lines.append(f'{label} RMS · {mask.sum()} BPMs · mm / (dp/p)\n' + '   |   '.join(stages))
    return '\n'.join(lines)


class DispersionPlot(Plot):
    dispersion_plane = 'y'
    bpm_clicked = pyqtSignal(str)
    save_selection = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.data = None
        self.excluded = frozenset()
        self.points = {}
        self.hovered = None
        self.canvas.toolbar.addSeparator()
        action = self.canvas.toolbar.addAction('Save BPM selection')
        action.setToolTip('Overwrite the BPM selection for the next session on this interface')
        action.triggered.connect(lambda: self.save_selection.emit())
        self.canvas.mpl_connect('motion_notify_event', self.hover)
        self.canvas.mpl_connect('figure_leave_event', self.hide_tip)
        self.canvas.mpl_connect('button_press_event', self.click)

    def set_excluded(self, names):
        names = frozenset(names)
        if self.excluded != names:
            self.excluded = names
            if self.data is not None:
                self.correction(self.data, preserve_view=True)

    def clear_data(self):
        self.hide_tip()
        self.points.clear()
        self.data = None
        self.figure.clear()
        self.canvas.draw_idle()

    def correction(self, data, preserve_view=False):
        limits = [(ax.get_xlim(), ax.get_ylim()) for ax in self.figure.axes] if preserve_view else []
        self.clear_data()
        self.data = data
        self.figure.set_layout_engine('constrained')
        axes = self.figure.subplots(2, 2).ravel()
        before = data.get('bpm_observations', data)
        names = np.asarray(before['names'])
        positions = np.asarray(before['positions_m'], float)
        indices = {name: i for i, name in enumerate(names)}
        rows = np.array([indices[n] for n in data['names']])
        excluded = np.isin(names, list(self.excluded))
        valid = np.asarray(data.get('acquisition_valid', data.get('scan_state', {}).get('common_valid', data['valid'])), bool)
        valid = valid & np.asarray(data.get('iteration_valid', True), bool)
        observed = valid if before is data else np.asarray(before['valid'], bool)
        known = bool(observed.any() and np.isfinite(positions[observed]).all())
        x = positions if known else np.arange(1, len(names) + 1)
        valid = valid & observed[rows]
        order = np.argsort(x, kind='stable')
        active = order[~excluded[order]]
        curve_order = np.argsort(x[rows], kind='stable')
        curve_rows = curve_order[~excluded[rows][curve_order]]
        for i, ax in enumerate(axes):
            values = np.asarray(before['full_measured'][i], float)
            title = data['full_signal_labels'][i]
            unit = 'mm / (dp/p)' if i < 2 else 'mm'
            ax.plot(x[active], np.where(observed, values, np.nan)[active], 'k.-',
                    linewidth=.65, markersize=4, label='Measured before')
            visible = observed & np.isfinite(values)
            grey = excluded & visible
            if grey.any():
                ax.plot(x[grey], values[grey], '.', color='0.65', label='Excluded', zorder=4)
            self.points[ax] = (names[visible], np.column_stack((x[visible], values[visible])),
                               positions[visible], title, unit)
            for key, label, style in [('full_predicted','Predicted','C2--'),
                                      ('full_after','Measured after SET','C3-'),
                                      ('full_target','Target',':')]:
                if key in data:
                    curve = np.asarray(data[key][i], float)
                elif key == 'full_predicted' and data.get('reference_matrix') and 'predicted' in data and i in (1, 3):
                    curve = np.asarray(data['predicted'][i//2], float)
                elif key == 'full_target' and 'target' in data and i in (1, 3):
                    curve = np.asarray(data['target'][i//2], float)
                else:
                    continue
                mask = valid
                if key == 'full_after':
                    mask = valid & np.asarray(data['after_valid'], bool)
                    if data.get('restored'):
                        label = 'After SET (since restored)'
                ax.plot(x[rows][curve_rows], np.where(mask, curve, np.nan)[curve_rows], style,
                        label=label, **({'color':'gray'} if key == 'full_target' else {}))
            ax.set(xlabel='s [m]' if known else 'BPM index (measurement order)',
                   ylabel='η [mm / (dp/p)]' if i < 2 else 'COD [mm]', title=title)
            ax.grid(alpha=.2)
            if limits:
                ax.set_xlim(limits[i][0])
                ax.set_ylim(limits[i][1])
        axis=0 if self.dispersion_plane=='x' else 1
        baseline=np.asarray(before['full_measured'][axis],float)[rows]
        summary=dispersion_summary(data,self.dispersion_plane,valid & ~excluded[rows],baseline)
        self.figure.supxlabel(summary,fontsize=9)
        entries = {}
        for ax in axes:
            handles, labels = ax.get_legend_handles_labels()
            entries.update(zip(labels, handles))
        self.figure.legend(list(entries.values()), list(entries), loc='outside upper center', ncol=3, fontsize=8)
        self.canvas.draw_idle()

    def hit(self, event):
        if event.inaxes not in self.points or self.canvas.toolbar.mode:
            return None
        names, coordinates, positions, title, unit = self.points[event.inaxes]
        if not len(names):
            return None
        pixels = event.inaxes.transData.transform(coordinates)
        dist = np.sum((pixels - [event.x, event.y])**2, axis=1)
        i = int(dist.argmin())
        if dist[i] > (7 * self.canvas.device_pixel_ratio)**2:
            return None
        x, y = coordinates[i]
        position = f's = {positions[i]:.4f} m' if np.isfinite(positions[i]) else f'BPM index = {x:g}'
        name = str(names[i])
        status = 'Excluded · click to include' if name in self.excluded else 'Included · click to exclude'
        return name, f'{name}\n{position}\n{title} = {y:.5g} {unit}\n{status}'

    def hide_tip(self, event=None):
        if self.hovered is not None:
            QToolTip.hideText()
            self.hovered = None

    def hover(self, event):
        point = self.hit(event)
        if point is None:
            self.hide_tip()
        elif self.hovered != point:
            self.hovered = point
            QToolTip.showText(QCursor.pos(), point[1], self.canvas)

    def click(self, event):
        if event.button == MouseButton.LEFT and not event.dblclick:
            point = self.hit(event)
            if point is not None:
                self.bpm_clicked.emit(point[0])
