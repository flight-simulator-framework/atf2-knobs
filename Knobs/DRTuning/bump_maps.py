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


def transfer_table(model,target,*,other_targets=()):
    import RF_Track as rft
    from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_twiss import _track_coordinates
    co=model.solver.find_closed_orbit();state=co.initial_coordinates.copy()
    total=np.eye(4);s=0.;rows={};kicks={};target_rows={}
    targets={name(n) for n in (target,*other_targets)};target=name(target)
    correctors={name(n):(n,p) for n,p in model.actuators.items() if p['kind'] in ('vcorrector','hcorrector')}
    for element in model.lattice['*']:
        model.check_cancel();n=element.get_name();local=rft.Lattice();local.append(element)
        if n in targets:
            part=rft.Lattice();part.append(partial_element(element,.5,model.momentum))
            target_rows[n]=dict(matrix=linear_map(part,state,model.momentum)@total,s_m=s+element.get_length()/2)
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
    if target not in target_rows:raise ValueError('SD/SF target absent from model: '+target)
    rows['target']=target_rows[target]
    normal_modes(total)
    if np.linalg.cond(np.eye(4)-total)>1.e10:raise ValueError('Periodic orbit response is ill-conditioned')
    return dict(rows=rows,target_rows=target_rows,one_turn_matrix=total,circumference_m=s,kicks_per_A=kicks,
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


class BumpGeometryUnavailable(ValueError):
    """Healthy acquisition, but no usable local geometry within the bounds."""


def select_geometry(model,target,valid,*,excluded=(),fixed=(),amplitude_mm=.4,max_step_A=1.,preferred=None,table=None,min_width_mm=0.,outside_max_mm=.01):
    """Choose local correctors around an observable, kick-free BPM interval.

    Two H and two V correctors on each side allow four-dimensional closure.
    The target displacement is reconstructed from two internal BPMs; it is
    model-calibrated, not a direct position measurement at the sextupole.
    """
    table=transfer_table(model,name(target)) if table is None else table
    rows=table['rows'];length=table['circumference_m']
    if 'target_rows' in table:
        if name(target) not in table['target_rows']:raise BumpGeometryUnavailable('SD/SF target absent from model: '+name(target))
        rows=dict(rows,target=table['target_rows'][name(target)])
    st=rows['target']['s_m'];ft=rows['target']['matrix'];m=table['one_turn_matrix']
    def relative(n):return (rows[name(n)]['s_m']-st+length/2)%length-length/2
    enabled=[i for i,n in enumerate(model.bpm_names) if valid[i] and n not in excluded]
    candidates=sorted(enabled,key=lambda i:abs(relative(model.bpm_names[i])))[:16]
    pairs=sorted(itertools.combinations(candidates,2),key=lambda ij:sum(abs(relative(model.bpm_names[i])) for i in ij))
    failures=[];preset_rejection=None
    def evaluate(pair,selected):
        model.check_cancel();ends=sorted(relative(model.bpm_names[i]) for i in pair)
        if ends[0]*ends[1]>=0:raise BumpGeometryUnavailable('BPMs do not bracket the target')
        if len(set(selected))!=8:raise BumpGeometryUnavailable('Need eight distinct local correctors')
        for family in (model.vcorrectors,model.hcorrectors):
            family_selected=[n for n in selected if n in family]
            if (sum(relative(n)<ends[0] for n in family_selected)!=2 or
                    sum(relative(n)>ends[1] for n in family_selected)!=2):
                raise BumpGeometryUnavailable('Need two H/V correctors on each side of the BPM interval')
        lo,hi=min(map(relative,selected)),max(map(relative,selected))
        if hi-lo>length/2:raise BumpGeometryUnavailable('Corrector support exceeds half the ring')
        outside=np.array([i for i in enabled if not lo<=relative(model.bpm_names[i])<=hi],int)
        if len(outside)<4:raise BumpGeometryUnavailable('Insufficient healthy outside BPMs (need at least four)')
        transforms=[]
        for i in pair:
            row=rows[name(model.bpm_names[i])];turn=round((st+relative(model.bpm_names[i])-row['s_m'])/length)
            transforms.append((row['matrix']@np.linalg.matrix_power(m,turn)@np.linalg.inv(ft))[[0,2]])
        observer=np.concatenate(transforms)
        if np.linalg.cond(observer)>1.e4:raise BumpGeometryUnavailable('BPM pair cannot resolve target position and angle')
        n=len(model.bpm_names);internal_rows=np.array([pair[0],pair[0]+n,pair[1],pair[1]+n])
        reconstruction=np.zeros((4,2*n));reconstruction[:,internal_rows]=np.linalg.inv(observer)
        response=periodic_response(table,model.bpm_names,selected,model.actuators)
        outrows=np.r_[outside,outside+n];centers=model.read('correctors',selected)
        lower,upper=model.limits('correctors',selected)
        # Feasible for both +amplitude and -amplitude about the same baseline.
        margin=np.minimum(max_step_A,np.minimum(centers-lower,upper-centers))/amplitude_mm
        if np.any(margin<=0):raise BumpGeometryUnavailable('Insufficient bidirectional corrector current margin')
        try:
            knob=constrained_step(response[outrows],np.zeros(len(outrows)),reconstruction@response,
                                  [0,0,1,0],-margin,margin)
            if np.max(abs(response[outrows]@knob))*amplitude_mm>outside_max_mm:
                raise BumpGeometryUnavailable(f'Predicted outside COD exceeds {outside_max_mm:g} mm')
        except ValueError as exc:
            raise BumpGeometryUnavailable(str(exc)) from exc
        predicted_range=None
        if min_width_mm>0:
            # Find the displacement interval permitted by current bounds and
            # linear outside-COD closure, allowing different shapes at each end.
            direction=np.array([0.,0.,1.,0.])
            equality=np.column_stack((reconstruction@response,-direction))
            leakage=np.column_stack((response[outrows],np.zeros(len(outrows))))
            bounds=list(zip(np.maximum(lower-centers,-max_step_A),np.minimum(upper-centers,max_step_A)))+[(None,None)]
            objective=np.r_[np.zeros(len(selected)),1.];ends_feasible=[]
            for sign in (1.,-1.):
                fit=linprog(sign*objective,A_eq=equality,b_eq=np.zeros(4),
                    A_ub=np.r_[leakage,-leakage],b_ub=np.full(2*len(outrows),outside_max_mm),bounds=bounds,method='highs')
                if not fit.success:raise BumpGeometryUnavailable('Cannot determine a bounded scan interval from model/current limits')
                ends_feasible.append(float(fit.x[-1]))
            predicted_range=ends_feasible
            width=predicted_range[1]-predicted_range[0]
            if width<min_width_mm-1.e-8:
                raise BumpGeometryUnavailable(f'Insufficient current budget: model scan width {width:.4g} mm < minimum {min_width_mm:.4g} mm')
        return dict(target=name(target),actuators=selected,bpm_names=list(model.bpm_names),
                    positions_m=model.positions_m.copy(),outside_indices=outside,outside_rows=outrows,
                    internal_indices=list(pair),internal_bpms=[model.bpm_names[i] for i in pair],
                    reconstruction=reconstruction,model_response=response,model_knob_A_per_mm=knob,
                    centers=centers,limits=[lower,upper],source=table['source'],support_m=[lo,hi],
                    target_position_note='mm inferred from internal BPMs and model transport; angle fixed to zero',
                    model_settings=model.settings,predicted_range_mm=predicted_range)
    if preferred is not None:
        bpm_map={name(n):i for i,n in enumerate(model.bpm_names)}
        actuator_map={name(n):n for n in model.hcorrectors+model.vcorrectors}
        try:
            pair=[bpm_map[n] for n in preferred['internal_bpms']]
            selected=[actuator_map[n] for n in preferred['actuators']]
            if len(set(pair))!=2 or any(i not in enabled for i in pair):
                raise BumpGeometryUnavailable('Preset BPMs missing, excluded or invalid')
            if any(n in fixed for n in selected):raise BumpGeometryUnavailable('Preset corrector excluded/fixed')
            result=evaluate(pair,selected)
        except KeyError as exc:preset_rejection='Preset device absent from model: '+str(exc)
        except BumpGeometryUnavailable as exc:preset_rejection=str(exc)
        else:
            result['selection']=dict(method='preset',preset_id=preferred.get('id'))
            return result
    for pair in pairs:
        model.check_cancel();ends=sorted(relative(model.bpm_names[i]) for i in pair)
        if ends[0]*ends[1]>=0:continue
        selected=[]
        for family in (model.vcorrectors,model.hcorrectors):
            upstream=sorted((n for n in family if n not in fixed and relative(n)<ends[0]),key=relative,reverse=True)
            downstream=sorted((n for n in family if n not in fixed and relative(n)>ends[1]),key=relative)
            if len(upstream)<2 or len(downstream)<2:break
            selected+=upstream[:2]+downstream[:2]
        if len(selected)!=8:continue
        try:result=evaluate(pair,selected)
        except BumpGeometryUnavailable as exc:failures.append(str(exc));continue
        result['selection']=dict(method='automatic',preset_rejection=preset_rejection)
        return result
    reason='; '.join(dict.fromkeys(([preset_rejection] if preset_rejection else [])+failures[-2:]))
    raise BumpGeometryUnavailable('No observable, bounded local bump geometry: need two healthy BPMs around the target and two H/V correctors on each side. '+reason)
