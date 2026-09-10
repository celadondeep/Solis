"""SE dashboard v2: shared presentation, no services and no control decisions.

The only custom dependencies are already installed button-card and ApexCharts.
Horizon is read as a forecast; the committed plan is the action source of truth.
"""
from copy import deepcopy

VERSION = "2.1"
COLORS = {"pv": "#d99a18", "load": "#4789e8", "battery": "#159b85", "grid": "#8973cf"}

EXTRA = {
    "Namai": {
        "horizon": "sensor.solis_energy_horizon",
        "power": "switch.solis_s6_eh3p_power_state",
        "heartbeat": "sensor.solis_s6_eh3p_last_modbus_success",
        "grid_sign": 1, "battery_sign": 1, "heartbeat_max_age": 180,
        "url": "namai-se-perziura", "other_url": "eimo-se-perziura", "other_name": "Eimo SE",
        "bank": "sensor.eso_bankas_likutis", "bank_official": "sensor.eso_bankas_220588",
        "bank_value": "sensor.eso_bankas_verte", "bank_import": "sensor.eso_ivado_importas",
        "bank_export": "sensor.eso_ivado_eksportas",
    },
    "Eimo": {
        "horizon": "sensor.eimo_energy_horizon", "power": None,
        "heartbeat": "sensor.solis_inverter_1033300254190112_solis_timestamp_measurements_received", "heartbeat_max_age": 900,
        "grid_sign": -1, "battery_sign": -1,
        "url": "eimo-se-perziura", "other_url": "namai-se-perziura", "other_name": "Namai SE",
        "bank": "sensor.eso_bankas_likutis_eimo", "bank_official": "sensor.eso_bankas_266493",
        "bank_value": "sensor.eso_bankas_verte_eimo", "bank_import": "sensor.eso_ivado_importas_eimo",
        "bank_export": "sensor.eso_ivado_eksportas_eimo",
    },
}

# Scoped inside button-card's shadow root. Uses HA colors in both theme modes.
PANEL_CSS = """
.se{font-family:var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif);text-align:left;color:var(--primary-text-color);font-size:14px;line-height:1.55;white-space:normal}
.se *{box-sizing:border-box}.se h2,.se h3,.se p{margin:0}.se h2{font-size:23px;line-height:1.3;font-weight:650;letter-spacing:-.4px}.se h3{font-size:19px;font-weight:600;line-height:1.4}
.se .eyebrow{font-size:10px;letter-spacing:1.7px;text-transform:uppercase;font-weight:650;color:var(--secondary-text-color);margin-bottom:9px}
.se .muted{font-size:12px;color:var(--secondary-text-color)}.se .desc{font-size:14px;color:var(--secondary-text-color);margin-top:9px;max-width:850px}
.se .top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.se .pill{display:inline-flex;gap:6px;align-items:center;white-space:nowrap;border-radius:20px;padding:4px 10px;font-size:11px;font-weight:600;color:var(--primary-text-color);background:var(--secondary-background-color)}
.se .dot{width:7px;height:7px;border-radius:50%;background:#159b85;flex-shrink:0}.se .warn{background:#d99a18}.se .bad{background:#d96b57}
.se .flow{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}.se .node{padding:13px 14px;border:1px solid var(--divider-color);border-radius:14px;min-width:0}
.se .node-head{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--secondary-text-color)}.se ha-icon{--mdc-icon-size:19px;width:19px;height:19px}.se .value{font-size:27px;line-height:1.2;font-weight:600;letter-spacing:-.7px;font-variant-numeric:tabular-nums;margin:6px 0 2px}.se .unit{font-size:13px;font-weight:400;letter-spacing:0}
.se .bat-motion{animation:se-battery-pulse 1.8s ease-in-out infinite}.se .bat-discharge{color:#d99a18!important}@keyframes se-battery-pulse{50%{opacity:.4}}@media(prefers-reduced-motion:reduce){.se .bat-motion{animation:none}}
.se .battery{margin-top:20px}.se .battery-value{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}.se .track{height:7px;margin-top:10px;border-radius:10px;background:var(--secondary-background-color);overflow:hidden}.se .fill{height:100%;border-radius:10px;background:#159b85}
.se .steps{margin-top:18px;border-left:2px solid var(--divider-color);margin-left:5px;padding-left:17px}.se .step{position:relative;display:grid;grid-template-columns:66px 1fr;gap:9px;padding:0 0 16px}.se .step:last-child{padding-bottom:0}.se .step:before{position:absolute;left:-23px;top:7px;content:'';width:8px;height:8px;border-radius:50%;background:#159b85;border:2px solid var(--ha-card-background,var(--card-background-color));box-sizing:content-box}.se .time{font-size:17px;font-weight:650;line-height:1.35;font-variant-numeric:tabular-nums}.se .step-title{font-size:13px;font-weight:600}
.se .rows{margin-top:15px}.se .row{display:flex;justify-content:space-between;gap:16px;border-top:1px solid var(--divider-color);padding:10px 0;font-size:13px}.se .row:last-child{padding-bottom:0}.se .row span:first-child{color:var(--secondary-text-color)}.se .row strong{text-align:right;font-weight:550;font-variant-numeric:tabular-nums}
.se .numbers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:19px}.se .number .value{font-size:26px}.se .callout{padding:11px 13px;border-left:3px solid #d99a18;background:var(--secondary-background-color);border-radius:0 8px 8px 0;font-size:12px;margin-top:16px}.se .foot{margin-top:16px;padding-top:12px;border-top:1px solid var(--divider-color)}
@media(max-width:450px){.se h2{font-size:20px}.se .flow{gap:8px}.se .node{padding:11px}.se .value{font-size:24px}}
"""

