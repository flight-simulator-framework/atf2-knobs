"""Local bump geometry from one linearization of the recorded magnet model.

Closed-orbit responses solve (I-M) z = kick; no repeated whole-ring tracking
is used to form columns. Element Jacobians are supplied by RFTrack.
"""
import itertools
import numpy as np
from scipy.optimize import LinearConstraint,linprog,minimize
from .model import linear_map,normal_modes,name
from .model_optics import partial_element


def constrained_step(a,b,e,target,lower,upper,ridge=1.e-5):
    """Bounded least squares with exact observable constraints, no clipping."""
    a,b,e,target,lower,upper=map(lambda v:np.asarray(v,float),(a,b,e,target,lower,upper))
    if (not all(np.isfinite(v).all() for v in (a,b,e,target,lower,upper)) or
            np.any(lower>=upper) or np.linalg.matrix_rank(e)<len(target)):
        raise ValueError('Nonfinite, rank-deficient or infeasible bump response')
    norms=np.linalg.norm(e,axis=1);en=e/norms[:,None];tn=target/norms
    feasible=linprog(np.zeros(len(lower)),A_eq=en,b_eq=tn,bounds=list(zip(lower,upper)),method='highs')
    if not feasible.success:raise ValueError('Bump target cannot be reached within current limits')
    scale=max(float(np.linalg.norm(a,2)),1.e-12);a=a/scale;b=b/scale
    h=a.T@a+ridge**2*np.eye(len(lower));g=a.T@b
    fit=minimize(lambda x:.5*x@h@x-g@x,feasible.x,jac=lambda x:h@x-g,
                 bounds=list(zip(lower,upper)),constraints=LinearConstraint(en,tn,tn),
                 method='SLSQP',options=dict(ftol=1.e-12,maxiter=300))
    if not fit.success or np.max(abs(e@fit.x-target))>1.e-6 or np.any(fit.x<lower-1.e-9) or np.any(fit.x>upper+1.e-9):
        raise ValueError('Constrained bump solve failed: '+fit.message)
    return fit.x


def transfer_table(model,target):
    import RF_Track as rft
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _track_coordinates
    co=model.solver.find_closed_orbit();state=co.initial_coordinates.copy()
    total=np.eye(4);s=0.;rows={};kicks={}
    correctors={name(n):(n,p) for n,p in model.actuators.items() if p['kind'] in ('vcorrector','hcorrector')}
    for element in model.lattice['*']:
        model.check_cancel();n=element.get_name();local=rft.Lattice();local.append(element)
        if n==target:
            part=rft.Lattice();part.append(partial_element(element,.5,model.momentum))
            rows['target']=dict(matrix=linear_map(part,state,model.momentum)@total,s_m=s+element.get_length()/2)
        total=linear_map(local,state,model.momentum)@total
        if n in correctors:
            label,p=correctors[n];original=model.solver._get_corrector_kick(element);samples=[]
            delta=np.zeros(2);delta[int(p['kind']=='vcorrector')]=p['scale']*.001
            try:
                for sign in (-1,1):
                    model.solver._set_corrector_kick(element,original+sign*delta)
                    probe=rft.Lattice();probe.append(element)
                    samples.append(_track_coordinates(probe,state,model.momentum))
            finally:model.solver._set_corrector_kick(element,original)
            # Calibrate the element's affine kick in the tracking coordinate
            # convention (including electron sign and finite element length).
            kicks[label]=(samples[1]-samples[0])/.002
        state=_track_coordinates(local,state,model.momentum);s+=element.get_length()
        rows[n]=dict(matrix=total.copy(),s_m=s)
    if 'target' not in rows:raise ValueError('SD/SF target absent from model: '+target)
    normal_modes(total)
    if np.linalg.cond(np.eye(4)-total)>1.e10:raise ValueError('Periodic orbit response is ill-conditioned')
    return dict(rows=rows,one_turn_matrix=total,circumference_m=s,kicks_per_A=kicks,
                source='Recorded-settings RFTrack element Jacobians; periodic matrix solve')


