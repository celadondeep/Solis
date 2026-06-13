"""
energy_manager.py — Išmanus energijos valdymo AppDaemon skriptas
================================================================
Autorius: sugeneruota pagal namų sistemos parametrus
Versija:  1.0

Sistemos parametrai:
  Kaupiklis:  16 kWh
  Inverteris: 10 kW (Solis S6)
  Paneliai:   5.34 kWp
  ESO limitas: 1 kW atidavimas į tinklą
  Boileris:   2.2 kW (ESP32 + SSR relė)

Logikos lygiai:
  1. Avariniai patikrinimai  — kiekvienam ciklui
  2. Strateginis ciklas      — kas 30 min (Solcast prognozė)
  3. Taktinis ciklas         — kas 10 sek (realūs duomenys)
  4. Vakaro iškrovimas       — kas 30 min nuo 18:00

Reikalingi HA sensoriai (pritaikyk prie savo):
  sensor.solcast_forecast_remaining_today   — likusi šiandienos prognozė kWh
  sensor.solcast_forecast_tomorrow          — rytojaus prognozė kWh
  sensor.solis_battery_soc                  — kaupiklio SOC %
  sensor.solis_pv_power                     — saulės galia W
  sensor.solis_house_load                   — namų apkrova W
  sensor.solis_grid_power                   — tinklo galia W (+ eksportas, - importas)
  sensor.boiler_temperature                 — boilerio temperatūra °C (ESP32)
  switch.boiler_switch                      — boilerio jungiklis (ESP32)
  input_select.energy_season               — sezonas: žiema/pavasaris/vasara/ruduo
  input_boolean.storm_mode                  — audros / ESO režimas
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, time, timedelta
import json
import os
import statistics


# ============================================================
#  KONFIGŪRACIJA — pritaikyk prie savo sistemos
# ============================================================

# Sistemos parametrai
# Naudingoji talpa suderinta su automations.yaml/configuration.yaml:
# 14.2 kWh tenka SOC ruožui 100%→11% (dugnas 11%), 1% SOC = 0.15955 kWh.
BATTERY_USABLE_KWH   = 14.2       # naudingoji kaupiklio talpa kWh (100%→11%)
KWH_PER_SOC          = BATTERY_USABLE_KWH / 89.0   # kWh viename SOC %
BOILER_POWER_KW      = 2.2        # boilerio galia kW
ESO_EXPORT_LIMIT_KW  = 1.0        # max atidavimas į tinklą kW
# Inverterio savivartojimas. Solis sensoriai (household_load_power,
# yesterday_energy_consumption) jo NEMATO — matuojama tik namų apkrova, todėl
# poreikio prognozės be šios pataisos ~1.2–1.5 kWh/naktį per mažos.
# Išmatuota 2026-06-11 naktį: baterija davė vid. ~175 W daugiau nei namai
# vartojo; fiksuojame 140 W (likutis — apkrovai proporcingi nuostoliai).
INVERTER_SELF_KW     = 0.14       # inverterio savivartojimas kW (24/7)

# Inverterio nakties ekonomika. Įjungtas be gamybos inverteris ima ~130 W
# (iš baterijos), išjungtas (power_state off) valdymo plokštė ima ~30 W iš
# tinklo. Naudojama skaičiuojant, kada apsimoka išjungti nakčiai.
INVERTER_IDLE_W      = 130.0      # įjungtas, be gamybos (iš baterijos)
INVERTER_OFF_W       = 30.0       # išjungtas, valdymo plokštė (iš tinklo)

# Elektros kainos — skaitomos iš input_number (dashboard), šie tik fallback
# kol input_number nenustatytas (reikšmė ≤ 0.001 laikoma nenustatyta).
PRICE_BUY_DEFAULT    = 0.18       # €/kWh perkant iš tinklo
PRICE_SELL_DEFAULT   = 0.08       # €/kWh parduodant į tinklą

# Adaptyvi Solcast korekcija: kasdien 23:50 koeficientas atnaujinamas pagal
# faktas/prognozė santykį (EMA). Visi strateginiai skaičiavimai naudoja
# koreguotą prognozę. Koeficientas ribojamas, kad vienas anomalus
# (pvz. sniego ant panelių) nesugriautų prognozių.
# Keliai per __file__ — AppDaemon konteineryje /config rodo į addon'o vidinį
# katalogą, todėl hardcoded /config/appdaemon/... ten neegzistuoja.
_APP_DIR             = os.path.dirname(os.path.abspath(__file__))
CORRECTION_FILE      = os.path.join(_APP_DIR, "forecast_correction.json")
# Paskutinis target_soc — išsaugomas, kad HA/AppDaemon restartas vakare ar
# naktį negrąžintų tikslo į 100% iki kito 18:00 perskaičiavimo.
TARGET_SOC_FILE      = os.path.join(_APP_DIR, "target_soc.json")
CORRECTION_ALPHA     = 0.2        # EMA glodinimo koeficientas
CORRECTION_MIN       = 0.7
CORRECTION_MAX       = 1.3

# Ryto įjungimas: PV galios slenkstis, nuo kurio gamyba dengia inverterio
# savivartojimą ir laikyti jį įjungtą tampa pelninga (~100 W skirtumas
# tarp idle 130 W ir off 30 W).
MORNING_PV_THRESHOLD_KW = 0.1
MORNING_ON_MARGIN_MIN   = 15      # įjungti tiek min anksčiau nei slenkstis

# Sezoniniai SOC minimumai %
SEASON_SOC_MIN = {
    "žiema":      80,
    "pavasaris":  60,
    "vasara":     20,
    "ruduo":      60,
}

# Boilerio temperatūros ribos
BOILER_TEMP_MAX      = 75.0   # aukščiau — boileris OFF
BOILER_TEMP_WINTER   = 70.0   # žiemos tikslas
BOILER_TEMP_SUMMER   = 55.0   # vasaros tikslas

# Strateginės ribos kWh
BALANCE_MIN_KWH      = 0.0    # balansas turi būti > 0 kad leistų boilerį
SOC_HIGH_THRESHOLD   = 90.0   # virš šio SOC — tikrinti ar galima leisti boilerį
SOC_TARGET_CHARGE    = 95.0   # tikslas po kurio laikome "pilną"

# Taktinės ribos
SOC_DROP_RATE_MAX    = 2.0    # max SOC kritimas % per 10 min
SURPLUS_MIN_KW       = 0.3    # minimalus perteklius kad boileris veiktų

# HA sensorių pavadinimai
SENSOR = {
    "solcast_today":     "sensor.solcast_pv_forecast_forecast_remaining_today",
    "solcast_tomorrow":  "sensor.solcast_pv_forecast_forecast_tomorrow",
    "soc":               "sensor.solis_s6_eh3p_battery_soc",
    "pv_power":          "sensor.solis_s6_eh3p_total_pv_power",
    "house_load":        "sensor.solis_s6_eh3p_household_load_power",
    "grid_power":        "sensor.solis_s6_eh3p_grid_power_net",
    "boiler_temp":       "sensor.boiler_temperature",       # TODO: ESP32 prijungus patikslinti
    "boiler_switch":     "switch.boiler_switch",             # TODO: ESP32 prijungus patikslinti
    "season":            "input_select.energy_season",
    "storm_mode":        "input_boolean.storm_mode",
    "inverter_temp":     "sensor.solis_s6_eh3p_temperature",
    "pv_today":          "sensor.solis_s6_eh3p_pv_today_energy_generation",
    "solcast_today_total": "sensor.solcast_pv_forecast_forecast_today",
    "battery_power":     "sensor.solis_s6_eh3p_battery_power_net",
    "power_state":       "switch.solis_s6_eh3p_power_state",
    "price_buy":         "input_number.electricity_price_buy",
    "price_sell":        "input_number.electricity_price_sell",
}

# PASTABA dėl inverterio valdymo: šis modulis inverterio NEvaldo tiesiogiai.
# Jis tik skaičiuoja ir publikuoja sensor.energy_manager_target_soc; patį TOU
# iškrovimą per LOKALŲ Modbus (solis_s6_eh3p_*) vykdo automations.yaml
# (solis_evening_discharge 20:00 + solis_tou_recalc_5min). Žemos baterijos
# apsaugą dienos metu vykdo automacijos solis_daytime_battery_protect (<13%)
# ir solis_daytime_export_resume (>30%). Cloud (solis_cloud_control_*)
# entity'ių nenaudojame — jų sistemoje nėra.

# Atsarginis vidutinis namų suvartojimas per dieną kWh — naudojamas TIK kai
# dinaminis vartojimo modelis (consumption_model.py) dar neturi reikšmės.
# Seed pagal realų ~13 kWh/parą (buvęs 8.0 stipriai per mažas).
DEFAULT_DAILY_CONSUMPTION = 13.0

# Vartojimo modelio sensoriai (publikuoja consumption_model.py)
CONSUMPTION_TOMORROW_SENSOR  = "sensor.consumption_forecast_tomorrow"
CONSUMPTION_REMAINING_SENSOR = "sensor.consumption_remaining_today"


# ============================================================
#  PAGRINDINĖ KLASĖ
# ============================================================

class EnergyManager(hass.Hass):

    def initialize(self):
        self.log("EnergyManager paleidžiamas...")

        # Vidinė būsena
        self.boiler_allowed      = False   # strateginis leidimas
        self.soc_history         = []      # SOC istorija greičio skaičiavimui
        self.last_strategic_run  = None
        self.last_decision       = "Paleidžiama..."
        self.last_balance        = 0.0
        self.last_surplus        = 0.0
        # Startinis tikslas: atstatomas iš failo, jei vakar/šiandien jau buvo
        # apskaičiuotas (kitaip restartas vakare užšaldytų 100% iki kito 18:00).
        # Jei failo nėra ar jis pasenęs — saugus 100% = „neiškrauti nieko".
        # (Anksčiau būdavo 0, dėl ko po restarto iškraudavo bateriją iki 0%.)
        self.last_target_soc     = self.load_target_soc()

        # Adaptyvi Solcast korekcija — koeficientas iš failo (default 1.0)
        self.correction = self.load_correction()

        # Korekcijos atnaujinimas kasdien 23:50 (prieš snapshot 23:55)
        self.run_daily(self.update_forecast_correction, time(23, 50))

        # Strateginis ciklas — kas 30 min; pirmą kartą po 5 sek
        self.run_in(self.strategic_cycle, 5)
        self.run_every(self.strategic_cycle, "now", 30 * 60)

        # Taktinis ciklas — kas 10 sek
        self.run_every(self.tactical_cycle, "now+15", 10)

        # Sensorių atnaujinimas — kas 5 min. Užtikrina, kad inverterio
        # temperatūra (ir kita būsena) grafike turėtų taškus kas 5 min, o ne
        # kas 30 min, kai boileris neaktyvus ir taktinis ciklas išeina anksčiau
        # nepasiekęs publish_status. Boilerio valdymo logikos neliečia.
        self.run_every(self.refresh_publish, "now+20", 5 * 60)

        # Vakaro iškrovimo ciklas — kas 30 min nuo 18:00 iki 23:00
        self.run_daily(self.evening_discharge_cycle, time(18, 0))
        self.run_daily(self.evening_discharge_cycle, time(18, 30))
        self.run_daily(self.evening_discharge_cycle, time(19, 0))
        self.run_daily(self.evening_discharge_cycle, time(19, 30))
        self.run_daily(self.evening_discharge_cycle, time(20, 0))
        self.run_daily(self.evening_discharge_cycle, time(20, 30))
        self.run_daily(self.evening_discharge_cycle, time(21, 0))
        self.run_daily(self.evening_discharge_cycle, time(21, 30))
        self.run_daily(self.evening_discharge_cycle, time(22, 0))
        self.run_daily(self.evening_discharge_cycle, time(22, 30))

        # Sukuriami sensoriai iš karto, kad dashboard nerodytų "entity not found"
        self.set_state("sensor.energy_manager_surplus_now", state="0.0",
                       attributes={"friendly_name": "Saulės perteklius (dabar)",
                                   "unit_of_measurement": "kW", "icon": "mdi:solar-panel"})
        self.set_state("sensor.energy_manager_target_soc", state=str(self.last_target_soc),
                       attributes={"friendly_name": "Tikslinė SOC riba (Solis)",
                                   "unit_of_measurement": "%", "icon": "mdi:battery-charging-80",
                                   "device_class": "battery"})
        self.set_state("sensor.energy_manager_inverter_temp", state="0",
                       attributes={"friendly_name": "Inverterio temperatūra",
                                   "unit_of_measurement": "°C", "icon": "mdi:thermometer",
                                   "device_class": "temperature"})
        self.set_state("sensor.solcast_correction_factor",
                       state=str(round(self.correction.get("factor", 1.0), 3)),
                       attributes={"friendly_name": "Solcast korekcijos koeficientas",
                                   "icon": "mdi:tune-variant"})

        self.log("EnergyManager paleistas sėkmingai.")


    # ============================================================
    #  BŪSENOS PUBLIKAVIMAS Į HA SENSORIUS
    # ============================================================

    def refresh_publish(self, kwargs):
        """Periodinis (kas 5 min) būsenos publikavimas — kad temperatūros ir
        kitų sensorių grafikai turėtų reguliarius taškus net kai taktinis
        ciklas išeina anksčiau."""
        self.publish_status()
        self.publish_night_economics()

    def publish_status(self):
        """Eksportuoja vidinę automacijos būseną kaip HA sensorius."""
        # SVARBU: visi skaitiniai state perduodami kaip str(). AppDaemon 4.5.13
        # clean_http_kwargs() filtruoja reikšmes per `v not in (None, False)`,
        # o Python'e 0.0 == False, tad skaitinis 0 tyliai dingsta iš POST ir
        # HA grąžina 400 "No state specified". String "0.0" pereina saugiai.
        soc           = self.get_float("soc")
        boiler_temp   = self.get_float("boiler_temp", default=20.0)
        inverter_temp = self.get_float("inverter_temp", default=0.0)
        pv_kw         = self.get_float("pv_power") / 1000
        house_kw      = self.get_float("house_load") / 1000
        season        = self.get_season()
        soc_min       = self.get_season_soc_min()
        storm         = self.is_storm_mode()
        boiler_state  = self.get_state(SENSOR["boiler_switch"])
        solcast_t     = self.corrected_kwh(self.get_float("solcast_today"))
        solcast_tm    = self.corrected_kwh(self.get_float("solcast_tomorrow"))

        # PASTABA: sensor.energy_manager_status priklauso template sensoriui
        # (configuration.yaml — rodo inverterio režimą). Sprendimo tekstas
        # publikuojamas į ATSKIRĄ entity, kad du šaltiniai nesipjautų.
        self.set_state(
            "sensor.energy_manager_decision",
            # Būsena = paskutinis priimtas sprendimas (HA riboja iki 255 simb.)
            state=self.last_decision[:254],
            attributes={
                "friendly_name": "Energijos valdymas — sprendimas",
                "icon": "mdi:solar-power-variant",
                "automacija": "aktyvus",
                "strateginis_leidimas": "TAIP" if self.boiler_allowed else "NE",
                "audros_rezimas": "AKTYVUS" if storm else "neaktyvus",
                "sezonas": season,
                "soc_minimumas": f"{soc_min}%",
                "inverterio_temperatura": f"{inverter_temp:.1f}°C",
            }
        )

        self.set_state(
            "sensor.energy_manager_balance",
            state=str(round(self.last_balance, 2)),
            attributes={
                "friendly_name": "Energijos balansas",
                "unit_of_measurement": "kWh",
                "icon": "mdi:scale-balance",
                # state_class reikalingas, kad recorder kauptų ilgalaikę
                # statistiką; device_class: energy čia negalimas (HA leidžia
                # tik su total/total_increasing, o balansas svyruoja).
                "state_class": "measurement",
                "solcast_siandiena": f"{solcast_t:.1f} kWh",
                "solcast_rytoj": f"{solcast_tm:.1f} kWh",
            }
        )

        # Momentinis perteklius (kW) — ATSKIRAS entity nuo template
        # sensor.energy_manager_surplus (likusios dienos perteklius kWh).
        self.set_state(
            "sensor.energy_manager_surplus_now",
            state=str(round(self.last_surplus, 2)),
            attributes={
                "friendly_name": "Saulės perteklius (dabar)",
                "unit_of_measurement": "kW",
                "icon": "mdi:solar-panel",
                "pv_galia": f"{pv_kw:.2f} kW",
                "namu_apkrova": f"{house_kw:.2f} kW",
            }
        )

        self.set_state(
            "sensor.energy_manager_target_soc",
            state=str(self.last_target_soc),
            attributes={
                "friendly_name": "Tikslinė SOC riba (Solis)",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-charging-80",
                "device_class": "battery",
            }
        )

        self.set_state(
            "sensor.energy_manager_inverter_temp",
            state=str(round(inverter_temp, 1)),
            attributes={
                "friendly_name": "Inverterio temperatūra",
                "unit_of_measurement": "°C",
                "icon": "mdi:thermometer",
                "device_class": "temperature",
            }
        )

        self.set_state(
            "sensor.energy_manager_boiler_status",
            state=boiler_state if boiler_state else "unknown",
            attributes={
                "friendly_name": "Boilerio valdymo būsena",
                "icon": "mdi:water-boiler",
                "temperatura": f"{boiler_temp:.1f}°C",
                "tikslas": f"{self.get_boiler_temp_target():.0f}°C",
                "strateginis_leidimas": "TAIP" if self.boiler_allowed else "NE",
            }
        )

    # ============================================================
    #  PAGALBINĖS FUNKCIJOS
    # ============================================================

    def get_float(self, sensor_name, default=0.0):
        """Gauna sensoriaus reikšmę kaip float, grąžina default jei klaida."""
        try:
            val = self.get_state(SENSOR[sensor_name])
            if val in (None, "unavailable", "unknown"):
                return default
            return float(val)
        except (ValueError, TypeError):
            self.log(f"Klaida skaitant {sensor_name}, naudojamas default {default}")
            return default

    def get_sensor_float(self, entity_id, default=0.0):
        """Kaip get_float, bet pagal pilną entity_id (ne iš SENSOR žodyno)."""
        try:
            val = self.get_state(entity_id)
            if val in (None, "unavailable", "unknown", ""):
                return default
            return float(val)
        except (ValueError, TypeError):
            return default

    def get_season(self):
        """Grąžina dabartinį sezoną."""
        season = self.get_state(SENSOR["season"])
        if season not in SEASON_SOC_MIN:
            # Auto sezonas pagal mėnesį jei input_select nepasirinktas
            month = datetime.now().month
            if month in (12, 1, 2):
                return "žiema"
            elif month in (3, 4, 5):
                return "pavasaris"
            elif month in (6, 7, 8):
                return "vasara"
            else:
                return "ruduo"
        return season

    def get_season_soc_min(self):
        """Grąžina sezono SOC minimumą %."""
        return SEASON_SOC_MIN.get(self.get_season(), 60)

    def is_storm_mode(self):
        """Tikrina ar įjungtas audros/ESO režimas."""
        return self.get_state(SENSOR["storm_mode"]) == "on"

    def get_boiler_temp_target(self):
        """Grąžina boilerio temperatūros tikslą pagal sezoną."""
        season = self.get_season()
        if season in ("žiema", "ruduo"):
            return BOILER_TEMP_WINTER
        return BOILER_TEMP_SUMMER

    def get_daily_consumption(self):
        """
        Grąžina prognozuojamą rytojaus suvartojimą kWh: namų poreikis iš
        vartojimo modelio (consumption_model.py, mokosi iš realios Solis
        istorijos) + inverterio savivartojimas (INVERTER_SELF_KW × 24 h),
        kurio Solis vartojimo sensoriai nemato.
        Jei modelio sensorius dar neprieinamas — naudoja DEFAULT_DAILY_CONSUMPTION.
        """
        base = self.get_sensor_float(
            CONSUMPTION_TOMORROW_SENSOR,
            default=DEFAULT_DAILY_CONSUMPTION,
        )
        return base + INVERTER_SELF_KW * 24

    def get_consumption_remaining_today(self):
        """
        Likęs suvartojimas šiandien kWh: namų poreikis iš vartojimo modelio
        (valandinis profilis) + inverterio savivartojimas likusioms valandoms.
        Atsarginis variantas: tolygus įvertis pagal likusias valandas.
        """
        now = datetime.now()
        hours_left = 24 - now.hour - (now.minute / 60)
        base_daily = self.get_sensor_float(
            CONSUMPTION_TOMORROW_SENSOR,
            default=DEFAULT_DAILY_CONSUMPTION,
        )
        fallback = base_daily / 24 * hours_left
        base = self.get_sensor_float(
            CONSUMPTION_REMAINING_SENSOR,
            default=round(fallback, 2),
        )
        return base + INVERTER_SELF_KW * hours_left

    # ============================================================
    #  ADAPTYVI SOLCAST KOREKCIJA
    # ============================================================

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
                if age_h <= 24 and 11 <= target <= 100:
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
        self.save_correction()

        self.log(f"[KOREKCIJA] Faktas {actual:.1f} / prognozė {forecast:.1f} "
                 f"= {ratio:.2f} → koeficientas {old:.3f} → {new:.3f}")

    # ============================================================
    #  INVERTERIO NAKTIES EKONOMIKA
    # ============================================================

    def get_price(self, key, default):
        """Elektros kaina iš input_number; fallback į konstantą kol nenustatyta."""
        val = self.get_sensor_float(SENSOR[key], default=default)
        return val if val > 0.001 else default

    def get_morning_on_time(self):
        """
        Apskaičiuoja optimalų inverterio įjungimo laiką iš Solcast
        pusvalandinės prognozės: pirmas intervalas, kai koreguota PV galia
        viršija MORNING_PV_THRESHOLD_KW, minus MORNING_ON_MARGIN_MIN min.
        Iki vidurdienio žiūri į šiandienos prognozę (aktualu prieš aušrą),
        po — į rytojaus. Grąžina aware datetime arba None.
        """
        now = datetime.now().astimezone()
        entity = (SENSOR["solcast_today_total"] if now.hour < 12
                  else SENSOR["solcast_tomorrow"])
        try:
            detailed = self.get_state(entity, attribute="detailedForecast")
            if not detailed:
                return None
            factor = self.correction.get("factor", 1.0)
            for period in detailed:
                if float(period.get("pv_estimate", 0)) * factor >= MORNING_PV_THRESHOLD_KW:
                    start = datetime.fromisoformat(period["period_start"])
                    on_time = start - timedelta(minutes=MORNING_ON_MARGIN_MIN)
                    # Jei laikas jau praėjęs (pvz. skaičiuojama po aušros) —
                    # įjungti tuoj pat, kad time-trigger dar suveiktų.
                    if on_time <= now:
                        on_time = now + timedelta(minutes=2)
                    return on_time
        except Exception as e:
            self.log(f"Ryto įjungimo laiko klaida: {e}", level="WARNING")
        return None

    def publish_night_economics(self):
        """
        Skaičiuoja ir publikuoja inverterio nakties ekonomiką:
          ĮJUNGTAS naktį: namai + ~130 W idle iš baterijos. Baterijos kWh
            vertė priklauso nuo to, ar rytoj baterija vis tiek prisipildys
            (perteklius → pardavimo kaina) ar ne (pirkimo kaina).
          IŠJUNGTAS: namai + ~30 W valdymo plokštė iš tinklo (pirkimo kaina).
        Publikuoja rekomendaciją, €/h palyginimą, numatomą išjungimo laiką
        (kada SOC pasieks tikslą) ir optimalų ryto įjungimo laiką.
        """
        load_w     = self.get_float("house_load")
        soc        = self.get_float("soc")
        target_soc = max(float(self.last_target_soc), 11.0)
        price_buy  = self.get_price("price_buy", PRICE_BUY_DEFAULT)
        price_sell = self.get_price("price_sell", PRICE_SELL_DEFAULT)
        power_on   = self.get_state(SENSOR["power_state"]) == "on"

        # Baterijos kWh vertė: ar rytojaus perteklius užpildys bateriją nuo
        # tikslinio SOC iki 95%? Jei taip — kiekviena naktį išleista kWh būtų
        # šiaip eksportuota (vertė = pardavimo kaina). Jei ne — jos vertė =
        # pirkimo kaina (rytoj vakare jos truks ir teks pirkti).
        tomorrow_corr  = self.corrected_kwh(self.get_float("solcast_tomorrow"))
        need_tomorrow  = self.get_daily_consumption()
        surplus_tom    = tomorrow_corr - need_tomorrow
        headroom_kwh   = max(0.0, (SOC_TARGET_CHARGE - target_soc) * KWH_PER_SOC)
        battery_refills = surplus_tom >= headroom_kwh
        batt_value     = price_sell if battery_refills else price_buy

        cost_on_h  = (load_w + INVERTER_IDLE_W) / 1000 * batt_value
        cost_off_h = (load_w + INVERTER_OFF_W) / 1000 * price_buy
        diff_h     = cost_on_h - cost_off_h     # >0 → išjungti apsimoka

        # Numatomas išjungimo laikas: kada baterija pasieks tikslinį SOC
        # dabartiniu iškrovimo greičiu (naktį ~namai + idle).
        eta_text = "—"
        if soc > target_soc + 1:
            # Realus iškrovimo greitis; jei baterija nekraunama/nesikrauna
            # (diena) — įvertis pagal naktinį scenarijų (namai + idle).
            batt_w = self.get_sensor_float(SENSOR["battery_power"])
            discharge_kw = (batt_w if batt_w > 50
                            else load_w + INVERTER_IDLE_W) / 1000
            if discharge_kw > 0.05:
                hours = (soc - target_soc) * KWH_PER_SOC / discharge_kw
                if hours < 24:
                    eta = datetime.now() + timedelta(hours=hours)
                    eta_text = eta.strftime("%H:%M")
        elif power_on:
            eta_text = "dabar (SOC ties tikslu)"

        if not power_on:
            recommendation = "Inverteris išjungtas 💤"
        elif diff_h > 0.001:
            recommendation = "Apsimoka išjungti — baterija jau ties tikslu" \
                if soc <= target_soc + 1 else \
                f"Išjungti apsimokės ~{eta_text} (pasiekus {target_soc:.0f}%)"
        else:
            recommendation = "Laikyti įjungtą — namai iš baterijos pigiau nei iš tinklo"

        # Nakties (8 val.) sutaupymas, jei išjungtume vietoj laikymo įjungto
        night_savings = max(diff_h, 0) * 8

        self.set_state(
            "sensor.inverter_night_economics",
            state=recommendation[:254],
            attributes={
                "friendly_name": "Inverterio nakties ekonomika",
                "icon": "mdi:power-sleep",
                "kaina_ijungtas_eur_h": round(cost_on_h, 4),
                "kaina_isjungtas_eur_h": round(cost_off_h, 4),
                "skirtumas_eur_h": round(diff_h, 4),
                "sutaupymas_nakti_eur": round(night_savings, 2),
                "baterijos_kwh_verte": f"{batt_value:.3f} €/kWh "
                    f"({'prisipildys rytoj — eksporto kaina' if battery_refills else 'nepilnės rytoj — pirkimo kaina'})",
                "namu_apkrova_w": round(load_w),
                "idle_w": INVERTER_IDLE_W,
                "isjungto_w": INVERTER_OFF_W,
            }
        )

        self.set_state(
            "sensor.inverter_shutdown_eta",
            state=eta_text,
            attributes={
                "friendly_name": "Numatomas inverterio išjungimas",
                "icon": "mdi:clock-end",
                "soc": soc,
                "tikslinis_soc": target_soc,
            }
        )

        on_time = self.get_morning_on_time()
        if on_time is not None:
            self.set_state(
                "sensor.inverter_morning_on_time",
                state=on_time.isoformat(),
                attributes={
                    "friendly_name": "Inverterio ryto įjungimas",
                    "device_class": "timestamp",
                    "icon": "mdi:weather-sunset-up",
                    "laikas": on_time.strftime("%H:%M"),
                    "pv_slenkstis_kw": MORNING_PV_THRESHOLD_KW,
                }
            )

        self.set_state(
            "sensor.solcast_correction_factor",
            state=str(round(self.correction.get("factor", 1.0), 3)),
            attributes={
                "friendly_name": "Solcast korekcijos koeficientas",
                "icon": "mdi:tune-variant",
                "dienos": self.correction.get("days", 0),
                "atnaujinta": self.correction.get("updated"),
                "rytoj_koreguota_kwh": round(tomorrow_corr, 1),
            }
        )

    def calculate_soc_drop_rate(self, current_soc):
        """
        Skaičiuoja SOC kritimo greitį % per 10 min.
        Naudoja paskutines 6 reikšmes (1 minutė).
        """
        self.soc_history.append(current_soc)
        if len(self.soc_history) > 6:
            self.soc_history.pop(0)

        if len(self.soc_history) < 2:
            return 0.0

        # SOC pokytis per turimas reikšmes
        drop = self.soc_history[0] - self.soc_history[-1]
        # Normalizuojame iki 10 min
        intervals = len(self.soc_history) - 1
        drop_per_10min = drop * (6 / intervals)
        return drop_per_10min

    def boiler_on(self, reason=""):
        """Įjungia boilerį. Kol ESP32 neprijungtas (entity nėra) — nieko nedaro."""
        if not self.entity_exists(SENSOR["boiler_switch"]):
            return
        current = self.get_state(SENSOR["boiler_switch"])
        if current != "on":
            self.turn_on(SENSOR["boiler_switch"])
            self.log(f"Boileris ĮJUNGTAS. Priežastis: {reason}")

    def boiler_off(self, reason=""):
        """Išjungia boilerį. Kol ESP32 neprijungtas (entity nėra) — nieko nedaro."""
        if not self.entity_exists(SENSOR["boiler_switch"]):
            return
        current = self.get_state(SENSOR["boiler_switch"])
        if current != "off":
            self.turn_off(SENSOR["boiler_switch"])
            self.log(f"Boileris IŠJUNGTAS. Priežastis: {reason}")


    # ============================================================
    #  1. AVARINIAI PATIKRINIMAI
    # ============================================================

    def emergency_checks(self, soc, boiler_temp):
        """
        Tikrina avarinius atvejus.
        Grąžina (True, priežastis) jei reikia SUSTABDYTI boilerį.
        """

        # Audros / ESO režimas
        if self.is_storm_mode():
            return True, "AUDROS režimas aktyvus — boileris blokuojamas"

        # SOC per žemas
        soc_min = self.get_season_soc_min()
        if soc <= soc_min:
            return True, f"SOC {soc:.1f}% <= sezono minimumas {soc_min}% — boileris blokuojamas"

        # Boileris per karštas
        if boiler_temp >= BOILER_TEMP_MAX:
            return True, f"Boilerio temperatūra {boiler_temp:.1f}°C >= {BOILER_TEMP_MAX}°C — boileris blokuojamas"

        return False, ""


    # ============================================================
    #  2. STRATEGINIS CIKLAS — kas 30 min
    # ============================================================

    def strategic_cycle(self, kwargs):
        """
        Strateginis ciklas — skaičiuoja energijos balansą
        ir nustato ar boileris apskritai gali veikti šiandien.
        """
        solcast_today    = self.corrected_kwh(self.get_float("solcast_today"))
        solcast_tomorrow = self.corrected_kwh(self.get_float("solcast_tomorrow"))
        soc              = self.get_float("soc")

        consumption_today    = self.get_consumption_remaining_today()
        consumption_tomorrow = self.get_daily_consumption()

        # Kiek trūksta iki 95% SOC (naudingoji talpa, 1% = 0.15955 kWh)
        soc_gap_kwh = max(0, (SOC_TARGET_CHARGE - soc) * KWH_PER_SOC)

        # Bendras energijos balansas
        total_available = solcast_today + solcast_tomorrow
        total_needed    = consumption_today + consumption_tomorrow + soc_gap_kwh
        balance         = total_available - total_needed

        self.log(
            f"[STRATEGINIS] Solcast šiandien: {solcast_today:.1f} kWh, "
            f"rytoj: {solcast_tomorrow:.1f} kWh | "
            f"Poreikis: {total_needed:.1f} kWh | "
            f"Balansas: {balance:.1f} kWh | "
            f"SOC: {soc:.1f}%"
        )

        # Sprendimas
        if balance > BALANCE_MIN_KWH:
            self.boiler_allowed = True
            self.last_decision = f"Balansas +{balance:.1f} kWh — boileris leidžiamas"
            self.log(f"[STRATEGINIS] Boileris LEIDŽIAMAS — balansas +{balance:.1f} kWh")
        else:
            self.boiler_allowed = False
            self.last_decision = f"Balansas {balance:.1f} kWh — boileris draudžiamas"
            self.log(f"[STRATEGINIS] Boileris DRAUDŽIAMAS — balansas {balance:.1f} kWh")
            self.last_balance = balance
            self.publish_status()
            return

        # Papildoma patikra: SOC > 90% — ar rytoj tikrai pasieks 95%?
        if soc > SOC_HIGH_THRESHOLD:
            if balance > soc_gap_kwh:
                self.log(
                    f"[STRATEGINIS] SOC {soc:.1f}% > 90%, "
                    f"rytoj tikrai pasieks 95% — boileris LEIDŽIAMAS"
                )
                self.boiler_allowed = True
                self.last_decision = f"SOC {soc:.0f}% > 90%, balansas pakankamas — leidžiamas"
            else:
                self.log(
                    f"[STRATEGINIS] SOC {soc:.1f}% > 90%, "
                    f"bet balansas per mažas 95% pasiekimui — boileris DRAUDŽIAMAS"
                )
                self.boiler_allowed = False
                self.last_decision = f"SOC {soc:.0f}% > 90%, bet balansas mažas 95% tikslui — draudžiamas"

        self.last_balance = balance
        self.publish_status()


    # ============================================================
    #  3. TAKTINIS CIKLAS — kas 10 sek
    # ============================================================

    def tactical_cycle(self, kwargs):
        """
        Taktinis ciklas — realiu laiku valdo boilerį
        pagal dabartinį perteklių ir kaupiklio būseną.
        """
        soc         = self.get_float("soc")
        pv_power_kw = self.get_float("pv_power") / 1000
        house_kw    = self.get_float("house_load") / 1000
        grid_kw     = self.get_float("grid_power") / 1000
        boiler_temp = self.get_float("boiler_temp", default=20.0)

        # Žemos baterijos apsaugą (eksporto stabdymą <13% ir atnaujinimą >30%)
        # vykdo automations.yaml — čia nebevaldome, kad nebūtų dviejų
        # konkuruojančių logikų su skirtingais slenksčiais.

        # 1. Avariniai patikrinimai — visada pirma
        emergency, reason = self.emergency_checks(soc, boiler_temp)
        if emergency:
            self.boiler_off(reason)
            return

        # 2. Strateginis leidimas
        if not self.boiler_allowed:
            self.boiler_off("Strateginis draudimas — nepakankamas energijos balansas")
            return

        # 3. Boilerio temperatūros tikslas
        temp_target = self.get_boiler_temp_target()
        if boiler_temp >= temp_target:
            self.boiler_off(f"Boileris pasiekė tikslą {boiler_temp:.1f}°C >= {temp_target}°C")
            return

        # 4. SOC kritimo greičio patikrinimas
        drop_rate = self.calculate_soc_drop_rate(soc)
        if drop_rate > SOC_DROP_RATE_MAX:
            self.boiler_off(
                f"SOC krenta per greitai: {drop_rate:.1f}% per 10 min — debesys?"
            )
            return

        # 5. Realaus pertekliaus skaičiavimas
        # Perteklius = saulė - namai - inverterio savivartojimas - eksporto riba
        surplus_kw = pv_power_kw - house_kw - INVERTER_SELF_KW - ESO_EXPORT_LIMIT_KW

        self.log(
            f"[TAKTINIS] PV: {pv_power_kw:.2f} kW | "
            f"Namai: {house_kw:.2f} kW | "
            f"Tinklas: {grid_kw:.2f} kW | "
            f"Perteklius: {surplus_kw:.2f} kW | "
            f"SOC: {soc:.1f}% | "
            f"Boileris: {boiler_temp:.1f}°C",
            level="DEBUG"
        )

        # 6. Sprendimas pagal perteklių ir SOC
        soc_min = self.get_season_soc_min()

        if surplus_kw >= BOILER_POWER_KW:
            reason = f"Perteklius {surplus_kw:.2f} kW >= boilerio {BOILER_POWER_KW} kW"
            self.last_decision = reason
            self.boiler_on(reason)

        elif surplus_kw >= SURPLUS_MIN_KW and soc > SOC_HIGH_THRESHOLD:
            deficit_kw = BOILER_POWER_KW - surplus_kw
            reason = (
                f"Dalinis perteklius {surplus_kw:.2f} kW, "
                f"kaupiklis padengs {deficit_kw:.2f} kW (SOC {soc:.1f}%)"
            )
            self.last_decision = reason
            self.boiler_on(reason)

        elif surplus_kw >= SURPLUS_MIN_KW and soc > (soc_min + 15):
            reason = (
                f"Perteklius {surplus_kw:.2f} kW, SOC {soc:.1f}% "
                f"(min+15={soc_min+15}%) — leidžiama"
            )
            self.last_decision = reason
            self.boiler_on(reason)

        else:
            reason = (
                f"Perteklius per mažas ({surplus_kw:.2f} kW) "
                f"arba SOC {soc:.1f}% per žemas — {soc_min}% minimumas"
            )
            self.last_decision = reason
            self.boiler_off(reason)

        self.last_surplus = surplus_kw
        self.publish_status()


    # ============================================================
    #  4. VAKARO IŠKROVIMO CIKLAS
    # ============================================================

    def evening_discharge_cycle(self, kwargs):
        """
        Vakaro ciklas — perskaičiuoja tikslinį SOC rytui
        ir nustato Solis S6 SOC minimumą.
        Veikia kas 30 min nuo 18:00 iki 23:00.
        """
        solcast_tomorrow = self.corrected_kwh(self.get_float("solcast_tomorrow"))
        soc              = self.get_float("soc")
        soc_min          = self.get_season_soc_min()

        consumption_tomorrow = self.get_daily_consumption()

        # Prognozuojamas perteklius rytoj
        expected_surplus = solcast_tomorrow - consumption_tomorrow

        self.log(
            f"[VAKARO] Solcast rytoj: {solcast_tomorrow:.1f} kWh | "
            f"Poreikis rytoj: {consumption_tomorrow:.1f} kWh | "
            f"Perteklius: {expected_surplus:.1f} kWh | "
            f"SOC dabar: {soc:.1f}%"
        )

        if expected_surplus >= BATTERY_USABLE_KWH:
            # Labai saulėta rytoj — baterija užsikraus pilnai bet kuriuo atveju
            # Galima išleisti iki sezono minimumo
            target_soc = soc_min
            self.log(
                f"[VAKARO] Labai saulėta rytoj ({solcast_tomorrow:.1f} kWh) — "
                f"leisk išsikrauti iki {target_soc}%"
            )

        elif expected_surplus > 0:
            # Dalinis perteklius — proporcingas tikslas
            # Kuo mažiau pertekliaus, tuo daugiau palikti
            ratio = expected_surplus / BATTERY_USABLE_KWH
            target_soc = soc_min + int((100 - soc_min) * (1 - min(ratio, 1)))
            target_soc = max(soc_min, min(target_soc, 85))
            self.log(
                f"[VAKARO] Vidutinė diena ({solcast_tomorrow:.1f} kWh) — "
                f"tikslas {target_soc}% (proporcingai)"
            )

        else:
            # Debesuota rytoj — palik maksimumą
            target_soc = min(85, soc)
            self.log(
                f"[VAKARO] Debesuota rytoj ({solcast_tomorrow:.1f} kWh) — "
                f"neiškrauk, palik {target_soc}%"
            )

        # Audros režimas — visada 100% rezervas
        if self.is_storm_mode():
            target_soc = 100
            self.log("[VAKARO] AUDROS režimas — SOC min nustatomas į 100%")

        # Tik publikuojame tikslą — patį TOU iškrovimą per Modbus įjungia/atnaujina
        # automations.yaml (solis_evening_discharge 20:00 + solis_tou_recalc_5min),
        # skaitydamos sensor.energy_manager_target_soc.
        self.last_target_soc = target_soc
        self.save_target_soc()
        self.publish_status()


    # ============================================================
    #  PRANEŠIMAI
    # ============================================================

    def send_notification(self, message, title="Energijos valdymas"):
        """
        Siunčia pranešimą į HA persistent notifications (notify.telegram
        nesukonfigūruotas — anksčiau visi pranešimai krisdavo į exception).
        Atsiradus Telegram/mobile_app — pakeisti servisą čia.
        """
        try:
            self.call_service(
                "notify/persistent_notification",
                title=title,
                message=message
            )
        except Exception as e:
            self.log(f"Pranešimo klaida: {e}", level="WARNING")

    def notify_power_outage(self):
        """Pranešimas kai dingsta elektra."""
        self.send_notification(
            "⚡ DINGO ELEKTRA! Sistema veikia iš kaupiklio.",
            title="Elektros gedimas"
        )

    def notify_storm_mode_activated(self):
        """Pranešimas kai aktyvuojamas audros režimas."""
        soc = self.get_float("soc")
        self.send_notification(
            f"🌩️ AUDROS režimas įjungtas. Kaupiklio lygis: {soc:.0f}%\n"
            f"Boileris išjungtas. Kraunama iki 100%.",
            title="Audros režimas"
        )