PANEL_JS = r"""
const p = variables.plant || {};
const a = id => states[id]?.attributes || {};
const s = id => states[id]?.state;
const num = v => v !== null && v !== undefined && String(v).trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v,d=1) => num(v) === null ? '—' : new Intl.NumberFormat('lt-LT',{maximumFractionDigits:d,minimumFractionDigits:d}).format(Number(v));
const on = v => v === true || v === 'on' || v === 'true';
const h = a(p.horizon), plan = a(p.plan), health = a(p.telemetry);
const ts = v => v && Number.isFinite(Date.parse(v)) ? Date.parse(v) : null;
const now = Date.now();
const expiry = ts(h.expires_at), calculated = ts(h.calculated_at);
const forecastOK = on(h.valid) && expiry !== null && expiry > now && calculated !== null && calculated <= now + 60000;
const planTime = ts(plan.committed_at);
const planFresh = planTime !== null && now - planTime < 600000 && planTime <= now + 60000;
const hbRaw=s(p.heartbeat);
const hb = num(hbRaw)!==null ? Number(hbRaw)*1000 : ts(hbRaw);
const teleFresh = s(p.telemetry) === 'ok' && (!p.heartbeat || (hb !== null && now - hb >= -60000 && now - hb < (p.heartbeat_max_age || 180)*1000));
const actionable = on(plan.actionable) && planFresh && teleFresh;
const clock = v => ts(v) === null ? '—' : new Date(v).toLocaleTimeString('lt-LT',{timeZone:'Europe/Vilnius',hour:'2-digit',minute:'2-digit'});
const date = v => ts(v) === null ? '' : new Date(v).toLocaleDateString('lt-LT',{timeZone:'Europe/Vilnius',month:'2-digit',day:'2-digit'});
const row = (label,value) => `<div class="row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
const title = (kicker,heading) => `<div class="eyebrow">${esc(kicker)}</div><h3>${esc(heading)}</h3>`;
const pill = (text,kind='') => `<span class="pill"><span class="dot ${kind}"></span>${esc(text)}</span>`;
const figure = (label,value,unit) => `<div class="number"><div class="muted">${esc(label)}</div><div class="value">${esc(value)} <span class="unit">${esc(unit)}</span></div></div>`;
let body='';
if(variables.kind === 'status') {
  let heading='Energija namams', detail='Saulė pirmiausia dengia vartojimą, tada įkrauna bateriją; likutis atiduodamas į tinklą.', badge='Automatika veikia', dot='';
  const priority=String(plan.priority || '');
  if(priority === 'horizon_pv_export') {heading='Saulė namams, tinklui ir baterijai';detail='Eksportuojamas saulės perteklius, kad baterijoje liktų vietos vėlesnei gamybai.';}
  else if(priority === 'horizon_night' && !on(plan.inverter_on)) {heading='Inverteris ilsisi iki ryto';detail=forecastOK ? `Įjungimas ${clock(h.wake_at)}. Baterijos energija saugoma; nakties vartojimą dengia tinklas.` : 'Laukiama naujo patikimo ryto plano.';}
  else if(on(plan.slot_active)) {heading='Atlaisvinama vieta saulei';detail=`Iškrovimas vyksta iki ${fmt(plan.slot_cutoff_soc,0)} % baterijos ribos.`;}
  else if(priority.includes('storm') || on(s(p.storm))) {heading='Kaupiamas energijos rezervas';detail='Aktyvus audros arba planuojamo elektros atjungimo rezervas.';badge='Rezervo režimas';dot='warn';}
  else if(priority.includes('manual') || on(s(p.manual)) || on(s(p.manual_soc))) {heading='Aktyvus rankinis valdymas';detail='Automatinį modelio pasiūlymą pakeičia rankiniai nustatymai.';badge='Rankinis režimas';dot='warn';}
  else if(priority === 'horizon_night') {heading='Baterija ruošiama rytui';detail='Inverteris įjungtas pagal artimiausios gamybos ir baterijos vietos planą.';}
  if(!actionable) {heading='Laukiama patikimo valdymo plano';detail=!teleFresh ? 'Matavimai vėluoja. Paskutinio plano nelaikome patvirtintu dabartiniu veiksmu.' : 'Valdymas pristabdytas arba planą reikia atnaujinti.';badge='Reikia dėmesio';dot='warn';}
  else if(s(p.executor)!=='ok') {badge='Tikrinamas įvykdymas';dot='warn';detail+=' Inverteris dar nepatvirtino visų plano nustatymų.';}
  body=`<div class="top"><div class="eyebrow" style="margin:0">${esc(p.site_label)} · DABAR</div>${pill(badge,dot)}</div><h2 style="margin-top:13px">${esc(heading)}</h2><p class="desc">${esc(detail)}</p>`;
  const qualityState=s(p.quality?.state);
  if(['critical','warning'].includes(qualityState)) body+=`<div class="foot muted">Tinklo kokybės perspėjimas · <a style="color:var(--primary-color)" href="/${esc(p.url)}/analize">Peržiūrėti analizę</a></div>`;
}
if(variables.kind === 'flow') {
  const read=id=>teleFresh ? num(s(id)) : null;
  const pv=read(p.pv), load=read(p.load), grid=read(p.grid), battery=read(p.battery), soc=read(p.soc);
  const g=grid===null ? null : grid*p.grid_sign, b=battery===null ? null : battery*p.battery_sign;
  const power=v=>v===null ? '—' : fmt(Math.abs(v)/1000,2);
  const node=(label,value,sub,icon,color,cls='')=>`<div class="node"><div class="node-head"><ha-icon class="${cls}" icon="${icon}" style="color:${color}"></ha-icon>${esc(label)}</div><div class="value">${power(value)} <span class="unit">kW</span></div><div class="muted">${esc(sub)}</div></div>`;
  const level=soc===null?null:Math.max(0,Math.min(100,Math.round(soc/10)*10));
  const batteryIcon=level===null?'mdi:battery-unknown':b!==null&&b < -20
    ? (level===0?'mdi:battery-charging-outline':'mdi:battery-charging-'+level)
    : level===100?'mdi:battery':level===0?'mdi:battery-outline':'mdi:battery-'+level;
  const batteryMotion=b!==null&&Math.abs(b)>20?'bat-motion'+(b>20?' bat-discharge':''):'';
  const field=health.fields?.pv_power || health.fields?.soc || {};
  const report=hb ?? ts(field.reported_at), age=report===null ? null : Math.max(0,Math.floor((now-report)/1000));
  const ageText=age===null ? 'Laikas nežinomas' : age<60 ? `prieš ${age} s` : `prieš ${Math.floor(age/60)} min`;
  body=title('Momentinė galia','Kur keliauja energija')+`<div class="flow">`+
    node('Saulė',pv,pv===null?'Nėra šviežių duomenų':pv>20?'Gamina':'Negamina','mdi:weather-sunny','#d99a18')+
    node('Namai',load,load===null?'Nėra šviežių duomenų':'Dabartinis vartojimas','mdi:home-lightning-bolt-outline','#4789e8')+
    node('Baterija',b,b===null?'Nėra šviežių duomenų':b < -20?'← Kraunasi':b>20?'→ Iškrauna':'Ramybė',batteryIcon,'#159b85',batteryMotion)+
    node('Tinklas',g,g===null?'Nėra šviežių duomenų':g>20?'← Imama iš tinklo':g< -20?'→ Atiduodama į tinklą':'Beveik subalansuota','mdi:transmission-tower','#8973cf')+`</div>`+
    `<div class="battery"><div class="top"><span class="muted">Baterijos įkrova</span><span class="battery-value">${fmt(soc,0)} %</span></div><div class="track"><div class="fill" style="width:${soc===null?0:Math.max(0,Math.min(100,soc))}%"></div></div></div>`+
    `<div class="foot muted">${teleFresh ? 'Matavimai' : 'Duomenys vėluoja'} · ${esc(ageText)}${p.power ? ' · Inverteris '+(s(p.power)==='off'?'išjungtas':s(p.power)==='on'?'įjungtas':'nežinoma') : ' · SolisCloud'}</div>`;
}
if(variables.kind === 'plan') {
  const night=forecastOK && on(h.night_active);
  body=title('Artimiausi veiksmai',night?'Pasiruošimas rytui':'Dienos planas');
  if(!forecastOK) body+='<p class="desc">Patikimos prognozės šiuo metu nėra. Ryto iškrovimo laikas nerodomas, kol modelis neperskaičiuotas.</p>';
  else if(night) {
    const implemented=actionable && plan.priority==='horizon_night';
    const amount=num(h.required_preexport_kwh);
    body+=`<p class="desc">${implemented ? 'Galiojantis planas' : 'Modelio siūlymas; aktyvus valdymas jį pakeičia'} · ${esc(date(h.pv_start_at))}</p>`;
    const step=(time,name,sub)=>`<div class="step"><div class="time">${esc(time)}</div><div><div class="step-title">${esc(name)}</div><div class="muted">${esc(sub)}</div></div></div>`;
    body+='<div class="steps">';
    const scheduled=[];
    if(p.power) scheduled.push([h.wake_at,'Įjungti inverterį','Pagal iškrovimo arba gamybos pradžią']);
    if(amount!==null && amount>0.02) scheduled.push([h.discharge_start_at,'Pradėti iškrovimą',`${fmt(amount)} kWh baterijos energijos`]);
    else body+=num(h.unmet_headroom_kwh)>0.05 ? step('—','Iškrovimas nesuplanuotas','Turimi apribojimai neleidžia atlaisvinti visos vietos') : step('—','Papildomai iškrauti nereikia','Turimos vietos prognozuojamai saulei pakanka');
    scheduled.sort((x,y)=>(ts(x[0])??Infinity)-(ts(y[0])??Infinity)).forEach(x=>{body+=step(clock(x[0]),x[1],x[2]);});
    body+=step(clock(h.pv_start_at),'Prasideda prognozuojama gamyba',`Baterijos viršutinė ryto riba ${fmt(h.target_soc,0)} %`);
    body+='</div><div class="rows">'+row('Reikiama laisva vieta',fmt(h.required_headroom_kwh)+' kWh')+row('Sutaupoma inverterio sąnaudų',fmt(h.standby_saved_kwh,2)+' kWh')+'</div>';
    if(num(h.unmet_headroom_kwh)>0.05) body+=`<div class="callout">Iki gamybos pradžios gali nepavykti atlaisvinti dar ${fmt(h.unmet_headroom_kwh)} kWh.</div>`;
    body+='<div class="foot muted">Ryto riba nereiškia, kad mažesnę įkrovą reikia papildyti iš tinklo.</div>';
  } else {
    const pvFirst=plan.priority==='horizon_pv_export';
    const override=String(plan.priority||'').includes('manual') || String(plan.priority||'').includes('storm');
    const description=!actionable ? 'Paskutinio plano vykdymas šiuo metu nepatvirtintas.' : override ? 'Galioja rankinis arba rezervo planas. Modelio prognozė rodoma atskirai.' : pvFirst ? 'Vartojimas → eksportas iki leistinos ribos → baterija.' : 'Vartojimas → baterijos įkrovimas → pertekliaus eksportas.';
    body+=`<p class="desc">${description}</p>`;
    body+='<div class="rows">'+row('Eksporto riba',fmt(h.model_export_limit_kw)+' kW')+row('Priverstinis iškrovimas',on(plan.slot_active)?'Aktyvus':'Išjungtas')+row('Plano SOC riba',fmt(plan.target_soc,0)+' %')+'</div>';
    body+='<div class="callout">Kitas nakties planas bus parodytas, kai modelis pereis į pasiruošimą rytui. Praėjusios nakties laikai nerodomi.</div>';
    body+=`<div class="foot muted">Planas atnaujintas ${esc(clock(plan.committed_at))}. Koreguojamas pagal gamybą ir vartojimą.</div>`;
  }
}
if(variables.kind === 'forecast') {
  body=title('Žvilgsnis į priekį','Gamyba ir vartojimas');
  if(!forecastOK) body+='<p class="desc">Laukiama šviežios prognozės. Pasenę skaičiai nerodomi.</p>';
  else {
    const night=on(h.night_active);
    body+=`<p class="desc">${night ? 'Artimiausiai saulės dienai' : 'Nuo skaičiavimo momento iki rytojaus pabaigos'} · ${night?esc(date(h.pv_start_at)):fmt(h.horizon_hours,0)+' val.'}</p>`;
    body+='<div class="numbers">'+figure('Saulės energija',fmt(night?h.day_pv_kwh:h.pv_kwh),'kWh')+figure('Numatomas vartojimas',fmt(night?h.day_load_kwh:h.load_kwh),'kWh')+'</div>';
    const pct=v=>num(v)===null?'—':(v>=1?'+':'')+fmt((v-1)*100,0)+' %';
    body+='<div class="rows">'+row('Gamybos korekcija',pct(h.pv_adjustment))+row('Vartojimo korekcija',pct(h.consumption_adjustment))+'</div>';
    body+='<div class="foot muted">Korekcijos rodo, kiek modelis pakėlė ar sumažino prognozę pagal faktinius duomenis.</div>';
  }
}
if(variables.kind === 'quality') {
  const q=p.quality, qa=a(q.state), state=s(q.state);
  const names={ok:'Tinklas stabilus',watch:'Verta stebėti',warning:'Reikia dėmesio',critical:'Reikia patikrinti',stale:'Matavimai pasenę',unknown:'Laukiama matavimų'};
  const valid=['ok','watch','warning','critical'].includes(state);
  body=`<div class="top"><div class="eyebrow" style="margin:0">Tinklo kokybė</div>${pill(valid ? (state==='ok'?'Stebėsena veikia':'Yra nukrypimų'):'Trūksta duomenų',state==='ok'?'':'warn')}</div><h3 style="margin-top:13px">${esc(names[state]||'Laukiama matavimų')}</h3>`;
  body+='<div class="numbers">'+figure('Fazių įtampų skirtumas',fmt(valid?s(q.spread):null,2),'%')+figure('Galios faktorius',fmt(valid?s(q.pf):null,3),'PF')+'</div>';
  body+='<div class="rows">'+row('Įtampos ribos',valid?fmt(s(q.voltage_min),1)+'–'+fmt(s(q.voltage_max),1)+' V':'—')+row('Reaktyvioji galia',fmt(valid?s(q.reactive):null,0)+' var')+'</div>';
  if(valid && Array.isArray(qa.issues_lt) && qa.issues_lt.length) body+=`<div class="callout">${esc(qa.issues_lt.filter(x=>typeof x==='string').slice(0,2).join(' · '))}</div>`;
  body+='<div class="foot muted">Vertinami fazinių įtampų dydžiai. Tinklo ir inverterio išėjimo disbalansas yra skirtingi rodikliai.</div>';
}
if(variables.kind === 'profile') {
  const profile=a(p.consumption_profile), arr=profile.hourly_kwh;
  const valid=Array.isArray(arr)&&arr.length===24&&arr.every(v=>num(v)!==null);
  const peak=valid?arr.indexOf(Math.max(...arr)):null;
  body=title('Vartojimo įpročiai','Kada namams reikia energijos');
  body+='<div class="numbers">'+figure('Paros vidurkis',fmt(profile.daily_avg),'kWh')+figure('Didžiausia tipinė apkrova',peak===null?'—':String(peak).padStart(2,'0')+':00','')+'</div>';
  body+='<div class="rows">'+row('Rytojaus prognozė',fmt(s(p.cons_tomorrow))+' kWh')+row('Modeliui naudota istorijos',fmt(profile.usable_days,0)+' d.')+'</div>';
  body+='<div class="foot muted">Valandų ir savaitės dienų grafikai rodo išmoktus vidurkius, ne šiandienos skaitiklių rodmenis.</div>';
}
if(variables.kind === 'payback') {
  const inv=num(s('input_number.pv_investicija')), grant=num(s('input_number.pv_parama'));
  const valid=inv!==null&&grant!==null&&inv>grant;
  body=title('Bendras abiejų elektrinių rezultatas','Investicija ir grąža');
  body+='<div class="numbers">'+figure('Sukaupta nauda',fmt(s('sensor.pv_sutaupyta_viso'),0),'€')+figure('Gryna investicija',valid?fmt(inv-grant,0):'—','€')+'</div>';
  if(!valid) body+='<div class="callout">Investicijos suma turi būti didesnė už atskaitomą paramą. Patikslinkite įvestis žemiau — atsipirkimo procentas ir terminas kol kas nerodomi.</div>';
  else body+='<div class="rows">'+row('Atsipirko',fmt(s('sensor.pv_atsipirkes_procentais'),1)+' %')+row('Liko padengti',fmt(s('sensor.pv_liko_iki_atsipirkimo'),0)+' €')+'</div>';
  body+='<div class="foot muted">Abiejuose dashboarduose rodoma ta pati bendra investicija. Grafiko kreivės yra modeliuojamos, o ne sukauptos istorijos faktas.</div>';
}
return `<style>__CSS__</style><div class="se">${body}</div>`;
""".replace("__CSS__", PANEL_CSS)


