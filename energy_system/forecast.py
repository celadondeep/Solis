"""Solcast korekcijos, gamybos lango ir baterijos vietos periferija."""

import json
import os
from datetime import datetime, timedelta


def build_forecast_mixin(profile):
    KWH_PER_SOC = profile["KWH_PER_SOC"]
    ESO_EXPORT_LIMIT_KW = profile["ESO_EXPORT_LIMIT_KW"]
    CONS_BASE_KW = profile["CONS_BASE_KW"]
    PLAN_MARGIN = profile["PLAN_MARGIN"]
    PLAN_HARD_FLOOR = profile["PLAN_HARD_FLOOR"]
    CORRECTION_FILE = profile["CORRECTION_FILE"]
    TARGET_SOC_FILE = profile["TARGET_SOC_FILE"]
    CORRECTION_ALPHA = profile["CORRECTION_ALPHA"]
    CORRECTION_MIN = profile["CORRECTION_MIN"]
    CORRECTION_MAX = profile["CORRECTION_MAX"]
    HOURLY_MIN_DAYS = profile["HOURLY_MIN_DAYS"]
    HOURLY_FC_MIN_KWH = profile["HOURLY_FC_MIN_KWH"]
    HOURLY_RATIO_MIN = profile["HOURLY_RATIO_MIN"]
    HOURLY_RATIO_MAX = profile["HOURLY_RATIO_MAX"]
    HOURLY_FACTOR_MIN = profile["HOURLY_FACTOR_MIN"]
    HOURLY_FACTOR_MAX = profile["HOURLY_FACTOR_MAX"]
    MORNING_PV_THRESHOLD_KW = profile["MORNING_PV_THRESHOLD_KW"]
    MORNING_ON_MARGIN_MIN = profile["MORNING_ON_MARGIN_MIN"]
    SENSOR = profile["SENSOR"]
    OUTPUT = profile["OUTPUT"]

    class ForecastMixin:

        def load_correction(self):
            """Įkelia korekcijos koeficientą iš failo arba grąžina default."""
            try:
                if os.path.exists(CORRECTION_FILE):
                    with open(CORRECTION_FILE, "r") as f:
                        data = json.load(f)
                        self.log(f"Solcast korekcija įkelta: {data.get('factor', 1.0)} "
                                 f"({data.get('days', 0)} d. duomenys)")
                        return data
            except Exception as e:
                self.log(f"Korekcijos įkėlimo klaida: {e}", level="WARNING")
            return {"factor": 1.0, "days": 0, "updated": None}


        def save_correction(self):
            try:
                self.correction["updated"] = datetime.now().isoformat()
                with open(CORRECTION_FILE, "w") as f:
                    json.dump(self.correction, f, indent=2)
            except Exception as e:
                self.log(f"Korekcijos išsaugojimo klaida: {e}", level="WARNING")


        def load_target_soc(self):
            """Atstato paskutinį target_soc po restarto. Reikšmė galioja 24 val. —
            senesnė reiškia, kad sistema ilgai stovėjo, tada saugiau 100%."""
            try:
                if os.path.exists(TARGET_SOC_FILE):
                    with open(TARGET_SOC_FILE, "r") as f:
                        data = json.load(f)
                    saved = datetime.fromisoformat(data["updated"])
                    age_h = (datetime.now() - saved).total_seconds() / 3600
                    target = int(data["target_soc"])
                    if age_h <= 24 and PLAN_HARD_FLOOR <= target <= 100:
                        self.log(f"Target SOC atstatytas iš failo: {target}% "
                                 f"(išsaugota prieš {age_h:.1f} val.)")
                        return target
            except Exception as e:
                self.log(f"Target SOC įkėlimo klaida: {e}", level="WARNING")
            return 100


        def save_target_soc(self):
            try:
                with open(TARGET_SOC_FILE, "w") as f:
                    json.dump({"target_soc": int(self.last_target_soc),
                               "updated": datetime.now().isoformat()}, f, indent=2)
            except Exception as e:
                self.log(f"Target SOC išsaugojimo klaida: {e}", level="WARNING")


        def corrected_kwh(self, value):
            """Pritaiko adaptyvų korekcijos koeficientą Solcast prognozei."""
            return value * self.correction.get("factor", 1.0)


        def hourly_ready(self):
            return self.correction.get("hourly_days", 0) >= HOURLY_MIN_DAYS


        def hourly_factor(self, hour):
            """Valandos koeficientas, kol nesubrendęs — globalus."""
            if self.hourly_ready():
                return float(self.correction.get("hourly_factors", {})
                             .get(str(hour), self.correction.get("factor", 1.0)))
            return self.correction.get("factor", 1.0)


        def _corrected_series_kwh(self, entity_key, only_future):
            """kWh suma iš detailedForecast su valandiniais koeficientais.
            None — jei atributo nėra (tada kviečiantis krenta į skaliarinį kelią)."""
            detailed = self.get_state(SENSOR[entity_key], attribute="detailedForecast")
            if not detailed:
                return None
            now = datetime.now().astimezone()
            total = 0.0
            for period in detailed:
                try:
                    start = datetime.fromisoformat(period["period_start"]).astimezone()
                    if only_future and start + timedelta(minutes=30) <= now:
                        continue
                    total += (float(period.get("pv_estimate", 0)) * 0.5
                              * self.hourly_factor(start.hour))
                except (ValueError, TypeError, KeyError):
                    continue
            return total


        def corrected_remaining_today(self):
            """Likusi šiandienos prognozė kWh (valandiniai koeficientai, jei subrendę)."""
            if self.hourly_ready():
                v = self._corrected_series_kwh("solcast_today_total", only_future=True)
                if v is not None:
                    return v
            return self.corrected_kwh(self.get_float("solcast_today"))


        def corrected_tomorrow(self):
            """Rytojaus prognozė kWh (valandiniai koeficientai, jei subrendę)."""
            if self.hourly_ready():
                v = self._corrected_series_kwh("solcast_tomorrow", only_future=False)
                if v is not None:
                    return v
            return self.corrected_kwh(self.get_float("solcast_tomorrow"))


        def snapshot_today_forecast(self, kwargs):
            """04:40: įšaldo šiandienos pusvalandinę prognozę mokymuisi 23:50."""
            detailed = self.get_state(SENSOR["solcast_today_total"],
                                      attribute="detailedForecast")
            if not detailed:
                self.log("[KOREKCIJA] Snapshot nepavyko — nėra detailedForecast",
                         level="WARNING")
                return
            today = datetime.now().astimezone().date().isoformat()
            periods = {}
            for period in detailed:
                try:
                    start = datetime.fromisoformat(period["period_start"]).astimezone()
                    if start.date().isoformat() == today:
                        periods[start.isoformat()] = float(period.get("pv_estimate", 0))
                except (ValueError, TypeError, KeyError):
                    continue
            self.correction["snapshot"] = {"date": today, "periods": periods}
            self.save_correction()
            self.log(f"[KOREKCIJA] Rytinis snapshot: {len(periods)} periodų")


        def hourly_actual_pv(self):
            """Šiandienos gamyba pavalandžiui {val: kWh} iš kumuliacinio pv_today
            (recorder istorija). Valandos be įrašų praleidžiamos."""
            start = datetime.now().astimezone().replace(hour=0, minute=0,
                                                        second=0, microsecond=0)
            try:
                hist = self.get_history(entity_id=SENSOR["pv_today"], start_time=start)
            except Exception as e:  # noqa: BLE001
                self.log(f"[KOREKCIJA] get_history klaida: {e}", level="WARNING")
                return {}
            if not hist or not hist[0]:
                return {}
            # Paskutinė kumuliacinė reikšmė kiekvienoje valandoje (chronologiškai
            # paskutinis įrašas laimi), tada deltos tarp valandų.
            last_in_hour = {}
            for s in hist[0]:
                try:
                    # 2026-07-23: AppDaemon get_history grąžina last_changed kaip
                    # datetime OBJEKTĄ (ne ISO tekstą) → fromisoformat mesdavo
                    # TypeError kiekvienam taškui → valandinė korekcija nesimokė.
                    lc = s.get("last_changed") or s.get("last_updated")
                    t = (lc if isinstance(lc, datetime)
                         else datetime.fromisoformat(str(lc)))
                    t = t.astimezone()
                    v = float(s["state"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if t >= start:
                    last_in_hour[t.hour] = v
            per_hour = {}
            prev = 0.0
            for h in range(24):
                if h in last_in_hour:
                    per_hour[h] = max(0.0, last_in_hour[h] - prev)
                    prev = last_in_hour[h]
            return per_hour


        def update_hourly_factors(self):
            """23:50: EMA atnaujina valandinius koeficientus pagal snapshot vs faktą."""
            today = datetime.now().astimezone().date().isoformat()
            snap = self.correction.get("snapshot") or {}
            periods = snap.get("periods") if snap.get("date") == today else None
            source = "snapshot"
            if not periods:
                # Fallback — vakarinis atributas (jau prisitaikęs, mokymas silpnesnis)
                source = "vakarinis atributas"
                detailed = self.get_state(SENSOR["solcast_today_total"],
                                          attribute="detailedForecast") or []
                periods = {}
                for period in detailed:
                    try:
                        start = datetime.fromisoformat(period["period_start"]).astimezone()
                        if start.date().isoformat() == today:
                            periods[start.isoformat()] = float(period.get("pv_estimate", 0))
                    except (ValueError, TypeError, KeyError):
                        continue
            if not periods:
                return
            fc_by_hour = {}
            for iso, kw in periods.items():
                h = datetime.fromisoformat(iso).hour
                fc_by_hour[h] = fc_by_hour.get(h, 0.0) + kw * 0.5
            actual = self.hourly_actual_pv()
            if not actual:
                return
            factors = self.correction.setdefault("hourly_factors", {})
            updated = 0
            for h, fc in sorted(fc_by_hour.items()):
                if fc < HOURLY_FC_MIN_KWH or h not in actual:
                    continue
                ratio = max(HOURLY_RATIO_MIN, min(actual[h] / fc, HOURLY_RATIO_MAX))
                old = float(factors.get(str(h), self.correction.get("factor", 1.0)))
                new = old * (1 - CORRECTION_ALPHA) + ratio * CORRECTION_ALPHA
                factors[str(h)] = round(
                    max(HOURLY_FACTOR_MIN, min(new, HOURLY_FACTOR_MAX)), 3)
                updated += 1
            if updated:
                self.correction["hourly_days"] = self.correction.get("hourly_days", 0) + 1
                self.log(f"[KOREKCIJA] Valandiniai koeficientai ({source}): "
                         f"atnaujinta {updated} val., diena #{self.correction['hourly_days']}")


        def update_forecast_correction(self, kwargs):
            """
            Kasdien 23:50: atnaujina korekcijos koeficientą pagal šios dienos
            faktas/prognozė santykį (EMA, α=0.2). Santykis ribojamas 0.5–1.5,
            kad viena anomali diena nesugriautų koeficiento.
            """
            actual   = self.get_float("pv_today")
            forecast = self.get_float("solcast_today_total")

            if forecast < 1.0 or actual <= 0:
                self.log(f"[KOREKCIJA] Nepakanka duomenų (faktas {actual:.1f}, "
                         f"prognozė {forecast:.1f}) — koeficientas nekeičiamas.")
                return

            ratio = max(0.5, min(actual / forecast, 1.5))
            old   = self.correction.get("factor", 1.0)
            new   = old * (1 - CORRECTION_ALPHA) + ratio * CORRECTION_ALPHA
            new   = max(CORRECTION_MIN, min(new, CORRECTION_MAX))

            self.correction["factor"] = round(new, 4)
            self.correction["days"]   = self.correction.get("days", 0) + 1
            self.update_hourly_factors()
            self.save_correction()

            self.log(f"[KOREKCIJA] Faktas {actual:.1f} / prognozė {forecast:.1f} "
                     f"= {ratio:.2f} → koeficientas {old:.3f} → {new:.3f}")

        # ============================================================
        #  INVERTERIO NAKTIES EKONOMIKA
        # ============================================================


        def _production_window(self, day):
            """Grąžina konkrečios dienos (on, off) pagal Solcast periodus."""
            now = datetime.now().astimezone()
            target_date = now.date() if day == "today" else (
                now + timedelta(days=1)
            ).date()
            entity = (
                SENSOR["solcast_today_total"]
                if day == "today"
                else SENSOR["solcast_tomorrow"]
            )
            detailed = self.get_state(entity, attribute="detailedForecast") or []
            starts = []
            for period in detailed:
                try:
                    start = datetime.fromisoformat(
                        str(period["period_start"])
                    ).astimezone()
                    power = float(period.get("pv_estimate", 0))
                except (KeyError, ValueError, TypeError):
                    continue
                if start.date() == target_date and power >= MORNING_PV_THRESHOLD_KW:
                    starts.append(start)

            if not starts:
                return None, None
            on_time = min(starts) - timedelta(minutes=MORNING_ON_MARGIN_MIN)
            off_time = max(starts) + timedelta(
                minutes=30 + MORNING_ON_MARGIN_MIN
            )
            return on_time, off_time


        def get_morning_on_time(self):
            """Artimiausia gamybos pradžia; praėjęs laikas neperstumiamas."""
            now = datetime.now().astimezone()
            today_on, _ = self._production_window("today")
            if today_on is not None and today_on > now:
                return today_on
            tomorrow_on, _ = self._production_window("tomorrow")
            return tomorrow_on or today_on


        def is_production_time(self):
            """Nenaudoja kitos elektrinės PV sensoriaus."""
            pv_w = self.get_optional_float("pv_power")
            if pv_w is not None and pv_w / 1000.0 >= MORNING_PV_THRESHOLD_KW:
                return True

            now = datetime.now().astimezone()
            on_time, off_time = self._production_window("today")
            if on_time is not None and off_time is not None:
                return on_time <= now <= off_time
            return self.get_state(SENSOR["sun"]) == "above_horizon"


        def get_evening_off_time(self):
            """Šiandienos gamybos pabaiga; grąžinama ir jai jau praėjus."""
            _on_time, off_time = self._production_window("today")
            return off_time


        def battery_room_needed(self, day):
            """
            REALIZAVIMO kriterijus (2026-07-14): kiek kWh baterijos vietos reikia,
            kad VISA prognozuojama saulė būtų realizuota — sugerta namų apkrovos,
            ESO 1 kW eksporto arba baterijos, be nukirpimo.

            Kiekvienam Solcast 30 min periodui: kas iš PV galios (koreguotos ir su
            PLAN_MARGIN atsarga; šiandienai dar × intradienos santykis) netelpa į
            bazinę namų apkrovą (CONS_BASE_KW) + 1 kW eksportą, PRIVALO tilpti į
            bateriją. Vartojimo prognozė plane neužskaitoma — tik bonusas.

            day: "today" (nuo dabar iki paros galo) arba "tomorrow" (visa para).
            Grąžina (reikia_kwh, eksportas_kwh, pv_plan_kwh).
            """
            # Valandos koeficientas taikomas kiekvienam periodui atskirai
            # (hourly_factor), čia — tik bendroji marža ir intradienos santykis.
            margin = PLAN_MARGIN
            now = datetime.now().astimezone()

            if day == "today":
                entity = SENSOR["solcast_today_total"]
                # Intradienos santykis: jei gamyba jau lenkia prognozę (kaip
                # 07-13: faktas 128 %), likusi diena planuojama pagal faktą.
                intraday = self.get_sensor_float(SENSOR["intraday_ratio"],
                                                 default=1.0)
                margin *= max(intraday, 1.0)   # tik didina — mažėjimą dengia marža
                cons = self.get_consumption_remaining_today()
                hours_left = max(1.0, 24.0 - now.hour - now.minute / 60.0)
                load_kw = cons / hours_left
            else:
                entity = SENSOR["solcast_tomorrow"]
                load_kw = self.get_daily_consumption() / 24.0
            # Nuosaikiai: užskaitoma tik garantuota bazinė apkrova — prognozės
            # vidurkis perdėtai „sugeria" vidurdienio PV, kai vartojimas vakarinis.
            load_kw = min(load_kw, CONS_BASE_KW)

            detailed = self.get_state(entity, attribute="detailedForecast") or []
            if not detailed:
                self.log(f"[VIETA] {entity} detailedForecast nepasiekiamas — "
                         f"vietos poreikis 0 (saugu: neiškrauna)", level="WARNING")
                return 0.0, 0.0, 0.0

            need = export = pv_total = 0.0
            for period in detailed:
                try:
                    start = datetime.fromisoformat(str(period["period_start"]))
                    pv = (float(period.get("pv_estimate", 0))
                          * self.hourly_factor(start.astimezone().hour) * margin)
                except (KeyError, ValueError, TypeError):
                    continue
                if day == "today" and start < now - timedelta(minutes=30):
                    continue
                pv_total += pv * 0.5
                surplus_kw = pv - load_kw
                if surplus_kw <= 0:
                    continue
                export += min(surplus_kw, ESO_EXPORT_LIMIT_KW) * 0.5
                need += max(0.0, surplus_kw - ESO_EXPORT_LIMIT_KW) * 0.5

            return need, export, pv_total


        def publish_realization(self):
            """
            sensor.energy_manager_room_shortfall — kiek kWh likusios šiandienos
            saulės (su marža) dar netilptų į laisvą baterijos vietą + namus +
            1 kW eksportą. > 0 reiškia nukirpimo riziką: vietą daro dienos
            automatika (solis_daytime_feedin_tou) ir ryto patikra.
            """
            soc = self.get_float("soc", default=100.0)
            need, export_est, pv_plan = self.battery_room_needed("today")
            headroom = max(0.0, (100.0 - soc) * KWH_PER_SOC)
            shortfall = need - headroom
            self.set_state(
                OUTPUT["room_shortfall"],
                state=str(round(shortfall, 2)),
                attributes={
                    "friendly_name": "Vietos trūkumas saulei (šiandien)",
                    "unit_of_measurement": "kWh",
                    "state_class": "measurement",
                    "icon": "mdi:battery-alert" if shortfall > 0 else "mdi:battery-check",
                    "reikia_vietos_kwh": round(need, 2),
                    "laisva_vieta_kwh": round(headroom, 2),
                    "pv_planas_kwh": round(pv_plan, 2),
                    "tiketinas_eksportas_kwh": round(export_est, 2),
                    "marza": PLAN_MARGIN,
                }
            )


    return ForecastMixin
