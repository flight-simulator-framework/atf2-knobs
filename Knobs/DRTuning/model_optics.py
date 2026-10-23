"""RFTrack transport, extraction constraints and SR calibration generation."""
import copy
import numpy as np
from Knobs.DRCorrection.core import CorrectionService
from .model import linear_map,normal_modes,name


def partial_element(element,fraction,momentum):
    """Entrance to fractional position; do not add the original exit edge."""
    import RF_Track as rft
    length=element.get_length()*fraction;kind=type(element).__name__
    if not 0<fraction<1:raise ValueError('Interior element fraction must be in (0,1)')
    if kind=='SBend':
        e=rft.SBend(length,element.get_angle()*fraction,-momentum,element.get_E1(),0.)
        e.set_K1L(element.get_K1L()*fraction)
    elif kind=='Quadrupole':e=rft.Quadrupole(length,-momentum,element.get_K1(-momentum))
    elif kind=='Sextupole':e=rft.Sextupole(length,-momentum,element.get_K2(-momentum))
    elif kind=='Drift':e=rft.Drift(length)
    else:raise ValueError('Partial-element optics unsupported for '+kind)
    return e


def transport(model,locations=None):
    """Maps at BPMs, quad centres, kicker and SR fractional bend positions."""
    import RF_Track as rft
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _track_coordinates
    locations=locations or {'SR':('BH1R.27',.27),'SR2':('BH1R.27',.9)}
    co=model.solver.find_closed_orbit();state=co.initial_coordinates.copy();total=np.eye(4);rows={};s=0.
    for e in model.lattice['*']:
        model.check_cancel();n=e.get_name();local=rft.Lattice();local.append(e)
        interior=[(label,float(fraction)) for label,(element,fraction) in locations.items() if element==n]
        if type(e).__name__=='Quadrupole':
            interior.append((n,.5))
            nodes,weights=np.polynomial.legendre.leggauss(5)
            interior.extend((n+'$AVG'+str(i),float((x+1)/2)) for i,x in enumerate(nodes))
        for label,fraction in interior:
            part=rft.Lattice();part.append(partial_element(e,fraction,model.momentum))
            rows[label]=dict(matrix=linear_map(part,state,model.momentum)@total,s_m=s+e.get_length()*fraction)
        total=linear_map(local,state,model.momentum)@total
        state=_track_coordinates(local,state,model.momentum);s+=e.get_length()
        if type(e).__name__=='Bpm' or n.startswith('KIX'):rows[n]=dict(matrix=total.copy(),s_m=s)
    if set(locations)-set(rows):raise ValueError('SR location absent from lattice: '+str(set(locations)-set(rows)))
    tunes,modes=normal_modes(total)
    for row in rows.values():
        v=row['matrix']@modes
        row['beta_m']=[float(abs(v[0,0])**2),float(abs(v[2,1])**2)]
    for n in model.settings['quadrupoles_k1l']:
        samples=[rows[n+'$AVG'+str(i)] for i in range(5)]
        rows[n]['average_beta_m']=sum(w/2*np.asarray(r['beta_m']) for w,r in zip(weights,samples))
        rows[n]['average_beta_transport']={}
        for axis,sl in [('x',slice(0,2)),('y',slice(2,4))]:
            terms=[]
            for r in samples:
                a,b=r['matrix'][sl,sl][0];terms.append([a*a,-2*a*b,b*b])
            rows[n]['average_beta_transport'][axis]=np.asarray(weights)@np.asarray(terms)/2
        for i in range(5):del rows[n+'$AVG'+str(i)]
    return dict(rows=rows,one_turn_matrix=total,tunes=tunes,
                coupling_norm=float(max(np.max(abs(total[:2,2:])),np.max(abs(total[2:,:2])))),
                source='RFTrack transport from recorded settings; model prediction',locations=locations)