def button_templates():
    return {
        "se_panel": {
            "section_mode": True,
            "show_name": False, "show_icon": False, "show_state": False,
            "tap_action": {"action": "none"}, "hold_action": {"action": "none"},
            "update_timer": "15s",
            "styles": {
                "card": [{"padding": "20px"}, {"border-radius": "18px"}, {"box-shadow": "none"}, {"height": "100%"}],
                "grid": [{"grid-template-areas": '"body"'}, {"grid-template-columns": "1fr"}],
                "custom_fields": {"body": [{"width": "100%"}, {"min-width": "0"}]},
            },
            "custom_fields": {"body": "[[[" + PANEL_JS + "]]]"},
        },
        "se_metric": {
            "section_mode": True,
            "show_name": True, "show_icon": True, "show_state": True,
            "tap_action": {"action": "more-info"},
            "state_display": """[[[
              const raw=variables.attribute ? entity?.attributes?.[variables.attribute] : entity?.state;
              const n=raw!==null && raw!==undefined && String(raw).trim()!=='' ? Number(raw) : NaN;
              if(!Number.isFinite(n)) return '—';
              return new Intl.NumberFormat('lt-LT',{maximumFractionDigits:variables.decimals ?? 1}).format(n)+' '+(variables.unit ?? entity?.attributes?.unit_of_measurement ?? '');
            ]]]""",
            "styles": {
                "card": [{"padding": "16px"}, {"border-radius": "16px"}, {"box-shadow": "none"}],
                "grid": [{"grid-template-areas": '"i n" "s s"'}, {"grid-template-columns": "24px 1fr"}, {"row-gap": "12px"}],
                "img_cell": [{"justify-self": "start"}, {"width": "20px"}],
                "icon": [{"width": "20px"}, {"color": "[[[ return variables.color || 'var(--primary-color)'; ]]]"}],
                "name": [{"justify-self": "start"}, {"text-align": "left"}, {"font-size": "12px"}, {"color": "var(--secondary-text-color)"}, {"white-space": "normal"}],
                "state": [{"justify-self": "start"}, {"font-size": "25px"}, {"font-weight": "550"}, {"font-variant-numeric": "tabular-nums"}],
            },
        },
    }


