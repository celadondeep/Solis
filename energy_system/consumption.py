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

from energy_system.consumption_math import remaining_profile_ratio
from energy_system.consumption_rolling import (
    window_bounds, rolling_daily_statistics, recorder_days, rolling_hourly_statistics,
)


def build_consumption_model(profile):
    window_days = int(profile['ROLLING_WINDOW_DAYS'])
    min_days = int(profile['HISTORY_MIN_DAYS'])
    tz = ZoneInfo(profile['TIMEZONE'])
    sensor, output = profile['SENSOR'], profile['OUTPUT']
    model_file = profile['MODEL_FILE']
    label = profile['SITE_LABEL']
    window_bounds(datetime.now(tz).date(), window_days)  # Validate before startup.

    class ConsumptionModel(hass.Hass):
        def local_now(self):
            return datetime.now(tz)

        def initialize(self):
            self.model = self.load_model()
            self.retry_timer = None
            self.recompute_from_history()
            self.run_daily(self.update_model, profile['DAILY_UPDATE_TIME'])
            self.run_in(self.update_model, profile.get('STARTUP_REFRESH_DELAY', 25))
            self.run_every(self.update_ha_sensors, 'now+5', 30 * 60)
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
                'recorder/get_statistics', statistic_ids=[sensor['today_consumption']],
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
            self.model['incomplete_hourly_days'] = incomplete
            self.model['statistics_refreshed_at'] = now.isoformat()
            self.model['statistics_status'] = 'ok'
            return complete

        def merge_days(self, totals, source):
            history = {item['date']: item for item in self.model['history']}
            for day, value in totals.items():
                if isfinite(value) and value > 0:
                    history[day] = dict(history.get(day, {}), date=day, kwh=round(value, 5), source=source)
            self.model['history'] = [history[key] for key in sorted(history)]
            self.normalize_model(self.model)

        def import_eso_data(self):
            """Optional CSV seed; dated samples still obey the same rolling window."""
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
            self.enrich_history_with_weather()
            self.save_model()
            self.update_ha_sensors({})
            # At most one delayed local-recorder retry per daily/startup run.
            if not kwargs.get('statistics_retry') and self.retry_timer is None and (complete is None or yesterday not in complete):
                self.retry_timer = self.run_in(self.update_model, 30*60, statistics_retry=True)

        def recompute_from_history(self):
            self.normalize_model(self.model)
            stats = rolling_daily_statistics(self.model['history'], today=self.local_now().date(),
                                             window_days=window_days, min_days=min_days)
            start, end = stats['window_start'], stats['window_end']
            days = {d: values for d, values in self.model.get('hourly_days', {}).items() if start <= d <= end}
            self.model['hourly_days'] = days
            hourly = rolling_hourly_statistics(days, excluded_days=stats['anomaly_days'],
                                                min_days=min_days, min_total=profile['PROFILE_MIN_TOTAL'])
            learned = hourly.pop('hourly_profile')
            self.model['rolling'] = dict(stats, **hourly)
            if stats['daily_mean_kwh'] is not None:
                self.model['daily_avg'] = stats['daily_mean_kwh']
                self.model['weekday_factors'] = stats['weekday_factors']
                self.model['forecast_method'] = 'rolling_mean_weekday_shrinkage'
            if learned is not None:
                self.model['hourly_profile'] = learned
                self.model['hourly_profile_window_end'] = end
            self.model.update(model_version=3, data_days=len(self.model['history']),
                              usable_days=stats['daily_sample_days'], anomaly_days=stats['anomaly_days'],
                              profile_days=hourly['hourly_sample_days'])
            return stats['daily_mean_kwh'] is not None

        def predict_daily(self, date=None, season=None):
            date = date or self.local_now().date()
            factor = float(self.model['weekday_factors'].get(str(date.weekday()), 1))
            # Old seasonal model is retained only until enough dated observations
            # exist. A learned rolling mean already follows seasonal consumption.
            seasonal = 1.0
            if self.model.get('forecast_method') != 'rolling_mean_weekday_shrinkage':
                month = date.month
                season = season or ('žiema' if month in (12,1,2) else 'pavasaris' if month in (3,4,5) else 'vasara' if month in (6,7,8) else 'ruduo')
                seasonal = float(self.model['season_factors'].get(season, 1))
            return round(float(self.model['daily_avg']) * factor * seasonal, 2)

        def predict_remaining_today(self):
            now = self.local_now()
            ratio = remaining_profile_ratio(self.model['hourly_profile'], now.hour, now.minute, now.second)
            return round(self.predict_daily() * ratio, 2)

        def predict_tomorrow(self):
            return self.predict_daily(self.local_now().date() + timedelta(days=1))

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
            common = {'window_days': window_days, 'window_start': stats['window_start'],
                      'window_end': stats['window_end'], 'sample_days': stats['daily_sample_days'],
                      'quality': stats['daily_status']}
            for entity, value, name in (
                (output['remaining'], self.predict_remaining_today(), 'likęs suvartojimas šiandien'),
                (output['tomorrow'], self.predict_tomorrow(), 'rytojaus suvartojimo prognozė'),
                (output['daily_avg'], stats['daily_mean_kwh'], 'dienos suvartojimo vidurkis'),
            ):
                self.set_state(entity, state=str(round(value, 2)) if value is not None else 'unknown',
                               attributes=dict(common, unit_of_measurement='kWh', friendly_name=f'{label}: {name}'))
            attrs = {key: value for key, value in stats.items() if key not in ('valid_dates', 'weekday_factors')}
            attrs.update(friendly_name=f'{label}: vartojimo profilis', icon='mdi:chart-bar',
                         daily_avg=stats['daily_mean_kwh'], data_days=len(self.model['history']),
                         usable_days=stats['daily_sample_days'], profile_days=stats['hourly_sample_days'],
                         anomaly_count=len(stats['anomaly_days']), model_version=3,
                         method='rolling_mean_filtered', timezone=profile['TIMEZONE'],
                         statistics_status=self.model.get('statistics_status', 'waiting'),
                         statistics_refreshed_at=self.model.get('statistics_refreshed_at'),
                         incomplete_hourly_days=self.model.get('incomplete_hourly_days', []),
                         forecast_today_kwh=self.predict_daily(),
                         hourly_forecast_window_end=self.model.get('hourly_profile_window_end'))
            # AppDaemon 4.5.13 drops numeric zero from REST payloads: use text.
            self.set_state(output['profile'], state=str(stats['hourly_sample_days']), attributes=attrs)

    return ConsumptionModel