def sr_beta_calibration(model,table=None):
    table=table or transport(model)
    if table['coupling_norm']>1.e-5:
        raise ValueError('Uncoupled SR beta fit requires negligible model coupling; correct skew or supply explicit uncoupled reference optics')
    planes={}
    for axis,sl in [('x',slice(0,2)),('y',slice(2,4))]:
        def row(label,physical):
            m=table['rows'][physical]['matrix'][sl,sl]
            return dict(name=label,r11=float(m[0,0]),r12_m=float(m[0,1]))
        bpms=[row(n,name(n)) for n in model.bpm_names]
        bpms += [dict(name=n,beta_transport=table['rows'][n]['average_beta_transport'][axis]) for n in model.settings['quadrupoles_k1l']]
        planes[axis]=dict(schema='dr-sr-beta-1',source=table['source'],bpms=bpms,
                         monitors=[row(n,n) for n in table['locations']])
    return dict(planes=planes)


def phase_calibration(model,table=None):
    table=table or transport(model)
    if table['coupling_norm']>1.e-5:raise ValueError('Three-BPM phase beta requires an uncoupled reference model')
    planes={}
    for k,(axis,sl) in enumerate([('x',slice(0,2)),('y',slice(2,4))]):
        rows=[];names=model.bpm_names
        for i in range(len(names)-2):
            a=table['rows'][name(names[i])]['matrix'][sl,sl];candidates=[]
            for n in names[i+1:i+9]:
                m=table['rows'][name(n)]['matrix'][sl,sl]@np.linalg.inv(a)
                if abs(m[0,1])>.1:candidates.append((n,float(m[0,0]/m[0,1])))
            if len(candidates)<2:continue
            _,u,v=max((abs(u[1]-v[1]),u,v) for j,u in enumerate(candidates) for v in candidates[j+1:])
            rows.append(dict(names=[names[i],u[0],v[0]],m11_m12=[u[1],v[1]],phase_sign=1 if table['tunes'][k]<.5 else -1))
        planes[axis]=rows
    return dict(schema='dr-phase-optics-1',source=table['source'],planes=planes)


def positions(model,locations,momentum):
    """Closed-orbit coordinates at exact exported element fractions."""
    import RF_Track as rft
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _track_coordinates
    co=model.solver.find_closed_orbit(momentum_mev_c=momentum);state=co.initial_coordinates.copy();rows={}
    for e in model.lattice['*']:
        model.check_cancel();n=e.get_name()
        for label,(element,fraction) in locations.items():
            if n!=element:continue
            if fraction==0:rows[label]=state.copy()
            elif fraction<1:
                part=rft.Lattice();part.append(partial_element(e,fraction,model.momentum))
                rows[label]=_track_coordinates(part,state,momentum)
        local=rft.Lattice();local.append(e);state=_track_coordinates(local,state,momentum)
        for label,(element,fraction) in locations.items():
            if n==element and fraction==1:rows[label]=state.copy()
    if set(rows)!=set(locations):raise ValueError('Model locations missing: '+str(set(locations)-set(rows)))
    return np.array([rows[n] for n in locations])


def dispersion_at(model,locations,step=1.e-4):
    return (positions(model,locations,model.momentum*(1+step))-positions(model,locations,model.momentum*(1-step)))/(2*step)


def regional_targets(data,model):
    """Historical SAD straight-section target convention, explicit opt-in."""
    def region(n):
        s=model.elements[name(n)].get_S('exit')
        return any(model.elements[a].get_S('exit')<=s<=model.elements[b].get_S('exit') for a,b in [('MB19R','MB31R'),('MB66R','MB80R')])
    straight=np.array([region(n) for n in data['names']]);target=np.asarray(data['target']).copy()
    orbit=model.orbit()['x']
    if data['mode']=='dispersion':
        target[0]=np.where(straight,0.,model.target_dispersion('x'))
        target[1]=np.where(straight,orbit,0.)
    else:target[0]=np.where(straight,orbit,0.)
    return target