def _panel(p, kind):
    keys = ("site_label", "pv", "load", "grid", "battery", "soc", "plan", "telemetry", "executor", "storm", "manual", "manual_soc", "consumption_profile", "cons_tomorrow")
    cfg = {k: p.get(k) for k in keys}
    cfg.update(EXTRA[p["site_label"]])
    cfg["quality"] = p["analysis"]["quality"]
    return {"type": "custom:button-card", "template": "se_panel", "update_timer": "5s" if kind == "flow" else "15s",
            "variables": {"plant": cfg, "kind": kind}, "grid_options": {"columns": "full", "rows": "auto"}}


def _metric(entity, name, icon, color, *, attribute=None, unit=None, decimals=1, columns=6):
    return {"type": "custom:button-card", "template": "se_metric", "entity": entity, "name": name, "icon": icon,
            "variables": {"color": color, "attribute": attribute, "unit": unit, "decimals": decimals},
            "grid_options": {"columns": columns, "rows": 2}}


def _heading(title, icon="mdi:chart-box-outline"):
    return {"type": "heading", "heading": title, "icon": icon, "heading_style": "subtitle", "grid_options": {"columns": "full"}}


def _section(*cards, span=1, visible=None):
    section = {"type": "grid", "cards": list(cards)}
    if span > 1:
        section["column_span"] = span
    if visible:
        section["visibility"] = [{"condition": "state", "entity": visible, "state": "on"}]
    return section