def periodic_response(table,bpms,correctors,actuators):
    rows=table['rows'];m=table['one_turn_matrix'];columns=[]
    for n in correctors:
        source=rows[name(n)];kick=np.asarray(table['kicks_per_A'][n])
        at_start=np.linalg.solve(source['matrix'],kick)
        periodic=np.linalg.solve(np.eye(4)-m,m@at_start)
        values=[]
        for bpm in bpms:
            row=rows[name(bpm)];z=row['matrix']@periodic
            if row['s_m']>=source['s_m']:z+=row['matrix']@at_start
            values.append(z[[0,2]])
        columns.append(np.asarray(values).T.ravel())
    return np.column_stack(columns)


def select_geometry(model,target,valid,*,excluded=(),fixed=(),amplitude_mm=.4,max_step_A=1.):
    """Choose local correctors around an observable, kick-free BPM interval.

    Two H and two V correctors on each side allow four-dimensional closure.
    The target displacement is reconstructed from two internal BPMs; it is
    model-calibrated, not a direct position measurement at the sextupole.
    """
    table=transfer_table(model,name(target));rows=table['rows'];length=table['circumference_m']
    st=rows['target']['s_m'];ft=rows['target']['matrix'];m=table['one_turn_matrix']
    def relative(n):return (rows[name(n)]['s_m']-st+length/2)%length-length/2
    enabled=[i for i,n in enumerate(model.bpm_names) if valid[i] and n not in excluded]
    candidates=sorted(enabled,key=lambda i:abs(relative(model.bpm_names[i])))[:16]
    pairs=sorted(itertools.combinations(candidates,2),key=lambda ij:sum(abs(relative(model.bpm_names[i])) for i in ij))
    failures=[]
    for pair in pairs:
        model.check_cancel();ends=sorted(relative(model.bpm_names[i]) for i in pair)
        if ends[0]*ends[1]>=0:continue  # observe the target from both sides
        selected=[]
        for family in (model.vcorrectors,model.hcorrectors):
            upstream=sorted((n for n in family if n not in fixed and relative(n)<ends[0]),key=relative,reverse=True)
            downstream=sorted((n for n in family if n not in fixed and relative(n)>ends[1]),key=relative)
            if len(upstream)<2 or len(downstream)<2:break
            selected+=upstream[:2]+downstream[:2]
        if len(selected)!=8:continue
        lo,hi=min(map(relative,selected)),max(map(relative,selected))
        if hi-lo>length/2:continue
        outside=np.array([i for i in enabled if not lo<=relative(model.bpm_names[i])<=hi],int)
        if len(outside)<4:continue
        transforms=[]
        for i in pair:
            row=rows[name(model.bpm_names[i])];turn=round((st+relative(model.bpm_names[i])-row['s_m'])/length)
            transforms.append((row['matrix']@np.linalg.matrix_power(m,turn)@np.linalg.inv(ft))[[0,2]])
        observer=np.concatenate(transforms)
        if np.linalg.cond(observer)>1.e4:continue
        n=len(model.bpm_names);internal_rows=np.array([pair[0],pair[0]+n,pair[1],pair[1]+n])
        reconstruction=np.zeros((4,2*n));reconstruction[:,internal_rows]=np.linalg.inv(observer)
        response=periodic_response(table,model.bpm_names,selected,model.actuators)
        outrows=np.r_[outside,outside+n];centers=model.read('correctors',selected)
        lower,upper=model.limits('correctors',selected)
        # Feasible for both +amplitude and -amplitude about the same baseline.
        margin=np.minimum(max_step_A,np.minimum(centers-lower,upper-centers))/amplitude_mm
        try:
            knob=constrained_step(response[outrows],np.zeros(len(outrows)),reconstruction@response,
                                  [0,0,1,0],-margin,margin)
            if np.max(abs(response[outrows]@knob))*amplitude_mm>.01:continue
        except ValueError as exc:
            failures.append(str(exc));continue
        return dict(target=name(target),actuators=selected,bpm_names=list(model.bpm_names),
                    positions_m=model.positions_m.copy(),outside_indices=outside,outside_rows=outrows,
                    internal_indices=list(pair),internal_bpms=[model.bpm_names[i] for i in pair],
                    reconstruction=reconstruction,model_response=response,model_knob_A_per_mm=knob,
                    centers=centers,limits=[lower,upper],source=table['source'],support_m=[lo,hi],
                    target_position_note='mm inferred from internal BPMs and model transport; angle fixed to zero',
                    model_settings=model.settings)
    raise ValueError('No observable, bounded local bump geometry: need two healthy BPMs around the target and two H/V correctors on each side. '+('; '.join(failures[-2:])))