def extraction_constraints(model,mode,opt,data,cancel):
    """Keep predicted extraction changes small via weighted additional rows.

    Soft constraints as in the legacy large-weight fits, not exact equalities.
    Both position and slope at KIX are retained in physical mm/mrad units.
    """
    kickers=[n for n in model.elements if n=='KIX' or n.startswith('KIX.')]
    if not kickers:raise ValueError('No extraction kicker in model')
    locations={n:(n,1.) for n in kickers+['MB19R','MB20R','MB21R']};index=0 if opt.plane=='x' else 2
    labels=[]
    if opt.model_fix_extraction_orbit:labels += [n+' '+quantity for n in locations for quantity in ('position change [mm]','slope change [mrad]')]
    if mode=='dispersion' and opt.model_fix_extraction_dispersion:labels += [n+' '+quantity for n in locations for quantity in ('dispersion change [mm/delta]','dispersion slope change [mrad/delta]')]
    def signal():
        values=[]
        if opt.model_fix_extraction_orbit:values.extend(positions(model,locations,model.momentum)[:,index:index+2].reshape(-1))
        if mode=='dispersion' and opt.model_fix_extraction_dispersion:values.extend(dispersion_at(model,locations)[:,index:index+2].reshape(-1))
        return np.array(values)
    columns=[];probe=opt.skew_probe if mode=='skew' else opt.corrector_probe_A
    for n,center in zip(data['actuators'],data['centers']):
        cancel();p=model.actuators[n];lo=max(p['limits'][0],center-probe);hi=min(p['limits'][1],center+probe)
        try:
            model.write(data['kind'],[n],[lo]);minus=signal()
            model.write(data['kind'],[n],[hi]);plus=signal()
        finally:model.write(data['kind'],[n],[center])
        columns.append((plus-minus)/(hi-lo))
    return dict(matrix=np.column_stack(columns),residual=np.zeros(len(labels)),weight=float(opt.model_extraction_weight),
                labels=labels,locations=locations,method='soft model constraint; preserve predicted extraction position/slope',verified_on_machine=False)


def sr_dispersion_calibration(model,locations=None):
    """Local upstream-steerer basis, truncated to two independent modes/plane.

    Matching identical reduced bases at BPM/SR points reproduces the linear
    transport fit without a rank-deficient many-steerer fit to three BPMs.
    """
    locations=locations or {'SR':('BH1R.27',.27),'SR2':('BH1R.27',.9)}
    selected=[n for n in model.bpm_names if name(n) in ('MB64R','MB65R','MB66R')]
    if len(selected)<3:raise ValueError('SR dispersion fit needs MB64R, MB65R and MB66R')
    at={n:(name(n),1.) for n in selected};at.update(locations)
    nominal=dispersion_at(model,at);planes={}
    smin=min(model.elements[name(n)].get_S('exit') for n in selected)
    for axis,index,names in [('x',0,model.hcorrectors),('y',2,model.vcorrectors)]:
        columns=[];used=[]
        for n in names:
            if model.elements[name(n)].get_S('exit')>=smin:continue
            center=model.values[n];lo=max(center-.01,model.actuators[n]['limits'][0]);hi=min(center+.01,model.actuators[n]['limits'][1])
            if hi<=lo:continue
            try:
                model.write('correctors',[n],[lo]);minus=dispersion_at(model,at)[:,index]
                model.write('correctors',[n],[hi]);plus=dispersion_at(model,at)[:,index]
            finally:model.write('correctors',[n],[center])
            columns.append((plus-minus)/(hi-lo));used.append(n)
        if not columns:raise ValueError('No upstream steerer basis for SR '+axis)
        a=np.column_stack(columns);u,s,vt=np.linalg.svd(a[:len(selected)],full_matrices=False)
        keep=np.flatnonzero(s>max(s[0]*1.e-5,1.e-12))[:2]
        if len(keep)<2:raise ValueError('SR dispersion model basis has fewer than two independent modes')
        basis=a@vt[keep].T
        planes[axis]=dict(bpm_response=basis[:len(selected)],monitor_response=basis[len(selected):],
                          nominal_bpm=nominal[:len(selected),index],nominal_monitor=nominal[len(selected):,index],
                          basis_actuators=used,basis_projection=vt[keep].T)
    table=transport(model,locations)
    return dict(schema='dr-sr-response-1',source='Independent RFTrack local upstream-steerer dispersion basis',names=selected,
                monitors=[dict(name=n,s_m=table['rows'][n]['s_m']) for n in locations],planes=planes)