def _entities(title, rows):
    return {"type": "entities", "title": title, "show_header_toggle": False, "entities": [r for r in rows if r],
            "grid_options": {"columns": "full", "rows": "auto"}}


def _row(entity, name):
    return {"entity": entity, "name": name} if entity else None


def _note(content):
    return {"type": "markdown", "content": content, "text_only": True, "grid_options": {"columns": "full", "rows": "auto"}}


def _chart(card, *, title=None, half=False):
    card = deepcopy(card)
    if title:
        if "header" in card:
            card["header"]["title"] = title
        else:
            card["title"] = title
    card["grid_options"] = {"columns": 12 if half else "full", "rows": "auto"}
    for item in card.get("entities", []):
        if isinstance(item, dict):
            item["name"] = {"Pirkta": "Iš tinklo", "Parduota": "Į tinklą"}.get(item.get("name"), item.get("name", ""))
    if card.get("type") == "custom:apexcharts-card":
        for series in card.get("series", []):
            if series.get("name") in ("Saulė", "Suvartota", "Parduota", "Pirkta"):
                original = series["name"]
                series["color"] = {"Saulė": COLORS["pv"], "Suvartota": COLORS["load"], "Parduota": COLORS["grid"], "Pirkta": "#7c8799"}[original]
                series["name"] = {"Pirkta": "Iš tinklo", "Parduota": "Į tinklą"}.get(original, original)
        apex = card.setdefault("apex_config", {})
        apex.setdefault("chart", {}).update(height=260, animations={"enabled": False}, toolbar={"show": False})
        title_text = card.get("header", {}).get("title", "").lower()
        if any(word in title_text for word in ("fazi", "fazė", "reaktyv", "įtamp")):
            apex["stroke"] = {"width": 1, "curve": "straight"}
            apex["markers"] = {"size": 0}
            for series in card.get("series", []):
                series.update(type="line", stroke_width=1)
        apex.setdefault("legend", {}).update(position="bottom", fontSize="11px")
        apex.setdefault("dataLabels", {})["enabled"] = False
        apex.setdefault("xaxis", {}).setdefault("labels", {})["datetimeUTC"] = False
        card.setdefault("update_interval", "5min")
        if card.get("header", {}).get("title") == "Baterijos round-trip efektyvumas (30 d.)":
            card["yaxis"][0]["min"] = "~80"
        if card.get("header", {}).get("title") == "PCC fazinė įtampa (7 d.)":
            card["yaxis"][0].update(min="~200", max="~255")
    return card


