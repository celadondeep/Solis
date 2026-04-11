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
from datetime import datetime, time
import statistics


# ============================================================
#  KONFIGŪRACIJA — pritaikyk prie savo sistemos
# ============================================================

# Sistemos parametrai
BATTERY_CAPACITY_KWH = 16.0       # kaupiklio talpa kWh
BOILER_POWER_KW      = 2.2        # boilerio galia kW
ESO_EXPORT_LIMIT_KW  = 1.0        # max atidavimas į tinklą kW

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
    "solcast_today":     "sensor.solcast_forecast_remaining_today",
    "solcast_tomorrow":  "sensor.solcast_forecast_tomorrow",
    "soc":               "sensor.solis_battery_soc",
    "pv_power":          "sensor.solis_pv_power",
    "house_load":        "sensor.solis_house_load",
    "grid_power":        "sensor.solis_grid_power",
    "boiler_temp":       "sensor.boiler_temperature",
    "boiler_switch":     "switch.boiler_switch",
    "season":            "input_select.energy_season",
    "storm_mode":        "input_boolean.storm_mode",
}

# Vidutinis namų suvartojimas per dieną kWh (bus atnaujinamas iš istorijos)
DEFAULT_DAILY_CONSUMPTION = 8.0


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

        # Strateginis ciklas — kas 30 min
        self.run_every(self.strategic_cycle, "now", 30 * 60)

        # Taktinis ciklas — kas 10 sek
        self.run_every(self.tactical_cycle, "now+15", 10)

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

        self.log("EnergyManager paleistas sėkmingai.")


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
        Grąžina vidutinį namų suvartojimą per dieną kWh.
        Idealiu atveju čia reikėtų pasiimti iš HA statistikos.
        Kol kas naudojamas konstantinis default.
        """
        return DEFAULT_DAILY_CONSUMPTION

    def get_consumption_remaining_today(self):
        """Apskaičiuoja likusį suvartojimą šiandien kWh."""
        now = datetime.now()
        hours_left = 24 - now.hour - (now.minute / 60)
        daily = self.get_daily_consumption()
        return (daily / 24) * hours_left

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
        """Įjungia boilerį."""
        current = self.get_state(SENSOR["boiler_switch"])
        if current != "on":
            self.turn_on(SENSOR["boiler_switch"])
            self.log(f"Boileris ĮJUNGTAS. Priežastis: {reason}")

    def boiler_off(self, reason=""):
        """Išjungia boilerį."""
        current = self.get_state(SENSOR["boiler_switch"])
        if current != "off":
            self.turn_off(SENSOR["boiler_switch"])
            self.log(f"Boileris IŠJUNGTAS. Priežastis: {reason}")

    def set_solis_soc_minimum(self, soc_min):
        """
        Nustato Solis S6 SOC minimumą per Modbus.
        Reikia sukonfigūruoti number entity solis_modbus integracijoje.
        Pakeisk entity pavadinimą pagal savo integraciją.
        """
        try:
            self.call_service(
                "number/set_value",
                entity_id="number.solis_battery_over_discharge_soc",
                value=soc_min
            )
            self.log(f"Solis SOC minimumas nustatytas: {soc_min}%")
        except Exception as e:
            self.log(f"Klaida nustatant Solis SOC min: {e}", level="WARNING")


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
        solcast_today    = self.get_float("solcast_today")
        solcast_tomorrow = self.get_float("solcast_tomorrow")
        soc              = self.get_float("soc")

        consumption_today    = self.get_consumption_remaining_today()
        consumption_tomorrow = self.get_daily_consumption()

        # Kiek trūksta iki 95% SOC
        soc_gap_kwh = max(0, (SOC_TARGET_CHARGE - soc) / 100 * BATTERY_CAPACITY_KWH)

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
            self.log(f"[STRATEGINIS] Boileris LEIDŽIAMAS — balansas +{balance:.1f} kWh")
        else:
            self.boiler_allowed = False
            self.log(f"[STRATEGINIS] Boileris DRAUDŽIAMAS — balansas {balance:.1f} kWh")
            return

        # Papildoma patikra: SOC > 90% — ar rytoj tikrai pasieks 95%?
        if soc > SOC_HIGH_THRESHOLD:
            if balance > soc_gap_kwh:
                self.log(
                    f"[STRATEGINIS] SOC {soc:.1f}% > 90%, "
                    f"rytoj tikrai pasieks 95% — boileris LEIDŽIAMAS"
                )
                self.boiler_allowed = True
            else:
                self.log(
                    f"[STRATEGINIS] SOC {soc:.1f}% > 90%, "
                    f"bet balansas per mažas 95% pasiekimui — boileris DRAUDŽIAMAS"
                )
                self.boiler_allowed = False


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
        # Perteklius = saulė - namai - jau atiduodama į tinklą
        surplus_kw = pv_power_kw - house_kw - ESO_EXPORT_LIMIT_KW

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
            # Pilnas perteklius — boileris veikia iš saulės
            self.boiler_on(
                f"Perteklius {surplus_kw:.2f} kW >= boilerio {BOILER_POWER_KW} kW"
            )

        elif surplus_kw >= SURPLUS_MIN_KW and soc > SOC_HIGH_THRESHOLD:
            # Dalinis perteklius — kaupiklis padengia skirtumą
            # Leidžiame tik jei SOC aukštas (> 90%)
            deficit_kw = BOILER_POWER_KW - surplus_kw
            self.boiler_on(
                f"Dalinis perteklius {surplus_kw:.2f} kW, "
                f"kaupiklis padengs {deficit_kw:.2f} kW (SOC {soc:.1f}%)"
            )

        elif surplus_kw >= SURPLUS_MIN_KW and soc > (soc_min + 15):
            # Perteklius yra ir kaupiklis gerokai virš minimumo
            self.boiler_on(
                f"Perteklius {surplus_kw:.2f} kW, SOC {soc:.1f}% "
                f"(min+15={soc_min+15}%) — leidžiama"
            )

        else:
            # Pertekliaus nėra arba SOC per žemas
            self.boiler_off(
                f"Perteklius per mažas ({surplus_kw:.2f} kW) "
                f"arba SOC {soc:.1f}% per žemas — {soc_min}% minimumas"
            )


    # ============================================================
    #  4. VAKARO IŠKROVIMO CIKLAS
    # ============================================================

    def evening_discharge_cycle(self, kwargs):
        """
        Vakaro ciklas — perskaičiuoja tikslinį SOC rytui
        ir nustato Solis S6 SOC minimumą.
        Veikia kas 30 min nuo 18:00 iki 23:00.
        """
        solcast_tomorrow = self.get_float("solcast_tomorrow")
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

        if expected_surplus >= BATTERY_CAPACITY_KWH:
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
            ratio = expected_surplus / BATTERY_CAPACITY_KWH
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

        # Nustatome Solis SOC minimumą
        self.set_solis_soc_minimum(target_soc)


    # ============================================================
    #  PRANEŠIMAI
    # ============================================================

    def send_notification(self, message, title="Energijos valdymas"):
        """
        Siunčia pranešimą.
        Pakeisk notify servisą pagal savo konfigūraciją
        (Telegram, Callmebot Facebook Messenger ir t.t.)
        """
        try:
            self.call_service(
                "notify/telegram",   # <-- pakeisk į savo notify servisą
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
