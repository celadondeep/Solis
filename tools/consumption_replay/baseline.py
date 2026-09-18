"""Rolling-origin replay. A tomorrow forecast only sees days ending yesterday."""
import json,sys,math
from pathlib import Path
from datetime import date,timedelta
from statistics import fmean,median
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from energy_system.consumption_rolling import rolling_daily_statistics,_outliers,rolling_hourly_statistics

# Set by the command-line runner. No Home Assistant access or credentials.
DATA_DIR=None
CUT=date(2026,9,4)


def load(site):
    if DATA_DIR is None:raise ValueError('Set --data-dir')
    data=json.loads((DATA_DIR/(site+'.json')).read_text())
    byday={h['date']:h['kwh'] for h in data['history']}
    hourly=data['hourly_days']
    for d,item in hourly.items():
        if item['kwh']>0:byday[d]=item['kwh']
    invalid=dict(data.get('invalid_days',{}))
    invalid.update({d:'zero_hourly_total' for d,item in hourly.items() if item['kwh']==0})
    return byday,hourly,invalid


def fit(hist,origin,model):
    window=30
    if model.startswith('mean14'):window=14
    elif model.startswith('mean21'):window=21
    elif model.startswith('mean42'):window=42
    samples=[(d,v) for d,v in hist.items() if origin-timedelta(days=window)<=date.fromisoformat(d)<origin and v>0]
    bad=set(_outliers(samples));samples=[(d,v) for d,v in sorted(samples) if d not in bad]
    if len(samples)<5:return None
    if model.startswith('median'):
        base=median(v for _,v in samples)
    elif model.startswith('exp'):
        half=float(model.split('_')[0][3:])
        weights=[2**(-(origin-date.fromisoformat(d)).days/half) for d,_ in samples]
        base=sum(v*w for (_,v),w in zip(samples,weights))/sum(weights)
    else:base=fmean(v for _,v in samples)
    return base,samples


def predict(hist,origin,target,model):
    result=fit(hist,origin,model)
    if result is None:return None
    base,samples=result
    wd=[v for d,v in samples if date.fromisoformat(d).weekday()==target.weekday()]
    if model=='last_week':
        return hist.get((target-timedelta(days=7)).isoformat(),base)
    if model.endswith('_wd'):
        return (sum(wd)+3*base)/(len(wd)+3)
    return base


def metrics(rows):
    if not rows:return {}
    errors=[r['pred']-r['actual'] for r in rows]
    return dict(n=len(rows),mae=fmean(abs(e) for e in errors),rmse=math.sqrt(fmean(e*e for e in errors)),bias=fmean(errors),wape=sum(abs(e) for e in errors)/sum(r['actual'] for r in rows)*100)