def _view(p, title, path, icon, subtitle, sections):
    e = EXTRA[p["site_label"]]
    return {"title": title, "path": path, "icon": icon, "type": "sections", "max_columns": 2,
            "dense_section_placement": False,
            "header": {"layout": "start", "badges_position": "bottom", "card": {
                "type": "markdown", "text_only": True,
                "content": f"# {p['title']} · {title}\n{subtitle}",
            }},
            "badges": [{"type": "entity", "entity": p["soc"], "name": "Baterija", "show_name": True, "color": "teal"},
                       {"type": "entity", "entity": p["pv"], "name": e["other_name"], "show_name": True, "show_state": False,
                        "icon": "mdi:swap-horizontal", "tap_action": {"action": "navigate", "navigation_path": f"/{e['other_url']}/{path}"}}],
            "sections": sections}


def _history(p):
    return _chart({"type": "custom:apexcharts-card", "header": {"show": True, "title": "Saulė, vartojimas ir baterija · 24 val."},
        "graph_span": "24h", "update_interval": "2min",
        "yaxis": [{"id": "kw", "min": 0, "decimals": 1}, {"id": "soc", "opposite": True, "min": 0, "max": 100, "decimals": 0}],
        "series": [
            {"entity": p["pv"], "name": "Saulė · kW", "yaxis_id": "kw", "color": COLORS["pv"], "type": "area", "opacity": .15,
             "transform": "return x === null ? null : Number(x) / 1000;", "stroke_width": 2, "group_by": {"func": "avg", "duration": "5min"}},
            {"entity": p["load"], "name": "Namai · kW", "yaxis_id": "kw", "color": COLORS["load"],
             "transform": "return x === null ? null : Number(x) / 1000;", "stroke_width": 2, "group_by": {"func": "avg", "duration": "5min"}},
            {"entity": p["soc"], "name": "Baterija · %", "yaxis_id": "soc", "color": COLORS["battery"],
             "stroke_width": 2, "group_by": {"func": "last", "duration": "5min"}},
        ]})


