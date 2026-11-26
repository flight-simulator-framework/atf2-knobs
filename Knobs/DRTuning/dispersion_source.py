"""Provenance of measured response matrices and current dispersion observations."""
from datetime import datetime
from pathlib import Path
import re


QM_FILES='QM trim / main response (qm_scan_*.json qm_dispersion_*.json qm_dispersion.json qm_scan.checkpoint.json)'
ZV_FILES='Vertical ZV response (vertical_dispersion*.json dispersion.json)'


def local_time(stamp):
    if not stamp:return 'measurement time unknown'
    try:return datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError,ValueError):return 'measurement time unknown'


def measured_time(data):
    stamp=data.get('matrix_measured_at') or (data.get('response_origin') or {}).get('measured_at') or data.get('created_at')
    if stamp:return stamp
    # Legacy ZV files sometimes still contain IOC timestamps for the probe endpoints.
    for frame in reversed(data.get('orbit_measurements',[])):
        if not frame.get('freshness_verified'):continue
        for shot in reversed(frame.get('timestamps',[])):
            if isinstance(shot,dict):
                epochs=[v for v in shot.values() if isinstance(v,(float,int)) and 1.e9<v<1.e10]
                if epochs:return datetime.fromtimestamp(max(epochs)).astimezone().isoformat()
    return None


def matrix_caption(data,kind,path=None,observation=None):
    text=f'{kind} · {local_time(measured_time(data))}' if data else 'No matrix selected'
    if path:text+='\n'+Path(path).name
    if observation:text+='\nBeam measured '+local_time(observation['measured_at'])
    return text


def observation_from_result(data):
    applied=data.get('applied',False)
    state=data.get('applied_baseline') if applied else data.get('baseline')
    if state is None:return None
    options=data['options']
    return dict(baseline=state,names=data['names'],positions_m=data['positions_m'],full_signal_labels=data['full_signal_labels'],
                full_measured=data['full_after' if applied else 'full_measured'],
                measured=data.get('after' if applied else 'measured'),
                valid=data['after_valid'] if applied else data.get('baseline_valid',data.get('acquisition_valid',data['valid'])),
                frames=data.get('after_orbit_measurements' if applied else 'baseline_orbit_measurements',[]),
                frequency_change_khz=options['frequency_change_khz'],momentum_compaction=options['momentum_compaction'],
                tunes=data.get('after_tunes' if applied else 'initial_tunes'),
                evidence=data.get('after_tune_evidence' if applied else 'initial_tune_evidence'),
                measured_at=(data.get('applied_at') if applied else data.get('baseline_measured_at')) or datetime.now().astimezone().isoformat())


def latest_matrices(directories, readers, check_cancel, progress):
    """Find the latest usable measurement for each tool. Inspect data off the GUI thread."""
    folders=set()
    for directory in map(Path,directories):
        folders.add(directory.resolve())
        root=directory.parent if re.fullmatch(r'\d{4}',directory.name) else directory
        folders.update(p.resolve() for p in root.glob('[0-9][0-9][0-9][0-9]') if p.is_dir())
    files=[]
    for folder in folders:
        check_cancel()
        for path in folder.glob('*.json'):
            name=path.name
            if name.startswith(('qm_scan','qm_dispersion')):kind='qm'
            elif name.startswith('vertical_dispersion') or name=='dispersion.json':kind='vertical'
            elif name.startswith('bump_response_'):kind='bump'
            else:continue
            try:mtime=path.stat().st_mtime
            except OSError:continue
            match=re.search(r'_(\d{8}_\d{6})(?:_(\d{6}))?',name)
            file_time=mtime
            if match:
                try:file_time=datetime.strptime(match[1]+(match[2] or '000000'),'%Y%m%d_%H%M%S%f').timestamp()
                except ValueError:pass
            files.append((max(file_time,mtime),path,kind,file_time))

    latest={};times={}
    for index,(written,path,kind,file_time) in enumerate(sorted(files,reverse=True)):
        check_cancel()
        keys=('trim','main') if kind=='qm' else (kind,)
        if all(times.get(key,float('-inf'))>written for key in keys):continue
        if kind=='qm':
            coil=re.match(r'qm_(?:scan|dispersion)_(trim|main)_',path.name)
            if coil and times.get(coil[1],float('-inf'))>written:continue
        progress(int(100*index/len(files)),f'Checking {path.name}')
        try:
            data=readers[kind](path)
            key=data['acquisition_options'].get('coil','trim') if kind=='qm' else kind
            stamp=measured_time(data)
            when=(float(data['measured_unix_s']) if kind=='bump' else
                  datetime.fromisoformat(stamp).timestamp() if stamp else file_time)
        except (OSError,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError):
            continue
        if when>times.get(key,float('-inf')):
            times[key]=when
            latest[key]=(data,path)
    return latest
