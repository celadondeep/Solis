"""Bendras abiejų elektrinių dinaminio vartojimo modelis."""

import csv
import json
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
import appdaemon.plugins.hass.hassapi as hass

from energy_system.consumption_math import (
    remaining_profile_ratio,
    robust_consumption_statistics,
)


def build_consumption_model(profile):
    """Sukuria izoliuotą vartojimo modelį iš elektrinės profilio."""
    HISTORY_MAX_DAYS = profile["HISTORY_MAX_DAYS"]
    HISTORY_MIN_DAYS = profile["HISTORY_MIN_DAYS"]
    MEDIAN_WINDOW_DAYS = profile["MEDIAN_WINDOW_DAYS"]
    PROFILE_ALPHA = profile["PROFILE_ALPHA"]
    PROFILE_MIN_TOTAL = profile["PROFILE_MIN_TOTAL"]
    PROFILE_MIN_HOURS = profile["PROFILE_MIN_HOURS"]
    WEATHER_URL = profile["WEATHER_URL"]
    ESO_CSV_FILE = profile["ESO_CSV_FILE"]
    MODEL_FILE = profile["MODEL_FILE"]
    DEFAULT_DAILY_KWH = profile["DEFAULT_DAILY_KWH"]
    DEFAULT_WEEKDAY_FACTORS = profile["DEFAULT_WEEKDAY_FACTORS"]
    DEFAULT_SEASON_FACTORS = profile["DEFAULT_SEASON_FACTORS"]
    SENSOR = profile["SENSOR"]
    OUTPUT = profile["OUTPUT"]
    MANUAL_EVENT = profile["MANUAL_EVENT"]
    SITE_LABEL = profile["SITE_LABEL"]

    class ConsumptionModel(hass.Hass):

        def initialize(self):
            self.log(f"[{SITE_LABEL}] ConsumptionModel paleidžiamas...")

            self.model = self.load_model()
            self.daily_readings = []

            # Senas modelio failas perkeliamas į robust v2 formatą deterministiškai.
            before = json.dumps(self.model, sort_keys=True, ensure_ascii=False)
            if self.recompute_from_history():
                after = json.dumps(self.model, sort_keys=True, ensure_ascii=False)
                if after != before:
                    self.save_model()

            # ESO failas importuojamas tik jam realiai pasikeitus.
            if os.path.exists(ESO_CSV_FILE):
                fingerprint = self.eso_file_fingerprint()
                if fingerprint != self.model.get("eso_import_fingerprint"):
                    self.import_eso_data()
                else:
                    self.log("ESO failas nepasikeitė — pakartotinis importas praleistas")

            # Kaupti realiojo laiko duomenis — kas 15 min
            self.run_every(self.collect_reading, "now", 15 * 60)

            # Atnaujinti modelį — kas dieną 00:05
            self.run_daily(self.update_model, "00:05:00")

            # Rankinis paleidimas derinimui: fire_event CONSUMPTION_MODEL_RUN
            self.listen_event(self.manual_update, MANUAL_EVENT)

            # Eksponuoti sensorių į HA
            self.run_every(self.update_ha_sensors, "now+30", 30 * 60)

            self.log(f"[{SITE_LABEL}] ConsumptionModel paleistas.")

        # ============================================================
        #  MODELIO ĮKĖLIMAS / IŠSAUGOJIMAS
        # ============================================================

        def load_model(self):
            """Įkelia modelį iš failo arba naudoja default."""
            try:
                if os.path.exists(MODEL_FILE):
                    with open(MODEL_FILE, "r") as f:
                        model = json.load(f)
                        model = self.normalize_model(model)
                        self.log(
                            f"Modelis įkeltas. Duomenų dienų: "
                            f"{model.get('data_days', 0)}"
                        )
                        return model
            except Exception as e:
                self.log(f"Modelio įkėlimo klaida: {e}")

            return {
                "daily_avg":        DEFAULT_DAILY_KWH,
                "weekday_factors":  DEFAULT_WEEKDAY_FACTORS,
                "season_factors":   DEFAULT_SEASON_FACTORS,
                "hourly_profile":   self.default_hourly_profile(),
                "history":          [],
                "data_days":        0,
                "updated":          None,
            }

        def normalize_model(self, model):
            """Suvienodina senų JSON versijų raktus ir šalina dubliuotas datas."""
            factors = model.get("weekday_factors") or DEFAULT_WEEKDAY_FACTORS
            model["weekday_factors"] = {
                str(key): float(value) for key, value in factors.items()
            }
            season = model.get("season_factors") or DEFAULT_SEASON_FACTORS
            model["season_factors"] = {
                str(key): float(value) for key, value in season.items()
            }

            unique = {}
            for item in model.get("history", []):
                try:
                    day = datetime.strptime(
                        str(item.get("date")), "%Y-%m-%d"
                    ).date().isoformat()
                    kwh = float(item.get("kwh"))
                    if kwh <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                clean = dict(item)
                clean["date"] = day
                clean["kwh"] = round(kwh, 3)
                unique[day] = clean
            model["history"] = [unique[key] for key in sorted(unique)]
            model["data_days"] = len(model["history"])
            return model

        def eso_file_fingerprint(self):
            stat = os.stat(ESO_CSV_FILE)
            return f"{stat.st_size}:{stat.st_mtime_ns}"

        def save_model(self):
            """Išsaugo modelį į failą."""
            try:
                self.model["updated"] = datetime.now().isoformat()
                with open(MODEL_FILE, "w") as f:
                    json.dump(self.model, f, indent=2)
                self.log("Modelis išsaugotas.")
            except Exception as e:
                self.log(f"Modelio išsaugojimo klaida: {e}")

        def default_hourly_profile(self):
            """Numatytasis valandinis profilis (normalizuotas)."""
            # Tipiškas Lietuvos namų ūkio profilis
            profile = {
                "0": 0.6, "1": 0.5, "2": 0.5, "3": 0.5,
                "4": 0.5, "5": 0.6, "6": 0.8, "7": 1.2,
                "8": 1.3, "9": 1.1, "10": 1.0, "11": 0.9,
                "12": 1.0, "13": 0.9, "14": 0.8, "15": 0.9,
                "16": 1.1, "17": 1.4, "18": 1.6, "19": 1.5,
                "20": 1.4, "21": 1.2, "22": 1.0, "23": 0.8,
            }
            return profile

        # ============================================================
        #  ESO DUOMENŲ IMPORTAS
        # ============================================================

        def import_eso_data(self):
            """
            Importuoja ESO istorinius duomenis iš CSV failo.
            Apskaičiuoja dieninius vidurkius ir savaitės koeficientus.
            """
            self.log(f"Importuojami ESO duomenys iš {ESO_CSV_FILE}...")

            daily_totals    = defaultdict(float)
            daily_counts    = defaultdict(int)
            hourly_totals   = defaultdict(float)
            hourly_counts   = defaultdict(int)

            rows_read = 0
            errors    = 0

            try:
                with open(ESO_CSV_FILE, "r", encoding="utf-8-sig") as f:
                    # Pabandyti nustatyti separatorių automatiškai
                    sample = f.read(1024)
                    f.seek(0)
                    delimiter = ";" if ";" in sample else ","

                    reader = csv.DictReader(f, delimiter=delimiter)

                    for row in reader:
                        try:
                            # Lankstus stulpelių pavadinimų apdorojimas
                            date_str = (
                                row.get("Data") or
                                row.get("Date") or
                                row.get("date") or
                                list(row.values())[0]
                            ).strip()

                            time_str = (
                                row.get("Laikas") or
                                row.get("Time") or
                                row.get("time") or
                                list(row.values())[1]
                            ).strip()

                            kwh_str = (
                                row.get("Suvartojimas (kWh)") or
                                row.get("kWh") or
                                row.get("consumption") or
                                list(row.values())[2]
                            ).strip().replace(",", ".")

                            dt  = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                            kwh = float(kwh_str)

                            day_key     = dt.date().isoformat()
                            weekday     = str(dt.weekday())
                            hour        = str(dt.hour)

                            daily_totals[day_key]  += kwh
                            daily_counts[day_key]  += 1
                            hourly_totals[hour]    += kwh
                            hourly_counts[hour]    += 1
                            rows_read += 1

                        except (ValueError, KeyError, IndexError):
                            errors += 1
                            continue

            except Exception as e:
                self.log(f"ESO failo skaitymo klaida: {e}", level="ERROR")
                return

            if not daily_totals:
                self.log("ESO duomenų nepavyko perskaityti.", level="WARNING")
                return

            self.log(f"ESO: {rows_read} eilutės, {len(daily_totals)} dienos, {errors} klaidos")

            # Dienos vidurkis
            complete_days = {k: v for k, v in daily_totals.items() if daily_counts[k] >= 20}
            if complete_days:
                avg = sum(complete_days.values()) / len(complete_days)
                self.model["daily_avg"] = round(avg, 2)
                self.model["data_days"] = len(complete_days)
                self.log(f"Dienos vidurkis iš ESO: {avg:.2f} kWh ({len(complete_days)} dienų)")

            # Savaitės koeficientai
            weekday_totals  = defaultdict(list)
            for day_str, total in complete_days.items():
                dt      = datetime.strptime(day_str, "%Y-%m-%d")
                weekday = str(dt.weekday())
                weekday_totals[weekday].append(total)

            weekday_avgs = {}
            for wd, vals in weekday_totals.items():
                weekday_avgs[wd] = sum(vals) / len(vals)

            if weekday_avgs:
                overall_avg = sum(weekday_avgs.values()) / len(weekday_avgs)
                factors = {wd: round(avg / overall_avg, 3) for wd, avg in weekday_avgs.items()}
                self.model["weekday_factors"] = factors
                self.log(f"Savaitės koeficientai atnaujinti iš ESO duomenų.")

            # Valandinis profilis
            if hourly_counts:
                hour_avgs = {
                    h: hourly_totals[h] / hourly_counts[h]
                    for h in hourly_totals
                }
                overall_hour_avg = sum(hour_avgs.values()) / len(hour_avgs)
                profile = {
                    h: round(avg / overall_hour_avg, 3)
                    for h, avg in hour_avgs.items()
                }
                self.model["hourly_profile"] = profile
                self.log("Valandinis profilis atnaujintas iš ESO duomenų.")

            # Sezoniniai koeficientai iš ESO duomenų
            season_totals  = defaultdict(list)
            for day_str, total in complete_days.items():
                month  = datetime.strptime(day_str, "%Y-%m-%d").month
                season = self.month_to_season(month)
                season_totals[season].append(total)

            if len(season_totals) >= 2:
                season_avgs = {s: sum(v) / len(v) for s, v in season_totals.items()}
                overall_s   = sum(season_avgs.values()) / len(season_avgs)
                s_factors   = {s: round(avg / overall_s, 3) for s, avg in season_avgs.items()}
                self.model["season_factors"] = s_factors
                self.log(f"Sezoniniai koeficientai atnaujinti: {s_factors}")

            self.model["eso_import_fingerprint"] = self.eso_file_fingerprint()
            self.save_model()
            self.log("ESO importas baigtas sėkmingai.")

        def month_to_season(self, month):
            if month in (12, 1, 2):   return "žiema"
            elif month in (3, 4, 5):  return "pavasaris"
            elif month in (6, 7, 8):  return "vasara"
            else:                     return "ruduo"

        # ============================================================
        #  PROGNOZAVIMAS
        # ============================================================

        def predict_daily(self, date=None, season=None):
            """
            Prognozuoja dienos suvartojimą kWh.
            Naudoja savaitės dienos ir sezono koeficientus.
            """
            if date is None:
                date = datetime.now().date()

            if season is None:
                season = self.get_current_season()

            weekday = str(date.weekday())

            weekday_factor = float(
                self.model["weekday_factors"].get(weekday, 1.0)
            )
            season_factor = float(
                self.model["season_factors"].get(season, 1.0)
            )

            prediction = self.model["daily_avg"] * weekday_factor * season_factor
            return round(prediction, 2)

        def predict_remaining_today(self):
            """Prognozuoja likusį suvartojimą šiandien kWh."""
            now          = datetime.now()
            profile = self.model.get(
                "hourly_profile", self.default_hourly_profile()
            )
            ratio = remaining_profile_ratio(
                profile, now.hour, now.minute, now.second
            )
            daily_pred = self.predict_daily()
            remaining = daily_pred * ratio
            return round(remaining, 2)

        def predict_tomorrow(self):
            """Prognozuoja rytojaus suvartojimą kWh."""
            tomorrow = datetime.now().date() + timedelta(days=1)
            return self.predict_daily(date=tomorrow)

        def get_current_season(self):
            """Grąžina dabartinį sezoną."""
            try:
                season = self.get_state(SENSOR["season"])
                if season in self.model.get("season_factors", {}):
                    return season
            except Exception:
                pass
            return self.month_to_season(datetime.now().month)

        # ============================================================
        #  REALIŲ DUOMENŲ KAUPIMAS
        # ============================================================

        def collect_reading(self, kwargs):
            """Renka realiojo laiko duomenis kas 15 min."""
            try:
                load_w = float(self.get_state(SENSOR["house_load"]) or 0)
                load_kwh = load_w / 1000 * (15 / 60)  # kWh per 15 min

                self.daily_readings.append({
                    "hour":  datetime.now().hour,
                    "kwh":   round(load_kwh, 4),
                })
            except Exception:
                pass

        def manual_update(self, event_name, data, kwargs):
            self.log("[MANUAL] Rankinis modelio atnaujinimas paleistas")
            self.update_model({})

        def update_model(self, kwargs):
            """
            Atnaujina modelį su vakardienos realiais duomenimis.
            Veikia kas dieną 00:05.
            """
            # Vakardienos faktinė paros suma: pirmiausia iš Solis paros skaitiklio
            # (tikslu ir atsparu restartams), kitu atveju — iš surinktų 15 min matavimų.
            meter_total = None
            try:
                raw = self.get_state(SENSOR["daily_consumption"])
                if raw not in (None, "unavailable", "unknown", ""):
                    meter_total = float(raw)
            except (ValueError, TypeError):
                meter_total = None

            readings_total = (
                sum(r["kwh"] for r in self.daily_readings) if self.daily_readings else 0.0
            )

            if meter_total and meter_total > 0:
                yesterday_total = meter_total
            elif readings_total > 0:
                yesterday_total = readings_total
            else:
                # Nėra patikimų duomenų — modelio nekeičiam (kad nedegraduotų vidurkis).
                self.daily_readings = []
                return

            yesterday       = (datetime.now() - timedelta(days=1)).date()
            weekday         = str(yesterday.weekday())
            season          = self.month_to_season(yesterday.month)

            self.log(
                f"[MODEL] Vakar suvartotas: {yesterday_total:.2f} kWh "
                f"(savaitės diena: {weekday}, sezonas: {season})"
            )

            # Paros istorija — medianinei bazei ir savaitės koeficientų mokymuisi
            history = self.model.setdefault("history", [])
            day_key = yesterday.isoformat()
            history[:] = [h for h in history if h.get("date") != day_key]
            history.append({"date": day_key, "kwh": round(yesterday_total, 2)})
            history.sort(key=lambda h: h["date"])
            del history[:-HISTORY_MAX_DAYS]

            # Prie parų be temperatūros pridedame t_mean/t_max (žr. WEATHER_URL).
            # Nepavykus — tyliai praleidžiama, modelio atnaujinimo tai neblokuoja.
            self.enrich_history_with_weather(history)

            # Valandinio profilio EMA mokymasis iš vakardienos valandinių deltų
            self.update_hourly_profile()

            if not self.recompute_from_history():
                # Istorija dar trumpa — senas EMA kelias. Prieš įtraukiant
                # pašaliname savaitės dienos ir sezono įtaką (deseasonalize),
                # kad daily_avg liktų BENDRAS vidurkis ir predict_daily
                # nepadaugintų koeficientų antrą kartą.
                wf = float(self.model["weekday_factors"].get(weekday, 1.0)) or 1.0
                sf = float(self.model["season_factors"].get(season, 1.0)) or 1.0
                baseline = yesterday_total / (wf * sf)

                alpha = 0.1
                old_avg = self.model["daily_avg"]
                new_avg = old_avg * (1 - alpha) + baseline * alpha
                self.model["daily_avg"] = round(new_avg, 3)

            self.model["data_days"] = len(history)
            self.save_model()
            self.daily_readings = []
            # Iškart atnaujinam HA jutiklius (profilio dashbordas) — kad po
            # naktinio mokymosi nereiktų laukti 30 min ciklo.
            self.update_ha_sensors({})

        def hourly_consumption_yesterday(self):
            """Vakardienos suvartojimas pavalandžiui {0..23: kWh} iš kumuliacinio
            sensoriaus recorder istorijos. Tuščias dict — jei duomenų nepakanka.
            Veikia ir su paros (resetinasi 00:00), ir su monotoniniu skaitliuku —
            atskaita nuo pirmos lango reikšmės, kritimai (reset) apkerpami iki 0."""
            day_start = (datetime.now().astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
            day_end = day_start + timedelta(days=1)
            try:
                hist = self.get_history(entity_id=SENSOR["today_consumption"],
                                        start_time=day_start, end_time=day_end)
            except Exception as e:  # noqa: BLE001
                self.log(f"[PROFILIS] get_history klaida: {e}", level="WARNING")
                return {}
            if not hist or not hist[0]:
                self.log("[PROFILIS] get_history grąžino tuščią istoriją", level="WARNING")
                return {}
            points = []
            for s in hist[0]:
                try:
                    # 2026-07-23: AppDaemon get_history grąžina last_changed kaip
                    # datetime OBJEKTĄ (ne ISO tekstą), todėl fromisoformat mesdavo
                    # TypeError kiekvienam taškui → profilis niekada nesimokė.
                    lc = s.get("last_changed") or s.get("last_updated")
                    t = (lc if isinstance(lc, datetime)
                         else datetime.fromisoformat(str(lc)))
                    t = t.astimezone()
                    v = float(s["state"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if day_start <= t < day_end:
                    points.append((t, v))
            if len(points) < 2:
                self.log(f"[PROFILIS] per mažai taškų lange ({len(points)}) "
                         f"iš {len(hist[0])} istorijos įrašų", level="WARNING")
                return {}
            points.sort()
            last_in_hour = {}
            for t, v in points:
                last_in_hour[t.hour] = v
            per_hour = {}
            prev = points[0][1]
            for h in range(24):
                if h in last_in_hour:
                    per_hour[h] = max(0.0, last_in_hour[h] - prev)
                    prev = last_in_hour[h]
            return per_hour

        def update_hourly_profile(self):
            """EMA atnaujina valandinį profilį pagal vakardienos faktą ir
            normalizuoja (vidurkis = 1), kad predict_remaining_today semantika
            nesikeistų."""
            per_hour = self.hourly_consumption_yesterday()
            total = sum(per_hour.values())
            if len(per_hour) < PROFILE_MIN_HOURS or total < PROFILE_MIN_TOTAL:
                self.log(f"[PROFILIS] Nepakanka duomenų ({len(per_hour)} val., "
                         f"{total:.1f} kWh) — profilis nekeičiamas")
                return
            profile = self.model.get("hourly_profile") or self.default_hourly_profile()
            mean_kwh = total / len(per_hour)
            for h, kwh in per_hour.items():
                target = kwh / mean_kwh
                old = float(profile.get(str(h), 1.0))
                profile[str(h)] = old * (1 - PROFILE_ALPHA) + target * PROFILE_ALPHA
            mean_f = sum(float(v) for v in profile.values()) / len(profile)
            if mean_f > 0:
                for k in profile:
                    profile[k] = round(float(profile[k]) / mean_f, 3)
            self.model["hourly_profile"] = profile
            self.model["profile_days"] = self.model.get("profile_days", 0) + 1
            self.log(f"[PROFILIS] Valandinis profilis atnaujintas "
                     f"({len(per_hour)} val., diena #{self.model['profile_days']})")

        def fetch_daily_temps(self):
            """Grąžina {"YYYY-MM-DD": (t_mean, t_max)} ~92 praėjusioms paroms ir
            artimiausioms 2 d. iš Open-Meteo. Tuščias dict — jei tinklas nepasiekiamas."""
            try:
                with urllib.request.urlopen(WEATHER_URL, timeout=15) as resp:
                    daily = json.load(resp)["daily"]
                return {
                    day: (daily["temperature_2m_mean"][i], daily["temperature_2m_max"][i])
                    for i, day in enumerate(daily["time"])
                }
            except Exception as e:  # tinklo/API klaida neturi versti update_model
                self.log(f"[MODEL] Orų duomenų nepavyko gauti: {e}", level="WARNING")
                return {}

        def enrich_history_with_weather(self, history):
            temps = self.fetch_daily_temps()
            if not temps:
                return
            added = 0
            for h in history:
                if "t_mean" in h:
                    continue
                t = temps.get(h.get("date"))
                if t and t[0] is not None:
                    h["t_mean"] = round(t[0], 1)
                    if t[1] is not None:
                        h["t_max"] = round(t[1], 1)
                    added += 1
            if added:
                self.log(f"[MODEL] Temperatūros pridėtos {added} paroms")

        def recompute_from_history(self):
            """
            daily_avg ir savaitės koeficientai iš realios paros istorijos.

            Mediana vietoj EMA: po kelių aukšto vartojimo dienų EMA vidurkis
            likdavo išpūstas savaitėmis (2026-07-05..07 prognozės +21..+35 %),
            mediana atspari vienetiniams šuoliams. Savaitės koeficientai mokomi
            iš istorijos su susitraukimu (shrinkage) link 1.0 — hardcoded
            numatytieji neatitiko šio namo (pvz., šeštadienio 0.85, nors realiai
            tai buvo didžiausia savaitės diena).
            """
            history = self.model.get("history", [])
            if len(history) < HISTORY_MIN_DAYS:
                self.model["data_days"] = len(history)
                return False

            normalized = []
            for item in history:
                try:
                    day = datetime.strptime(item["date"], "%Y-%m-%d")
                    raw_kwh = float(item["kwh"])
                    season = self.month_to_season(day.month)
                    season_factor = float(
                        self.model["season_factors"].get(season, 1.0)
                    ) or 1.0
                    normalized.append({
                        "date": item["date"],
                        "value": raw_kwh / season_factor,
                        "raw_kwh": raw_kwh,
                    })
                except (KeyError, TypeError, ValueError):
                    continue

            stats = robust_consumption_statistics(
                normalized,
                median_window_days=MEDIAN_WINDOW_DAYS,
            )
            if stats is None:
                return False

            self.model["weekday_factors"] = stats["weekday_factors"]
            self.model["daily_avg"] = round(stats["daily_avg"], 3)
            self.model["data_days"] = len(history)
            self.model["usable_days"] = stats["usable_days"]
            self.model["anomaly_days"] = stats["anomaly_days"]
            self.model["model_version"] = 2
            if stats["anomaly_days"]:
                self.log(
                    "[MODEL] Į robust vidurkį neįtrauktos anomalios paros: "
                    + ", ".join(stats["anomaly_days"]),
                    level="WARNING",
                )
            return True

        # ============================================================
        #  HA SENSORIAI
        # ============================================================

        def update_ha_sensors(self, kwargs):
            """Eksponuoja prognozės sensoriaus reikšmes į HA."""
            remaining = self.predict_remaining_today()
            tomorrow  = self.predict_tomorrow()
            daily     = self.predict_daily()

            # str() būtina: AppDaemon 4.5.13 clean_http_kwargs() išmeta skaitinį 0
            # iš POST payload (0.0 == False), tada HA grąžina 400 "No state specified".
            self.set_state(
                OUTPUT["remaining"],
                state=str(round(remaining, 2)),
                attributes={"unit_of_measurement": "kWh", "friendly_name": f"{SITE_LABEL}: likęs suvartojimas šiandien"}
            )
            self.set_state(
                OUTPUT["tomorrow"],
                state=str(round(tomorrow, 2)),
                attributes={"unit_of_measurement": "kWh", "friendly_name": f"{SITE_LABEL}: rytojaus suvartojimo prognozė"}
            )
            self.set_state(
                OUTPUT["daily_avg"],
                state=str(round(daily, 2)),
                attributes={"unit_of_measurement": "kWh", "friendly_name": f"{SITE_LABEL}: dienos suvartojimo vidurkis"}
            )

            # 2026-07-23: profilio ATVAIZDAVIMUI dashborde (ApexCharts). Publikuojam
            # laukiamą kWh pasiskirstymą pagal valandą ir pagal savaitės dieną.
            prof = self.model.get("hourly_profile") or self.default_hourly_profile()
            tot = sum(float(v) for v in prof.values()) or 24.0
            hourly_kwh = [round(daily * float(prof.get(str(h), 1.0)) / tot, 2)
                          for h in range(24)]
            wf = self.model.get("weekday_factors", {})
            d_avg = float(self.model.get("daily_avg", 0))
            weekday_kwh = [round(d_avg * float(wf.get(str(d), 1.0)), 1) for d in range(7)]
            self.set_state(
                OUTPUT["profile"],
                state=str(self.model.get("profile_days", 0)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: vartojimo profilis",
                    "icon": "mdi:chart-bar",
                    "hourly_kwh": hourly_kwh,       # 24 reikšmės (0..23 val.)
                    "weekday_kwh": weekday_kwh,     # 7 reikšmės (Pr..Sk)
                    "daily_avg": round(d_avg, 1),
                    "data_days": self.model.get("data_days", 0),
                    "usable_days": self.model.get("usable_days", 0),
                    "anomaly_days": self.model.get("anomaly_days", []),
                    "anomaly_count": len(self.model.get("anomaly_days", [])),
                    "model_version": self.model.get("model_version", 1),
                    "profile_days": self.model.get("profile_days", 0),
                }
            )

    return ConsumptionModel