def _bank_graph(p):
    return _chart({"type": "custom:apexcharts-card", "header": {"show": True, "title": "ESO bankas · uždarytų mėnesių faktas"},
        "graph_span": "1y", "span": {"start": "year", "offset": "+3month"}, "update_interval": "1h",
        "yaxis": [{"min": 0, "decimals": 0}], "series": [{"entity": EXTRA[p["site_label"]]["bank_official"],
            "name": "Likutis · kWh", "type": "column", "color": COLORS["battery"], "extend_to": False,
            "data_generator": """const s=entity.attributes.serija || {};
const now=new Date(), bankStart=(now.getMonth()>=3?now.getFullYear():now.getFullYear()-1)+'-04';
const current=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0');
return Object.keys(s).sort().filter(k=>k>=bankStart&&k<current&&Number.isFinite(Number(s[k]))).map(k=>{const [y,m]=k.split('-').map(Number);return [new Date(y,m,1).getTime(),Number(s[k])];});"""}]})


def _preferences(p):
    names = [("forecast", "Prognozės", "mdi:weather-partly-cloudy"), ("graphs", "Grafikai", "mdi:chart-line"),
             ("diagnostics", "Diagnostika", "mdi:stethoscope"), ("control", "Valdymas", "mdi:tune"), ("boiler", "Boileris", "mdi:water-boiler")]
    return _section(_heading("Rodyti daugiau", "mdi:view-dashboard-outline"), *[
        {"type": "tile", "entity": p["toggles"][key], "name": label, "icon": icon, "hide_state": True,
         "color": "teal", "tap_action": {"action": "toggle"}, "icon_tap_action": {"action": "toggle"},
         "grid_options": {"columns": 6, "rows": 1}}
        for key, label, icon in names], span=2)


