"""
energy_manager_eimo.py — 2-os elektrinės (Eimo SE) energijos valdymas
=====================================================================
ATSKIRA, NEPRIKLAUSOMA kopija nuo 1-os elektrinės energy_manager.py.
Valdoma per Solis Cloud API (solis / solis_cloud_control), inverteris
1033300254190112. Publikuoja *_eimo sensorius; būsena *_eimo.json failuose.
NIEKADA neliesti 1-os elektrinės entitės/failų.
Versija:  1.0 (Eimo)

Sistemos parametrai:
  Kaupiklis:  Dyness PowerBrick 14.336 kWh, naudojama 5–100% (≈13.619 kWh)
  Inverteris: Solis (cloud), 5 min duomenų latencija
  Boilerio NĖRA — boilerio logika inertiška (switch entity neegzistuoja)

Logikos lygiai:
  1. Avariniai patikrinimai  — kiekvienam ciklui
  2. Strateginis ciklas      — kas 30 min (Solcast prognozė)
  3. Taktinis ciklas         — kas 60 sek (cloud duomenys ~5 min latencija)
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


# ============================================================
#  KONFIGŪRACIJA — pritaikyk prie savo sistemos
# ============================================================

# Sistemos parametrai
# Dyness PowerBrick 14.336 kWh, naudojama 5–100% (dugnas 5%), SOC ruožas 95%.
# 1% SOC = 0.14336 kWh (= 14.336 kWh / 100).
BATTERY_USABLE_KWH   = 13.619     # naudingoji kaupiklio talpa kWh (100%→5%, 14.336×95%)
KWH_PER_SOC          = BATTERY_USABLE_KWH / 95.0   # kWh viename SOC % (≈0.1434)
# Absoliutus SOC dugnas (Dyness leidžia iki 5%).
MIN_SOC              = 5
BOILER_POWER_KW      = 2.2        # boilerio galia kW
ESO_EXPORT_LIMIT_KW  = 1.0        # max atidavimas į tinklą kW (Eimo taip pat 1 kW,
                                  # vartotojo patvirtinta 2026-07-14)
# REALIZAVIMO plane vartojimas vertinamas nuosaikiai (2026-07-14, vartotojo
# nurodymas): jis paroje pasiskirsto netolygiai ir nėra patikimas kanalas,
# todėl plane užskaitoma tik garantuota bazinė apkrova, o visas likęs
# vartojimas lieka neplanuojamu bonusu.
CONS_BASE_KW         = 0.3

# REALIZAVIMO planavimo marža prognozei (2026-07-14, vartotojo kriterijus:
# svarbiausia realizuoti VISĄ pagamintą saulės energiją, target SOC — tik
# įrankis). Nuostoliai asimetriški: nukirpta kWh prarandama 100 %, o per
# daug paruošta vieta kainuoja tik naktinio eksporto round-trip ~8 %
# (ESO pasaugojimo banke kWh vertės nepraranda).
PLAN_MARGIN          = 1.15
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
# kai input_number nepasiekiamas. 0 — teisėta reikšmė (ESO pasaugojimo
# schema: buy=0.0/sell=0.25), žr. get_price().
PRICE_BUY_DEFAULT    = 0.18       # €/kWh perkant iš tinklo
PRICE_SELL_DEFAULT   = 0.08       # €/kWh parduodant į tinklą

# Adaptyvi Solcast korekcija: kasdien 23:50 koeficientas atnaujinamas pagal
# faktas/prognozė santykį (EMA). Visi strateginiai skaičiavimai naudoja
# koreguotą prognozę. Koeficientas ribojamas, kad vienas anomalus
# (pvz. sniego ant panelių) nesugriautų prognozių.
# Keliai per __file__ — AppDaemon konteineryje /config rodo į addon'o vidinį
# katalogą, todėl hardcoded /config/appdaemon/... ten neegzistuoja.
_APP_DIR             = os.path.dirname(os.path.abspath(__file__))
CORRECTION_FILE      = os.path.join(_APP_DIR, "forecast_correction_eimo.json")
# Paskutinis target_soc — išsaugomas, kad HA/AppDaemon restartas vakare ar
# naktį negrąžintų tikslo į 100% iki kito 18:00 perskaičiavimo.
TARGET_SOC_FILE      = os.path.join(_APP_DIR, "target_soc_eimo.json")
CORRECTION_ALPHA     = 0.2        # EMA glodinimo koeficientas
CORRECTION_MIN       = 0.7
CORRECTION_MAX       = 1.3

# Valandinė korekcija: šalia globalaus koeficiento mokomi valandos-of-day
# koeficientai (rytinis rūkas/šešėliai klysta sistemingai kitaip nei
# vidurdienis). Mokymui naudojamas RYTINIS prognozės snapshot (04:40) —
# vakare Solcast jau būna prisitaikęs prie dienos fakto ir klaida atrodytų
# mažesnė nei buvo planuojant. Faktas — iš pv_today valandinių deltų
# (get_history). PASTABA Eimo: Solcast svetainė modeliuoja Šeškinių stogą,
# tad čia koeficientai (kaip ir globalus 0.90) sugeria ir stogų skirtumą.
# Koeficientai TAIKOMI tik sukaupus HOURLY_MIN_DAYS parų.
HOURLY_MIN_DAYS   = 7
HOURLY_FC_MIN_KWH = 0.05   # valandos prognozės minimumas mokymuisi (kWh)
HOURLY_RATIO_MIN  = 0.3    # vienos paros santykio ribos (triukšmui)
HOURLY_RATIO_MAX  = 2.0
HOURLY_FACTOR_MIN = 0.4    # išmokto koeficiento ribos
HOURLY_FACTOR_MAX = 1.6

# Ryto įjungimas: PV galios slenkstis, nuo kurio gamyba dengia inverterio
# savivartojimą ir laikyti jį įjungtą tampa pelninga (~100 W skirtumas
# tarp idle 130 W ir off 30 W).
MORNING_PV_THRESHOLD_KW = 0.1
MORNING_ON_MARGIN_MIN   = 30      # įjungti tiek min anksčiau nei slenkstis
                                  # (30, kaip Solis: Solcast prognozė
                                  # pusvalandinė — perkirtimas gali būti
                                  # periodo pradžioje)

# Sezoniniai SOC minimumai %
SEASON_SOC_MIN = {
    "žiema":      80,
    "pavasaris":  60,
    "vasara":     5,
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

# HA sensorių pavadinimai — Eimo SE (cloud: solis / solis_cloud_control).
# Solcast laikinai bendras su 1-a elektrine (Eimo už 10 km, panaši prognozė).
_CLOUD = "sensor.solis_inverter_1033300254190112_"
SENSOR = {
    "solcast_today":     "sensor.solcast_pv_forecast_forecast_remaining_today",
    "solcast_tomorrow":  "sensor.solcast_pv_forecast_forecast_tomorrow",
    "soc":               _CLOUD + "solis_remaining_battery_capacity",
    # PV galia (W) = PV1+PV2 suma per template sensorių (packages/eimo.yaml).
    "pv_power":          "sensor.eimo_pv_power",
    "house_load":        _CLOUD + "solis_total_consumption_power",
    "grid_power":        _CLOUD + "solis_power_grid_total_power",
    # Boilerio Eimo nėra — entitės neegzistuoja, boilerio logika lieka inertiška.
    "boiler_temp":       "sensor.boiler_temperature_eimo",
    "boiler_switch":     "switch.boiler_switch_eimo",
    "season":            "input_select.energy_season_eimo",
    "storm_mode":        "input_boolean.storm_mode_eimo",
    "inverter_temp":     _CLOUD + "solis_temperature",
    "pv_today":          _CLOUD + "solis_energy_today",
    "solcast_today_total": "sensor.solcast_pv_forecast_forecast_today",
    "battery_power":     _CLOUD + "solis_battery_power",
    "power_state":       "switch.inverter_control_1033300254190112_inverter_on_off",
    "price_buy":         "input_number.electricity_price_buy_eimo",
    "price_sell":        "input_number.electricity_price_sell_eimo",
}

# PASTABA dėl inverterio valdymo: šis modulis inverterio NEvaldo tiesiogiai.
# Jis tik skaičiuoja ir publikuoja sensor.energy_manager_eimo_target_soc; patį TOU
# iškrovimą per CLOUD (solis_cloud_control slot1_*) vykdo packages/eimo.yaml
# automacijos (solis_evening_discharge_eimo 20:00 + solis_tou_recalc_eimo kas 5 min).
# Žemos baterijos apsaugą dienos metu užtikrina paties sloto cut-off SOC
# (daytime_export_floor_eimo, min 6%) — atskirų protect/resume automacijų,
# kaip Modbus pusėje, nereikia, nes Eimo iškrauna tik per slotą su cut-off.
# SOC dugnas — MIN_SOC (5%).

# Atsarginis vidutinis namų suvartojimas per dieną kWh — naudojamas TIK kai
# dinaminis vartojimo modelis (consumption_model.py) dar neturi reikšmės.
# Seed pagal realų ~13 kWh/parą (buvęs 8.0 stipriai per mažas).
DEFAULT_DAILY_CONSUMPTION = 13.0

# Vartojimo modelio sensoriai (publikuoja consumption_model.py)
CONSUMPTION_TOMORROW_SENSOR  = "sensor.consumption_forecast_tomorrow_eimo"
CONSUMPTION_REMAINING_SENSOR = "sensor.consumption_remaining_today_eimo"


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

        # Rytinis šiandienos prognozės snapshot — valandinės korekcijos
        # mokymuisi (žr. HOURLY_* konstantas)
        self.run_daily(self.snapshot_today_forecast, time(4, 40))

        # Strateginis ciklas — kas 30 min; pirmą kartą po 5 sek
        self.run_in(self.strategic_cycle, 5)
        self.run_every(self.strategic_cycle, "now", 30 * 60)

        # Taktinis ciklas — kas 60 sek (Eimo: boilerio nėra, cloud duomenys
        # atsinaujina ~kas 5 min, todėl 10 s ciklas tik kurtų nereikalingą churn).
        self.run_every(self.tactical_cycle, "now+15", 60)

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

        # Ryto vietos patikra — kol naktinis slotas dar gali padaryti vietos
        # (recalc solis_tou_recalc_eimo iki 07:00). Tik žemina target.
        self.run_daily(self.morning_room_check, time(5, 30))
        self.run_daily(self.morning_room_check, time(6, 15))
        self.run_daily(self.morning_room_check, time(6, 45))

        # Sukuriami sensoriai iš karto, kad dashboard nerodytų "entity not found"
        self.set_state("sensor.energy_manager_eimo_surplus_now", state="0.0",
                       attributes={"friendly_name": "Saulės perteklius (dabar)",
                                   "unit_of_measurement": "kW", "icon": "mdi:solar-panel"})
        self.set_state("sensor.energy_manager_eimo_target_soc", state=str(self.last_target_soc),
                       attributes={"friendly_name": "Tikslinė SOC riba (Solis)",
                                   "unit_of_measurement": "%", "icon": "mdi:battery-charging-80",
                                   "device_class": "battery"})
        self.set_state("sensor.energy_manager_eimo_inverter_temp", state="0",
                       attributes={"friendly_name": "Inverterio temperatūra",
                                   "unit_of_measurement": "°C", "icon": "mdi:thermometer",
                                   "device_class": "temperature"})
        self.set_state("sensor.solcast_correction_factor_eimo",
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
        self.publish_realization()

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
        solcast_t     = self.corrected_remaining_today()
        solcast_tm    = self.corrected_tomorrow()

        # PASTABA: sensor.energy_manager_eimo_status priklauso template sensoriui
        # (configuration.yaml — rodo inverterio režimą). Sprendimo tekstas
        # publikuojamas į ATSKIRĄ entity, kad du šaltiniai nesipjautų.
        self.set_state(
            "sensor.energy_manager_eimo_decision",
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
            "sensor.energy_manager_eimo_balance",
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
        # sensor.energy_manager_eimo_surplus (likusios dienos perteklius kWh).
        self.set_state(
            "sensor.energy_manager_eimo_surplus_now",
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
            "sensor.energy_manager_eimo_target_soc",
            state=str(self.last_target_soc),
            attributes={
                "friendly_name": "Tikslinė SOC riba (Solis)",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-charging-80",
                "device_class": "battery",
            }
        )

        self.set_state(
            "sensor.energy_manager_eimo_inverter_temp",
            state=str(round(inverter_temp, 1)),
            attributes={
                "friendly_name": "Inverterio temperatūra",
                "unit_of_measurement": "°C",
                "icon": "mdi:thermometer",
                "device_class": "temperature",
            }
        )

        self.set_state(
            "sensor.energy_manager_eimo_boiler_status",
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
                if age_h <= 24 and MIN_SOC <= target <= 100:
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
                t = datetime.fromisoformat(s["last_changed"]).astimezone()
                v = float(s["state"])
            except (ValueError, TypeError, KeyError):
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

    def get_price(self, key, default):
        """Elektros kaina iš input_number. 0 — TEISĖTA reikšmė (ESO pasaugojimo
        schema: buy=0.0/sell=0.25), todėl fallback į konstantą taikomas tik kai
        entity nepasiekiamas (iki 2026-07-17 sentinel >0.001 versdavo sąmoningą
        0.0 į 0.18 ir ekonomika skaičiuota ne ta kaina)."""
        val = self.get_sensor_float(SENSOR[key], default=-1.0)
        return val if val >= 0 else default

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
            for period in detailed:
                start = datetime.fromisoformat(period["period_start"])
                factor = self.hourly_factor(start.astimezone().hour)
                if float(period.get("pv_estimate", 0)) * factor >= MORNING_PV_THRESHOLD_KW:
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
        target_soc = max(float(self.last_target_soc), float(MIN_SOC))
        price_buy  = self.get_price("price_buy", PRICE_BUY_DEFAULT)
        price_sell = self.get_price("price_sell", PRICE_SELL_DEFAULT)
        power_on   = self.get_state(SENSOR["power_state"]) == "on"

        # Baterijos kWh vertė: ar rytojaus perteklius užpildys bateriją nuo
        # tikslinio SOC iki 95%? Jei taip — kiekviena naktį išleista kWh būtų
        # šiaip eksportuota (vertė = pardavimo kaina). Jei ne — jos vertė =
        # pirkimo kaina (rytoj vakare jos truks ir teks pirkti).
        tomorrow_corr  = self.corrected_tomorrow()
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
            # Eimo cloud battery_power ženklas: + = kraunasi, − = iškrauna
            # (atvirkščiai nei Modbus; patikrinta 2026-07-13 pagal istoriją).
            batt_w = self.get_sensor_float(SENSOR["battery_power"])
            discharge_kw = (-batt_w if batt_w < -50
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
            "sensor.inverter_night_economics_eimo",
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
            "sensor.inverter_shutdown_eta_eimo",
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
                "sensor.inverter_morning_on_time_eimo",
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
            "sensor.solcast_correction_factor_eimo",
            state=str(round(self.correction.get("factor", 1.0), 3)),
            attributes={
                "friendly_name": "Solcast korekcijos koeficientas",
                "icon": "mdi:tune-variant",
                "dienos": self.correction.get("days", 0),
                "atnaujinta": self.correction.get("updated"),
                "rytoj_koreguota_kwh": round(tomorrow_corr, 1),
                "valandiniai": self.correction.get("hourly_factors", {}),
                "valandiniu_dienos": self.correction.get("hourly_days", 0),
                "valandiniai_taikomi": self.hourly_ready(),
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
        solcast_today    = self.corrected_remaining_today()
        solcast_tomorrow = self.corrected_tomorrow()
        soc              = self.get_float("soc")

        consumption_today    = self.get_consumption_remaining_today()
        consumption_tomorrow = self.get_daily_consumption()

        # Kiek trūksta iki 95% SOC (naudingoji talpa, 1% = 0.1434 kWh)
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
    #  3. TAKTINIS CIKLAS — kas 60 sek
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

        # Žemos baterijos apsaugą užtikrina cloud sloto cut-off SOC
        # (packages/eimo.yaml) — čia nevaldome, kad nebūtų dviejų
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
    #  4. VAKARO IŠKROVIMO CIKLAS + REALIZAVIMO KRITERIJUS
    # ============================================================

    def battery_room_needed(self, day):
        """
        REALIZAVIMO kriterijus (2026-07-14): kiek kWh baterijos vietos reikia,
        kad VISA prognozuojama Eimo saulė būtų realizuota — sugerta namų
        apkrovos, ESO 1 kW eksporto arba baterijos, be nukirpimo.

        Kiekvienam Solcast 30 min periodui: kas iš PV galios (× Eimo korekcija
        × PLAN_MARGIN; šiandienai dar × intradienos santykis) netelpa į
        bazinę namų apkrovą (CONS_BASE_KW) + 1 kW eksportą, PRIVALO tilpti į
        bateriją. Vartojimo prognozė plane neužskaitoma — tik bonusas.
        Solcast masyvas bendras — Eimo galios profilį duoda korekcijos
        koeficientas (solcast_correction_factor_eimo).

        day: "today" (nuo dabar iki paros galo) arba "tomorrow" (visa para).
        Grąžina (reikia_kwh, eksportas_kwh, pv_plan_kwh).
        """
        # Valandos koeficientas taikomas kiekvienam periodui atskirai
        # (hourly_factor), čia — tik bendroji marža ir intradienos santykis.
        margin = PLAN_MARGIN
        now = datetime.now().astimezone()

        if day == "today":
            entity = SENSOR["solcast_today_total"]
            # Intradienos santykis: jei gamyba jau lenkia prognozę, likusi
            # diena planuojama pagal faktą. Tik didina — mažėjimą dengia marža.
            intraday = self.get_sensor_float("sensor.solcast_intraday_ratio_eimo",
                                             default=1.0)
            margin *= max(intraday, 1.0)
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
        sensor.energy_manager_eimo_room_shortfall — kiek kWh likusios
        šiandienos saulės (su marža) dar netilptų į laisvą baterijos vietą +
        namus + 1 kW eksportą. > 0 reiškia nukirpimo riziką: vietą daro
        dienos automatika (solis_daytime_discharge_eimo) ir ryto patikra.
        """
        soc = self.get_float("soc", default=100.0)
        need, export_est, pv_plan = self.battery_room_needed("today")
        headroom = max(0.0, (100.0 - soc) * KWH_PER_SOC)
        shortfall = need - headroom
        self.set_state(
            "sensor.energy_manager_eimo_room_shortfall",
            state=str(round(shortfall, 2)),
            attributes={
                "friendly_name": "Eimo vietos trūkumas saulei (šiandien)",
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

    def morning_room_check(self, kwargs):
        """
        Ryto patikra (05:30 / 06:15 / 06:45): vakarinis target skaičiuotas
        22:30 — jei šiandienos planas (koreguotas, su marža ir intradienos
        santykiu) rodo, kad laisvos vietos NEUŽTEKS visai saulei realizuoti,
        target NUŽEMINAMAS, kad naktinis slotas (recalc kas 5 min iki 07:00)
        spėtų padaryti daugiau vietos. Tikslo niekada nekelia.
        """
        if self.is_storm_mode():
            return
        soc = self.get_float("soc")
        need, _export, pv_plan = self.battery_room_needed("today")
        headroom = max(0.0, (100.0 - soc) * KWH_PER_SOC)
        self.publish_realization()

        if need <= headroom + 0.3:
            self.log(f"[RYTO VIETA] OK: reikia {need:.1f} kWh, laisva "
                     f"{headroom:.1f} kWh (PV planas {pv_plan:.1f} kWh)")
            return

        new_target = int(round(100 - need / KWH_PER_SOC))
        new_target = max(self.get_season_soc_min(), min(new_target, 85))
        if new_target < int(self.last_target_soc):
            self.log(f"[RYTO VIETA] Trūksta vietos: reikia {need:.1f} kWh, "
                     f"laisva {headroom:.1f} kWh → target {self.last_target_soc}% "
                     f"→ {new_target}% (PV planas {pv_plan:.1f} kWh)")
            self.last_target_soc = new_target
            self.save_target_soc()
            self.publish_status()

    def evening_discharge_cycle(self, kwargs):
        """
        Vakaro ciklas — perskaičiuoja tikslinį SOC rytui pagal REALIZAVIMO
        kriterijų: baterijoje turi likti tiek vietos, kad visa rytojaus
        saulė, netelpanti į namų apkrovą ir 1 kW ESO eksportą, tilptų be
        nukirpimo. Target SOC — tik šio kriterijaus išvestinė.
        (Iki 2026-07-14 buvo proporcinė formulė nuo „perteklius = prognozė −
        visos paros vartojimas": ji pervertindavo namų sugertį šviesiu metu
        ir nevertino eksporto kanalo nei prognozės nepataikymo.)
        Veikia kas 30 min nuo 18:00 iki 23:00.
        """
        soc     = self.get_float("soc")
        soc_min = self.get_season_soc_min()

        need, export_est, pv_plan = self.battery_room_needed("tomorrow")

        target_soc = int(round(100 - need / KWH_PER_SOC))
        target_soc = max(soc_min, min(target_soc, 85))

        self.log(
            f"[VAKARO] PV planas rytoj (su marža {PLAN_MARGIN}): {pv_plan:.1f} kWh | "
            f"eksportas dienos metu ~{export_est:.1f} kWh | "
            f"baterijai reikia vietos: {need:.1f} kWh → target {target_soc}% | "
            f"SOC dabar: {soc:.1f}%"
        )

        # Audros režimas — visada 100% rezervas
        if self.is_storm_mode():
            target_soc = 100
            self.log("[VAKARO] AUDROS režimas — SOC min nustatomas į 100%")

        # Tik publikuojame tikslą — patį slot1 iškrovimą per cloud įjungia/atnaujina
        # packages/eimo.yaml (solis_evening_discharge_eimo 20:00 +
        # solis_tou_recalc_eimo kas 5 min), skaitydamos
        # sensor.energy_manager_eimo_target_soc.
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
