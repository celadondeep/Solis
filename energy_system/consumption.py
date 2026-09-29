"""One configurable rolling consumption model for every plant and transport."""
import csv
import json
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, time, timezone
from math import isfinite
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

from energy_system.consumption_rolling import (
    window_bounds, rolling_daily_statistics, recorder_days, rolling_hourly_statistics,
)
from energy_system.consumption_forecast import rates_for_day, integrate, intraday_ratio, fresh_daily_value, day_quality, instant
from energy_system.consumption_accuracy import freeze, score, freeze_hour, score_hours
from energy_system.consumption_hybrid import capture, projection, replay_errors, uncertainty, component_fractions, validate_plan, hybrid_reader, nowcast_gate
from energy_system.horizon import ha_attributes


def build_consumption_model(profile):
    window_days = int(profile['ROLLING_WINDOW_DAYS'])
    min_days = int(profile['HISTORY_MIN_DAYS'])
    tz = ZoneInfo(profile['TIMEZONE'])
    sensor, output = profile['SENSOR'], profile['OUTPUT']
    model_file = profile['MODEL_FILE']
    label = profile['SITE_LABEL']
    appliances = profile.get('APPLIANCES', [])
    if len({p['key'] for p in appliances}) != len(appliances) or len({p['energy_sensor'] for p in appliances}) != len(appliances):
        raise ValueError('Appliance keys and component meters must be unique')
    if len({p['plan_entity'] for p in appliances}) != len(appliances) or any(p['energy_sensor']==sensor['today_consumption'] for p in appliances):
        raise ValueError('Appliance plans must be unique and meters separate from the whole-house meter')
    source_max_age = min(600, int(profile['SOC_BUFFER']['max_age']))
    window_bounds(datetime.now(tz).date(), window_days)  # Validate before startup.
    if not 0 <= float(profile.get('WEEKDAY_STRENGTH', 1.0)) <= 1:
        raise ValueError('WEEKDAY_STRENGTH must be between 0 and 1')
    if not 0 <= float(profile.get('INTRADAY_GAIN', 0.0)) <= .75:
        raise ValueError('INTRADAY_GAIN must be between 0 and 0.75')

    class ConsumptionModel(hass.Hass):
        def local_now(self):
            return datetime.now(tz)

        def initialize(self):
            self.model = self.load_model()
            self.retry_timer = None
            self.power_samples = []
            self.recompute_from_history()
            self.run_every(self.sample_power, 'now+2', 60, publish_on_change=True)
            self.run_daily(self.update_model, profile['DAILY_UPDATE_TIME'])
            self.run_daily(self.update_ha_sensors, '00:00:05')
            self.run_in(self.update_model, profile.get('STARTUP_REFRESH_DELAY', 25))
            self.run_every(self.update_ha_sensors, 'now+5', 5 * 60)
            self.listen_event(self.manual_update, profile['MANUAL_EVENT'])
            self.log(f'[{label}] Vartojimas: {window_days} užbaigtų parų slenkantis langas')

        def default_hourly_profile(self):
            values = [.6,.5,.5,.5,.5,.6,.8,1.2,1.3,1.1,1,.9,1,.9,.8,.9,1.1,1.4,1.6,1.5,1.4,1.2,1,.8]
            return {str(h): value for h, value in enumerate(values)}

        def load_model(self):
            try:
                with open(model_file, encoding='utf-8') as handle:
                    result = json.load(handle)
                if not isinstance(result, dict):
                    raise ValueError('Model must be an object')
            except FileNotFoundError:
                result = {}
            except (ValueError, OSError) as error:
                self.log(f'[{label}] Modelio įkėlimo klaida: {error}', level='WARNING')
                result = {}
            result.setdefault('history', [])
            result.setdefault('daily_avg', profile['DEFAULT_DAILY_KWH'])
            result.setdefault('weekday_factors', dict(profile['DEFAULT_WEEKDAY_FACTORS']))
            result.setdefault('season_factors', dict(profile['DEFAULT_SEASON_FACTORS']))
            result.setdefault('hourly_profile', self.default_hourly_profile())
            return self.normalize_model(result)

        def normalize_model(self, model):
            oldest = self.local_now().date() - timedelta(days=profile['HISTORY_MAX_DAYS'])
            today = self.local_now().date()
            unique = {}
            for item in model.get('history', []):
                try:
                    day = datetime.strptime(str(item['date']), '%Y-%m-%d').date()
                    value = float(item['kwh'])
                    if oldest <= day < today and isfinite(value) and value > 0:
                        unique[day.isoformat()] = dict(item, kwh=value)
                except (KeyError, TypeError, ValueError):
                    continue
            model['history'] = [unique[key] for key in sorted(unique)]
            return model

        def save_model(self):
            """A partial write must not destroy the last valid learned model."""
            temporary = model_file + '.tmp'
            try:
                self.model['updated'] = self.local_now().isoformat()
                with open(temporary, 'w', encoding='utf-8') as handle:
                    json.dump(self.model, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, model_file)
            except (OSError, TypeError, ValueError) as error:
                self.log(f'[{label}] Modelio išsaugojimo klaida: {error}', level='ERROR')
                if os.path.exists(temporary):
                    os.unlink(temporary)

        def read_statistics(self):
            """One bounded query to HA's local recorder; never calls the inverter."""
            now = self.local_now()
            last = self.model.get('statistics_attempt_at')
            if last:
                try:
                    if 0 <= (now - datetime.fromisoformat(last)).total_seconds() < 900:
                        return None
                except (ValueError, TypeError):
                    pass
            self.model['statistics_attempt_at'] = now.isoformat()
            start, end = window_bounds(now.date(), window_days)
            result = self.call_service(
                'recorder/get_statistics', statistic_ids=[sensor['today_consumption']]+[p['energy_sensor'] for p in appliances],
                start_time=datetime.combine(start, time.min, tz).astimezone(timezone.utc).isoformat(),
                end_time=datetime.combine(end, time.min, tz).astimezone(timezone.utc).isoformat(),
                period='hour', types=['change'], units={'energy': 'kWh'},
                return_response=True, hass_timeout=20, timeout=25,
            )
            if not isinstance(result, dict) or result.get('success') is not True:
                raise ValueError('Recorder service did not return success')
            rows = result['result']['response']['statistics'][sensor['today_consumption']]
            if not isinstance(rows, list) or not rows:
                raise ValueError('Recorder returned no hourly statistics')
            complete, incomplete = recorder_days(rows, today=now.date(), window_days=window_days,
                                                 timezone_name=profile['TIMEZONE'])
            # The successful response is authoritative, including deleted or
            # corrected statistics. Do not retain disappeared rows as valid.
            self.model['hourly_days'] = complete
            self.model['component_days'] = {}
            for part in appliances:
                part_rows = result['result']['response']['statistics'].get(part['energy_sensor'], [])
                self.model['component_days'][part['key']], _ = recorder_days(
                    part_rows, today=now.date(), window_days=window_days, timezone_name=profile['TIMEZONE'])
            self.model['incomplete_hourly_days'] = incomplete
            self.model['statistics_refreshed_at'] = now.isoformat()
            self.model['statistics_status'] = 'ok'
            return complete

        def merge_days(self, totals, source):
            history = {item['date']: item for item in self.model['history']}
            for day, value in totals.items():
                if isfinite(value) and value > 0:
                    history[day] = dict(history.get(day, {}), date=day, kwh=round(value, 5), source=source)
                elif value == 0 and source == 'ha_recorder':
                    # An authoritative zero cannot leave yesterday's stale
                    # positive fallback in the training set for this date.
                    history.pop(day, None)
            self.model['history'] = [history[key] for key in sorted(history)]
            self.normalize_model(self.model)

        def import_eso_data(self):
            """Optional CSV seed; dated samples still obey the same rolling window."""
            if not profile.get('ALLOW_GRID_CSV_SEED', False):
                return
            filename = profile['ESO_CSV_FILE']
            if not os.path.exists(filename):
                return
            stat = os.stat(filename)
            fingerprint = f'{stat.st_size}:{stat.st_mtime_ns}'
            if self.model.get('eso_import_fingerprint') == fingerprint:
                return
            totals, hours = defaultdict(float), defaultdict(set)
            with open(filename, encoding='utf-8-sig') as handle:
                sample = handle.read(1024); handle.seek(0)
                for row in csv.DictReader(handle, delimiter=';' if ';' in sample else ','):
                    try:
                        values = list(row.values())
                        day = (row.get('Data') or row.get('Date') or row.get('date') or values[0]).strip()
                        clock = (row.get('Laikas') or row.get('Time') or row.get('time') or values[1]).strip()
                        stamp = datetime.strptime(f'{day} {clock}', '%Y-%m-%d %H:%M')
                        raw = row.get('Suvartojimas (kWh)') or row.get('kWh') or row.get('consumption') or values[2]
                        value = float(raw.replace(',', '.'))
                        if isfinite(value) and value >= 0:
                            totals[day] += value; hours[day].add(stamp.hour)
                    except (ValueError, TypeError, IndexError):
                        continue
            # Do not overwrite actual household totals with grid-only CSV data.
            known = {item['date'] for item in self.model['history']}
            self.merge_days({d: v for d, v in totals.items() if len(hours[d]) == 24 and d not in known}, 'csv_seed')
            self.model['eso_import_fingerprint'] = fingerprint

        def manual_update(self, event_name, data, kwargs):
            self.update_model({})

        def update_model(self, kwargs):
            if kwargs.get('statistics_retry'):
                self.retry_timer = None
            yesterday = (self.local_now().date() - timedelta(days=1)).isoformat()
            try:
                self.import_eso_data()
            except (OSError, ValueError) as error:
                self.log(f'[{label}] CSV importas praleistas: {error}', level='WARNING')
            complete = None
            try:
                complete = self.read_statistics()
                if complete is not None:
                    self.merge_days({d: values['kwh'] for d, values in complete.items()}, 'ha_recorder')
            except (KeyError, TypeError, ValueError, OSError, TimeoutError) as error:
                self.model['statistics_status'] = 'cached_after_error'
                self.log(f'[{label}] HA valandinė istorija laikinai nepasiekiama: {error}', level='WARNING')
            # A dedicated yesterday meter can fill an incomplete recorder day,
            # but only when freshly reported today; partial power samples cannot.
            if not complete or yesterday not in complete:
                try:
                    record = self.get_state(sensor['daily_consumption'], attribute='all') or {}
                    stamp = record.get('last_updated')
                    stamp = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
                    value = float(record['state'])
                    if stamp.tzinfo is not None and stamp.astimezone(tz).date() == self.local_now().date() and isfinite(value) and value > 0:
                        self.merge_days({yesterday: value}, 'yesterday_meter')
                except (KeyError, TypeError, ValueError):
                    pass
            self.recompute_from_history()
            if profile.get('ENRICH_WEATHER', False):
                self.enrich_history_with_weather()
            self.save_model()
            self.update_ha_sensors({})
            # At most one delayed local-recorder retry per daily/startup run.
            if not kwargs.get('statistics_retry') and self.retry_timer is None and (complete is None or yesterday not in complete):
                self.retry_timer = self.run_in(self.update_model, 30*60, statistics_retry=True)

        def recompute_from_history(self):
            self.normalize_model(self.model)
            quality = day_quality(self.model.get('hourly_days', {}))
            history = [r for r in self.model['history'] if r['date'] not in quality]
            stats = rolling_daily_statistics(history, today=self.local_now().date(),
                                             window_days=window_days, min_days=min_days)
            start, end = stats['window_start'], stats['window_end']
            days = {d: values for d, values in self.model.get('hourly_days', {}).items() if start <= d <= end}
            self.model['hourly_days'] = days
            hourly = rolling_hourly_statistics(days, excluded_days=set(stats['anomaly_days']) | set(quality),
                                                min_days=min_days, min_total=profile['PROFILE_MIN_TOTAL'])
            learned = hourly.pop('hourly_profile')
            self.model['rolling'] = dict(stats, **hourly)
            if stats['daily_mean_kwh'] is not None:
                self.model['daily_avg'] = stats['daily_mean_kwh']
                self.model['weekday_factors'] = stats['weekday_factors']
                strength = float(profile.get('WEEKDAY_STRENGTH', 1.0))
                self.model['forecast_method'] = 'rolling_mean' if strength == 0 else 'rolling_mean_weekday_shrinkage'
                self.model['daily_latest_sample'] = max(stats['valid_dates'])
            if learned is not None:
                self.model['hourly_profile'] = learned
                self.model['hourly_profile_window_end'] = end
                self.model['hourly_latest_sample'] = max(d for d in days if d not in hourly['hourly_excluded_days'])
            self.model.update(model_version=5, data_days=len(self.model['history']),
                              usable_days=stats['daily_sample_days'], anomaly_days=stats['anomaly_days'],
                              profile_days=hourly['hourly_sample_days'], quality_issues=quality)
            return stats['daily_mean_kwh'] is not None

        def predict_daily(self, date=None, season=None):
            date = date or self.local_now().date()
            factor = float(self.model['weekday_factors'].get(str(date.weekday()), 1))
            factor = 1 + (factor-1)*float(profile.get('WEEKDAY_STRENGTH', 1.0))
            # Old seasonal model is retained only until enough dated observations
            # exist. A learned rolling mean already follows seasonal consumption.
            seasonal = 1.0
            if not str(self.model.get('forecast_method', '')).startswith('rolling_mean'):
                month = date.month
                season = season or ('žiema' if month in (12,1,2) else 'pavasaris' if month in (3,4,5) else 'vasara' if month in (6,7,8) else 'ruduo')
                seasonal = float(self.model['season_factors'].get(season, 1))
            return round(float(self.model['daily_avg']) * factor * seasonal, 2)

        def sample_power(self, kwargs):
            now = self.local_now()
            previous = getattr(self, 'power_source_status', None)
            record = self.get_state(sensor['house_load'], attribute='all') or {}
            heartbeat = self.get_state(profile['SOC_BUFFER']['heartbeat'])
            self.power_samples, self.power_source_status = capture(
                getattr(self, 'power_samples', []), record, heartbeat, now, max_age=source_max_age)
            power_entity = sensor.get('inverter_power')
            if power_entity and self.get_state(power_entity) != 'on':
                self.power_samples = []
                self.power_source_status = 'inverter_off_or_unknown'
            # Withdraw the optional correction promptly after source loss.
            # Routine publication remains once per five minutes, entirely local.
            if (kwargs.get('publish_on_change') and previous in ('fresh','duplicate_source')
                    and self.power_source_status not in ('fresh','duplicate_source')):
                self.run_in(self.update_ha_sensors, 1)

        def hybrid_forecasts(self, forecasts):
            now = self.local_now()
            def base(at):
                row = forecasts.get(str(at.date()))
                # Recent samples can precede midnight. Use the learned shape
                # for their own date; never index yesterday into today's total.
                rates = row['hourly_kw'] if row else rates_for_day(
                    self.model['hourly_profile'], self.predict_daily(at.date()), at.date(), profile['TIMEZONE'])
                return rates[at.hour]
            self.sample_power({})
            first = datetime.combine(now.date(), time.min, tz)
            expected = integrate(base, first, now)
            record = self.get_state(sensor.get('actual_today', sensor['today_consumption']), attribute='all') or {}
            actual = fresh_daily_value(record, now, profile.get('ACTUAL_MAX_AGE_SECONDS', 1800))
            fresh = self.power_source_status in ('fresh', 'duplicate_source')
            factor = intraday_ratio(actual if fresh else None, expected, profile.get('INTRADAY_GAIN', 0.0))
            applied, issues = [], {}
            accepted = set(self.model.get('hourly_days', {}))-set(self.model['rolling']['hourly_excluded_days'])
            for part in appliances:
                try:
                    fractions, count = component_fractions(self.model.get('hourly_days', {}),
                        self.model.get('component_days', {}).get(part['key'], {}), accepted)
                    plan = validate_plan(self.get_state(part['plan_entity'], attribute='all') or {}, now, max_kw=part['max_kw'])
                    if any(sum(p['historical_fraction'][h] for p in applied)+fractions[h] > 1+1e-6 for h in range(24)):
                        raise ValueError('overlapping_component_meters')
                    applied.append(dict(key=part['key'], historical_fraction=fractions,
                                        history_days=count, max_kw=part['max_kw'], plan=plan))
                except (KeyError, TypeError, ValueError) as error:
                    issues[part['key']] = str(error)
            # A scheduled load could also be present in measured whole-house
            # power. Until its live background is separately observed, plans
            # take priority and short-term whole-house corrections are disabled.
            projected = projection(self.power_samples if fresh else [], now, base,
                daily_ratio=factor if not applied else 1, gain=profile.get('NOWCAST_GAIN', .35) if not applied else 0,
                max_age=source_max_age, max_gap=max(180, source_max_age+60))
            if not fresh:
                projected.update(status=self.power_source_status, cumulative_ratio=1.0)
            elif applied:
                projected['status'] = 'appliance_plan_priority'
            data = dict(version=1, projection=projected, appliances=applied,
                        configured_appliances=len(appliances), active_appliances=len(applied),
                        appliance_issues=issues, power_source_status=self.power_source_status,
                        correction_scope='today_only_next_hour_power',
                        interval_scope='next_day_total_only')
            self._hybrid_candidate_reader = hybrid_reader(base, now, data)
            self._hybrid_candidate_active = projected['status']=='active' and abs(projected['power_delta_kw'])>1e-6
            self._hybrid_baseline_reader = hybrid_reader(base, now, dict(data,projection=dict(projected,power_delta_kw=0)))
            scores = score_hours(self.model.get('hybrid_hour_ledger',{}),today=now.date(),
                days=self.model.get('hourly_days',{}),invalid=self.model.get('quality_issues',{}))
            gate = nowcast_gate(scores,previously_enabled=self.model.get('nowcast_enabled',False))
            self.model['nowcast_enabled'] = gate['enabled']
            data['validation'] = gate
            projected['candidate_power_delta_kw'] = projected['power_delta_kw']
            if not gate['enabled']:
                projected['power_delta_kw'] = 0.0
                if projected['status'] == 'active':
                    projected['status'] = 'validation_pending' if gate['status']=='collecting' else 'validation_rejected'
            reader = hybrid_reader(base, now, data)
            key = (str(now.date()), tuple((r['date'],r['kwh']) for r in self.model['history']),
                   tuple(sorted(self.model.get('quality_issues', {}))))
            if getattr(self, '_interval_key', None) != key:
                history = [r for r in self.model['history'] if r['date'] not in self.model.get('quality_issues', {})]
                self._interval_errors = replay_errors(history, today=now.date(), strength=profile.get('WEEKDAY_STRENGTH', 1),
                    window=window_days, min_days=min_days, target_dates=set(self.model.get('hourly_days',{})))
                self._interval_key = key
            for delta in (0,1):
                day = now.date()+timedelta(days=delta)
                start = datetime.combine(day,time.min,tz)
                end = datetime.combine(day+timedelta(days=1),time.min,tz)
                forecasts[str(day)]['hybrid_daily_kwh'] = round(integrate(reader,start,end),5)
                if delta == 1:
                    forecasts[str(day)]['interval'] = uncertainty(forecasts[str(day)]['hybrid_daily_kwh'], self._interval_errors)
                    if applied:
                        forecasts[str(day)]['interval']['status'] = 'baseline_residuals_with_unvalidated_plan'
            end = datetime.combine(now.date()+timedelta(days=1), time.min, tz)
            data['remaining_kwh'] = round(integrate(reader,now,end),5)
            return data, reader

        def predict_remaining_today(self):
            forecasts, _ = self.dated_forecasts()
            data, _ = self.hybrid_forecasts(forecasts)
            return round(data['remaining_kwh'],2)

        def predict_tomorrow(self):
            return self.predict_daily(self.local_now().date() + timedelta(days=1))

        def dated_forecasts(self):
            now = self.local_now()
            freshness = {key: self.model.get(key) for key in ('daily_latest_sample', 'hourly_latest_sample')}
            status = 'ok'
            try:
                ages = [(now.date()-datetime.fromisoformat(value).date()).days for value in freshness.values()]
                if min(ages) < 1 or max(ages) > profile.get('MAX_TRAINING_AGE_DAYS', 7):
                    status = 'stale'
                elif max(ages) > 1 or self.model.get('statistics_status') != 'ok':
                    status = 'cached'
            except (TypeError, ValueError):
                status = 'insufficient_data'
            forecasts = {}
            for delta in (0, 1):
                day = now.date()+timedelta(days=delta)
                total = self.predict_daily(day)
                forecasts[str(day)] = dict(daily_kwh=total, hourly_kw=rates_for_day(
                    self.model['hourly_profile'], total, day, profile['TIMEZONE']), **freshness)
            return forecasts, status

        def accuracy(self, forecasts, status, reader=None):
            now = self.local_now()
            method = 'v5:hybrid:'+self.model.get('forecast_method', 'bootstrap')+':wd='+str(profile.get('WEEKDAY_STRENGTH', 1.0))
            ledger = self.model.setdefault('forecast_ledger', {})
            hourly_ledger = self.model.setdefault('hybrid_hour_ledger', {})
            if status in ('ok','cached') and reader is not None:
                candidate = getattr(self,'_hybrid_candidate_reader',reader)
                baseline = getattr(self,'_hybrid_baseline_reader',lambda at: forecasts[str(at.date())]['hourly_kw'][at.hour])
                if freeze_hour(hourly_ledger,now=now,forecast=candidate,baseline=baseline,applied=reader,
                               active=getattr(self,'_hybrid_candidate_active',False)):
                    self.save_model()
            oldest = str(now.date()-timedelta(days=120))
            for key in list(ledger):
                if ledger[key]['target_date'] < oldest:
                    del ledger[key]
            # The midnight publication changes forecast dates immediately, but
            # the ledger waits for today's recorder refresh (normally 00:20).
            try:
                refreshed_today = datetime.fromisoformat(self.model.get('statistics_refreshed_at', '')).astimezone(tz).date() == now.date()
            except (TypeError, ValueError):
                refreshed_today = False
            if status in ('ok', 'cached') and refreshed_today:
                target = now.date()+timedelta(days=1)
                row = forecasts[str(target)]
                baseline = row['daily_kwh']
                if freeze(ledger, now=now, forecast=row.get('hybrid_daily_kwh', baseline), baseline=baseline,
                          method=method, trained_through=self.model['daily_latest_sample'], interval=row.get('interval')):
                    self.save_model()
            return score(ledger, today=now.date(), days=self.model.get('hourly_days', {}),
                         invalid=self.model.get('quality_issues', {}), method=method)

        def enrich_history_with_weather(self):
            if not any('t_mean' not in item for item in self.model['history']):
                return
            try:
                with urllib.request.urlopen(profile['WEATHER_URL'], timeout=10) as response:
                    daily = json.load(response)['daily']
                values = dict(zip(daily['time'], zip(daily['temperature_2m_mean'], daily['temperature_2m_max'])))
                for item in self.model['history']:
                    mean, maximum = values.get(item['date'], (None, None))
                    if 't_mean' not in item and mean is not None:
                        item['t_mean'] = mean
                        if maximum is not None:
                            item['t_max'] = maximum
            except Exception as error:
                self.log(f'[{label}] Orų papildymas praleistas: {error}', level='WARNING')

        def update_ha_sensors(self, kwargs):
            # Move the calendar window even if yesterday's query failed. Cached
            # data can be used, but old dates cannot silently re-enter the window.
            self.recompute_from_history()
            stats = self.model['rolling']
            forecasts, status = self.dated_forecasts()
            hybrid, reader = self.hybrid_forecasts(forecasts)
            accuracy = self.accuracy(forecasts, status, reader)
            hybrid['accuracy'] = score_hours(self.model.get('hybrid_hour_ledger',{}),today=self.local_now().date(),
                days=self.model.get('hourly_days',{}),invalid=self.model.get('quality_issues',{}))
            next_day = forecasts[str(self.local_now().date()+timedelta(days=1))]
            common = {'window_days': window_days, 'window_start': stats['window_start'],
                      'window_end': stats['window_end'], 'sample_days': stats['daily_sample_days'],
                      'quality': stats['daily_status'], 'forecast_status': status,
                      'forecast_method': self.model.get('forecast_method')}
            for entity, value, name in (
                (output['remaining'], hybrid['remaining_kwh'], 'likęs suvartojimas šiandien'),
                (output['tomorrow'], next_day['hybrid_daily_kwh'], 'rytojaus suvartojimo prognozė'),
                (output['daily_avg'], stats['daily_mean_kwh'], 'dienos suvartojimo vidurkis'),
            ):
                self.set_state(entity, state=str(round(value, 2)) if value is not None else 'unknown',
                               attributes=dict(common, unit_of_measurement='kWh', friendly_name=f'{label}: {name}'))
            attrs = {key: value for key, value in stats.items() if key not in ('valid_dates', 'weekday_factors')}
            attrs.update(friendly_name=f'{label}: vartojimo profilis', icon='mdi:chart-bar',
                         daily_avg=stats['daily_mean_kwh'], data_days=len(self.model['history']),
                         usable_days=stats['daily_sample_days'], profile_days=stats['hourly_sample_days'],
                         anomaly_count=len(stats['anomaly_days']), model_version=5,
                         method='rolling_mean_filtered', timezone=profile['TIMEZONE'],
                         statistics_status=self.model.get('statistics_status', 'waiting'),
                         statistics_refreshed_at=self.model.get('statistics_refreshed_at'),
                         incomplete_hourly_days=self.model.get('incomplete_hourly_days', []),
                         forecast_today_kwh=self.predict_daily(),
                         forecast_method=self.model.get('forecast_method'),
                         forecasts=forecasts, forecast_status=status, accuracy=accuracy,
                         hybrid=hybrid, forecast_interval=next_day['interval'],
                         quality_issues=self.model.get('quality_issues', {}),
                         intraday_gain=profile.get('INTRADAY_GAIN', 0.0),
                         hourly_forecast_window_end=self.model.get('hourly_profile_window_end'))
            # AppDaemon 4.5.13 drops numeric zero from REST payloads: use text.
            self.set_state(output['profile'], state=str(stats['hourly_sample_days']), attributes=ha_attributes(attrs))

    return ConsumptionModel