def build_views(p, legacy):
    """Reuse selected historical cards; own the new layout in one place."""
    a, t, e = p["analysis"], p["toggles"], EXTRA[p["site_label"]]
    today = a["today"]
    energy = _view(p, "Energija", "energija", "mdi:lightning-bolt", "Dabartinė būsena ir artimiausias planas.", [
        _section(_panel(p, "status"), span=2),
        _section(_panel(p, "flow")), _section(_panel(p, "plan")),
        _section(_heading("Šiandien", "mdi:calendar-today"),
                 _metric(a["daily_balance"][0][0], "Pagaminta", "mdi:weather-sunny", COLORS["pv"]),
                 _metric(today["consumption"], "Suvartota", "mdi:home-lightning-bolt", COLORS["load"]),
                 _metric(today["export"], "Į tinklą", "mdi:transmission-tower-import", COLORS["grid"]),
                 _metric(today["import"], "Iš tinklo", "mdi:transmission-tower-export", "#7c8799"), span=2),
        _section(_history(p), span=2, visible=t["graphs"]),
        _section(_panel(p, "forecast"), visible=t["forecast"]),
        _section(_heading("ESO pasaugojimo bankas", "mdi:bank-outline"),
                 _metric(e["bank"], "Dabartinis likutis", "mdi:bank-outline", COLORS["battery"]),
                 _metric(e["bank_value"], "Likučio vertė", "mdi:cash", COLORS["battery"], decimals=0),
                 _note("Likutis apima einamojo laikotarpio srautus. Oficialūs uždarytų mėnesių duomenys — „Vartojime“.")),
        _preferences(p),
        _section(_heading("Rankinės parinktys", "mdi:tune"), _entities("Šios elektrinės valdymas", [
            _row(p["storm"], "Audros / ESO rezervas"), _row(p["manual"], "Rankinis iškrovimo valdymas"),
            _row(p["manual_soc"], "Rankinis SOC valdymas")]), visible=t["control"]),
        _section(_entities("Boileris", [_row(p["boiler"], "Dabartinis sprendimas")]), visible=t["boiler"]),
    ])
    # These graphs existed before the redesign; preserve metrics and entity IDs.
    analysis = _view(p, "Analizė", "analize", "mdi:chart-box-outline", "Rezultatai, prognozių tikslumas ir įrangos būklė.", [
        _section(_heading("Energijos rezultatas", "mdi:chart-bar"),
                 _chart(legacy["today"]), _chart(legacy["daily"])),
        _section(_panel(p, "quality"), _chart(legacy["voltage"])),
        _section(_heading("Baterija", "mdi:battery-heart-outline"), _chart(legacy["battery"]), _chart(legacy["battery_efficiency"])),
        _section(_heading("Inverteris", "mdi:solar-power-variant-outline"), _chart(legacy["inverter"]), _chart(legacy["temperature"])),
        _section(_heading("Prognozės ir mokymasis", "mdi:weather-partly-cloudy"), _panel(p, "forecast"), _chart(legacy["accuracy"]), visible=t["forecast"]),
        _section(_heading("Nuostoliai ir efektyvumas", "mdi:chart-donut"), _chart(legacy["efficiency"]), _chart(legacy["cable"]), visible=t["diagnostics"]),
        _section(_heading("Tinklo kokybės detalės", "mdi:sine-wave"), _chart(legacy["phase"], half=True), _chart(legacy["quality"], half=True),
                 _chart(legacy["reactive"], half=True), _chart(legacy["spread"], half=True), span=2, visible=t["diagnostics"]),
        _section(_heading("Valdymo diagnostika", "mdi:stethoscope"), _entities("Plano vykdymas", [
            _row(p["planner"], "Planuotojas"), _row(p["executor"], "Vykdymas"), _row(p["telemetry"], "Duomenys"),
            _row(p["shadow"], "Plano ir inverterio atitiktis"), _row(p["core"], "Branduolys"), _row(p["coordinator"], "Koordinatorius"),
            _row(p["plan"], "Galiojantis planas"), _row(p["shadow_plan"], "Kontrolinis planas"), _row(p["shadow_cmd"], "Kontrolinės komandos")]), span=2, visible=t["diagnostics"]),
    ])
    # Keep the financial input controls and existing model curves together.
    payback_graph = _chart(legacy["payback_graph"], title="Modeliuojamas atsipirkimas · abi elektrinės")
    payback_graph["visibility"] = [{"condition": "numeric_state", "entity": "sensor.pv_gryna_investicija", "above": 0}]
    payback = _view(p, "Atsipirkimas", "atsipirkimas", "mdi:cash-clock", "Bendra Namų ir Eimo investicija, nauda ir prielaidos.", [
        _section(_panel(p, "payback"), span=2),
        _section(payback_graph, span=2),
        _section(_heading("Skaičiavimo prielaidos", "mdi:calculator-variant-outline"), _chart(legacy["payback_inputs"])),
        _section(_heading("Pagaminta nuo įrengimo", "mdi:solar-panel"),
                 _metric("sensor.solis_s6_eh3p_pv_total_energy_generation", "Namai", "mdi:home-outline", COLORS["pv"], decimals=0),
                 _metric("sensor.solis_inverter_1033300254190112_solis_energy_total", "Eimo", "mdi:home-outline", COLORS["pv"], decimals=0),
                 _note("kWh vertė, pasaugojimo mokestis ir metinė gamyba yra modelio prielaidos. Investicijos ir paramos sumos įrašomos atskirai.")),
    ])
    eso = {"type": "statistics-graph", "title": "ESO tinklo srautas (valandinis)", "chart_type": "bar", "period": "hour", "days_to_show": 7,
           "stat_types": ["change"], "entities": [_row(p["eso_import"], "Iš tinklo"), _row(p["eso_export"], "Į tinklą")]}
    monthly = deepcopy(eso)
    monthly.update(title="ESO mėnesinis balansas", period="month", days_to_show=365)
    consumption = _view(p, "Vartojimas", "vartojimas", "mdi:home-lightning-bolt-outline", "Namų įpročiai ir oficialūs ESO energijos srautai.", [
        _section(_panel(p, "profile")),
        _section(_heading("Vartojimo modelis", "mdi:brain"), _chart(legacy["consumption_model"]),
                 _entities("ESO banko duomenys", [_row(e["bank_official"], "Uždaryto mėnesio likutis"),
                           _row(e["bank_export"], "Atiduota banko metais"), _row(e["bank_import"], "Atsiimta banko metais")])),
        _section(_chart(legacy["hourly"]), visible=t["graphs"]),
        _section(_chart(legacy["weekday"]), visible=t["graphs"]),
        _section(_chart(eso), span=2, visible=t["graphs"]),
        _section(_bank_graph(p), visible=t["graphs"]),
        _section(_chart(monthly), visible=t["graphs"]),
    ])
    def graph_visibility(value):
        if isinstance(value, dict):
            if value.get("type") in ("custom:apexcharts-card", "statistics-graph", "history-graph"):
                value.setdefault("visibility", []).append({"condition": "state", "entity": t["graphs"], "state": "on"})
            for child in list(value.values()):
                graph_visibility(child)
        elif isinstance(value, list):
            for child in value:
                graph_visibility(child)

    for view in (energy, analysis, consumption):
        graph_visibility(view)
    return [energy, analysis, payback, consumption]
