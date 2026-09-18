"""Exploratory next-day replay; does not read or write live Home Assistant.

This reuses the already inspected period, so it is NOT a new untouched test set.
All features and training targets obey the forecast issue-time cutoff.
Fixed small candidate set; no search for a winning hyperparameter combination.
"""
from baseline import load, predict, metrics
import baseline
from pathlib import Path
import argparse
from datetime import date, timedelta
import json
import math
from statistics import fmean
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import HistGradientBoostingRegressor


def observations(hist, origin, window=60):
    # Outlier labels are recalculated from past-only data at each issue time.
    from energy_system.consumption_rolling import _outliers
    pairs=sorted((d,v) for d,v in hist.items() if origin-timedelta(days=window)<=date.fromisoformat(d)<origin)
    bad=set(_outliers(pairs))
    return {date.fromisoformat(d):v for d,v in pairs if d not in bad}


def smoothing(hist, origin, target, kind):
    obs=observations(hist,origin)
    if len(obs)<14:return None
    level=next(iter(obs.values()));trend=0.;weekly=[0.]*7
    day=min(obs)
    while day<origin:
        previous=level
        predicted=level+(.9*trend if kind=='damped_holt' else 0)
        if day in obs:
            if kind=='weekly_smoothing':
                error=obs[day]-(level+weekly[day.weekday()])
                level+=.15*error;weekly[day.weekday()]+=.10*error
            else:
                level=.15*obs[day]+.85*predicted
                if kind=='damped_holt':trend=.03*(level-previous)+.97*.9*trend
        else:
            # Missing dates advance the prediction, never insert a zero sample.
            level=predicted;trend*=.9
        day+=timedelta(days=1)
    lead=(target-(origin-timedelta(days=1))).days
    return max(0,level+(sum(.9**h for h in range(1,lead+1))*trend if kind=='damped_holt' else 0)
                 +(weekly[target.weekday()] if kind=='weekly_smoothing' else 0))


def features(hist, target):
    origin=target-timedelta(days=1)
    obs=observations(hist,origin,30)
    if len(obs)<7:return None
    base=fmean(obs.values()); vals=[]
    # A next-day forecast never has a complete target-minus-one day.
    for lag in (2,3,7,14):
        value=obs.get(target-timedelta(days=lag))
        vals.extend([((value-base)/base if value is not None else 0),int(value is None)])
    recent=[v for d,v in obs.items() if d>=origin-timedelta(days=7)]
    vals.append((fmean(recent)-base)/base if recent else 0)
    vals.extend([math.sin(2*math.pi*target.weekday()/7),math.cos(2*math.pi*target.weekday()/7),int(target.weekday()>=5)])
    return vals,base


def regression(hist,origin,target,kind):
    obs=observations(hist,origin,60)
    x=[];y=[]
    for day,value in obs.items():
        # Feature construction sees the history available BEFORE that example
        # would have been forecast, not the later outer training window.
        past={d:v for d,v in hist.items() if date.fromisoformat(d)<day-timedelta(days=1)}
        feature=features(past,day)
        if feature is None:continue
        values,base=feature;x.append(values);y.append(value/base-1)
    future=features(hist,target)
    if len(x)<20 or future is None:return None
    values,base=future
    if kind=='ridge_lags':
        scaler=StandardScaler();x=scaler.fit_transform(x);values=scaler.transform([values])
        model=Ridge(alpha=20).fit(x,y)
    else:
        model=HistGradientBoostingRegressor(max_iter=60,learning_rate=.05,max_leaf_nodes=4,
            min_samples_leaf=10,l2_regularization=10,early_stopping=False,random_state=42).fit(x,y)
        values=[values]
    return max(0,base*(1+float(model.predict(values)[0])))


CANDIDATES=['current','level_smoothing','damped_holt','weekly_smoothing','ridge_lags','boosted_trees','equal_ensemble']


def run(args):
    result={}
    for source in sorted(args.data_dir.glob('*.json')):
        site=source.stem
        hist,days,invalid=load(site);hist={d:v for d,v in hist.items() if d not in invalid}
        method=json.loads(source.read_text())['baseline_method']
        rows={k:[] for k in CANDIDATES}
        for day,item in sorted(days.items()):
            target=date.fromisoformat(day)
            if target<args.start or day in invalid or item['kwh']<=0:continue
            origin=target-timedelta(days=1)
            past={d:v for d,v in hist.items() if date.fromisoformat(d)<origin}
            candidates={'current':predict(past,origin,target,method)}
            candidates.update({k:smoothing(past,origin,target,k) for k in CANDIDATES[1:4]})
            candidates.update({k:regression(past,origin,target,k) for k in CANDIDATES[4:6]})
            members=[candidates[k] for k in ('current','level_smoothing','ridge_lags')]
            candidates['equal_ensemble']=fmean(members) if all(v is not None for v in members) else None
            # Paired scoring: every candidate gets exactly the same target days.
            if any(v is None for v in candidates.values()):continue
            for k,p in candidates.items():rows[k].append(dict(date=day,pred=p,actual=item['kwh']))
        result[site]={k:{'earlier':metrics([r for r in rr if r['date']<str(args.recent_start)]),
                        'recent':metrics([r for r in rr if r['date']>=str(args.recent_start)]),
                        'all':metrics(rr),'rows':rr} for k,rr in rows.items()}
        print(site)
        for k,rs in result[site].items():
            print(k,{p:{'n':rs[p]['n'],'MAE':round(rs[p]['mae'],3),'RMSE':round(rs[p]['rmse'],3),'bias':round(rs[p]['bias'],3)} for p in ('earlier','recent','all')})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True,type=Path)
    parser.add_argument('--start',required=True,type=date.fromisoformat)
    parser.add_argument('--recent-start',required=True,type=date.fromisoformat)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    baseline.DATA_DIR=args.data_dir
    run(args)
