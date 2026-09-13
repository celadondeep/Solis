"""
battery_health.py — Kaupiklio sveikatos stebėjimas
====================================================
Stebi Solis S6 kaupiklio būseną per Modbus ir siunčia
įspėjimus jei aptinka anomalijas.

Stebimi rodikliai:
  - Temperatūra (per aukšta = degradacija)
  - SOH — State of Health %
  - Ciklų skaičius
  - Įkrovimo/iškrovimo efektyvumas
  - Įtampos anomalijos

Naudojami HA sensoriai (Solis Modbus, žr. SENSOR žodyną žemiau):
  solis_s6_eh3p_battery_temperature_bms / _soc / _soh / _voltage / _current
  solis_s6_eh3p_today_battery_charge_energy / _discharge_energy
  solis_s6_eh3p_total_battery_charge_energy — ekvivalentiniams ciklams
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import json
import os

from energy_system.battery_health_math import (
    EFFICIENCY_MAX_VALID,
    battery_efficiency_percent,
    is_plausible_efficiency,
    plausible_efficiency_rows,
)


# ============================================================
#  KONFIGŪRACIJA
# ============================================================

BATTERY_CAPACITY_KWH = 16.0

# Temperatūros ribos
TEMP_WARNING  = 40.0   # °C — įspėjimas
TEMP_CRITICAL = 45.0   # °C — kritinis
TEMP_LOW      = 5.0    # °C — per šalta (žiemą)

# SOH ribos
SOH_WARNING  = 85.0    # % — įspėjimas
SOH_CRITICAL = 75.0    # % — rimta degradacija

# Efektyvumo riba
EFFICIENCY_MIN = 88.0  # % — žemiau = anomalija

# Ciklų perspėjimas
CYCLES_WARNING = 3000  # LFP baterijoms ~6000 ciklų

# Tikri Solis Modbus entity (solis_s6_eh3p_*). Ciklų sensoriaus inverteris
# neturi — ekvivalentiniai ciklai skaičiuojami iš bendros įkrovos energijos:
# ciklai ≈ total_charge_kwh / naudingoji talpa (14.4 kWh = 16 kWh × 90%,
# ruožas 10–100%; BMS fizinis dugnas 10%, 2026-07-14 — suderinta su
# energy_manager.py ir sensor.battery_equivalent_cycles).
BATTERY_USABLE_KWH = 14.4

SENSOR = {
    "temp":         "sensor.solis_s6_eh3p_battery_temperature_bms",
    "soc":          "sensor.solis_s6_eh3p_battery_soc",
    "soh":          "sensor.solis_s6_eh3p_battery_soh",
    "voltage":      "sensor.solis_s6_eh3p_battery_voltage",
    "current":      "sensor.solis_s6_eh3p_battery_current",
    "charge":       "sensor.solis_s6_eh3p_today_battery_charge_energy",
    "discharge":    "sensor.solis_s6_eh3p_today_battery_discharge_energy",
    "total_charge": "sensor.solis_s6_eh3p_total_battery_charge_energy",
    # Inverterio savivarta iš baterijos (kWh/d.) — kai PV < apkrova,
    # inverterio ~140 W maitinami iš baterijos, bet iškrovimo skaitliuke
    # neužsiskaito. Pridedama prie iškrautos energijos, kad inverterio
    # nuostoliai nebūtų priskirti baterijai.
    "inv_self":     "sensor.inverterio_savivarta_is_baterijos_siandien",
}

# Istorijos failas
# Kelias per __file__ — AppDaemon konteineryje /config rodo į addon'o vidinį
# katalogą, todėl hardcoded /config/appdaemon/... ten neegzistuoja.
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battery_history.json")


# ============================================================
#  KLASĖ
# ============================================================

class BatteryHealth(hass.Hass):

    def initialize(self):
        self.log("BatteryHealth stebėjimas paleidžiamas...")

        self.temp_history     = []
        self.efficiency_data  = []
        self.day_start_soc    = None  # {"date": ..., "soc": ...} — persistuojama faile
        self.alerts_sent      = {}   # kad nesiųstų to paties alert kartotinai

        self.load_history()

        # Temperatūros stebėjimas — kas 5 min
        self.run_every(self.check_temperature, "now", 5 * 60)

        # Paros pradžios SOC — 00:01, kad 23:55 būtų galima atimti
        # baterijoje likusią energiją iš efektyvumo skaičiavimo
        self.run_daily(self.record_day_start_soc, "00:01:00")

        # Efektyvumo skaičiavimas — kas dieną 23:55
        self.run_daily(self.calculate_daily_efficiency, "23:55:00")

        # SOH ir ciklų patikra — kasdien 08:00; metodas vykdo tik pirmadienį.
        # Ši AppDaemon versija neturi run_weekly(), todėl savaitės diena
        # tikrinama pačiame callback ir išlaikomas vietinis 08:00 per DST.
        self.run_daily(self.check_long_term_health, "08:00:00")

        # Pilna ataskaita — kas mėnesį 1-ą dieną
        self.run_daily(self.monthly_report, "09:00:00")

        self.log("BatteryHealth paleistas.")

    # ============================================================
    #  PAGALBINĖS FUNKCIJOS
    # ============================================================

    def get_float(self, key, default=0.0):
        try:
            val = self.get_state(SENSOR[key])
            if val in (None, "unavailable", "unknown"):
                return default
            return float(val)
        except (ValueError, TypeError):
            return default

    def send_alert(self, alert_id, message, title="Kaupiklio įspėjimas", level="WARNING"):
        """Siunčia įspėjimą — vieną kartą per dieną tą patį alert_id."""
        today = datetime.now().date().isoformat()
        key = f"{alert_id}_{today}"

        if key in self.alerts_sent:
            return

        self.alerts_sent[key] = True
        self.log(f"[ALERT] {title}: {message}", level=level)

        try:
            self.call_service(
                "notify/persistent_notification",
                title=f"🔋 {title}",
                message=message
            )
        except Exception as e:
            self.log(f"Pranešimo klaida: {e}")

    def load_history(self):
        """Įkelia išsaugotą istoriją iš failo."""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    data = json.load(f)
                    self.efficiency_data = data.get("efficiency", [])
                    self.day_start_soc   = data.get("day_start_soc")
                    self.log(f"Istorija įkelta: {len(self.efficiency_data)} dienų duomenys")
        except Exception as e:
            self.log(f"Istorijos įkėlimo klaida: {e}")
            self.efficiency_data = []

    def save_history(self):
        """Išsaugo istoriją į failą."""
        try:
            # Palikti tik paskutinius 365 įrašus
            data = {
                "efficiency": self.efficiency_data[-365:],
                "day_start_soc": self.day_start_soc,
                "updated": datetime.now().isoformat()
            }
            with open(HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log(f"Istorijos išsaugojimo klaida: {e}")

    # ============================================================
    #  TEMPERATŪROS STEBĖJIMAS
    # ============================================================

    def check_temperature(self, kwargs):
        """Tikrina baterijos temperatūrą kas 5 min."""
        temp = self.get_float("temp")

        if temp == 0.0:
            return  # sensoriaus nėra

        self.temp_history.append({
            "time": datetime.now().isoformat(),
            "temp": temp
        })

        # Palikti tik paskutines 24 val (288 reikšmės)
        if len(self.temp_history) > 288:
            self.temp_history.pop(0)

        # Kritinė temperatūra
        if temp >= TEMP_CRITICAL:
            self.send_alert(
                "temp_critical",
                f"⚠️ KRITINĖ baterijos temperatūra: {temp:.1f}°C!\n"
                f"Maksimali leistina: {TEMP_CRITICAL}°C\n"
                f"Patikrinkite vėdinimą ir apkrovą!",
                title="Kritinė temperatūra",
                level="ERROR"
            )

        # Įspėjamoji temperatūra
        elif temp >= TEMP_WARNING:
            self.send_alert(
                "temp_warning",
                f"⚠️ Aukšta baterijos temperatūra: {temp:.1f}°C\n"
                f"Rekomenduojama riba: {TEMP_WARNING}°C",
                level="WARNING"
            )

        # Per žema temperatūra
        elif temp <= TEMP_LOW:
            self.send_alert(
                "temp_low",
                f"🥶 Žema baterijos temperatūra: {temp:.1f}°C\n"
                f"Žema temperatūra mažina efektyvumą ir talpą.",
                level="WARNING"
            )

    # ============================================================
    #  DIENOS EFEKTYVUMO SKAIČIAVIMAS
    # ============================================================

    def record_day_start_soc(self, kwargs):
        """Įsimena SOC paros pradžioje (persistuojama istorijos faile)."""
        soc = self.get_float("soc", default=-1.0)
        if soc < 0:
            self.log("[HEALTH] SOC nepasiekiamas 00:01 — paros pradžios taškas nefiksuotas.")
            return
        self.day_start_soc = {"date": datetime.now().date().isoformat(), "soc": soc}
        self.save_history()

    def calculate_daily_efficiency(self, kwargs):
        """
        Apskaičiuoja dienos įkrovimo/iškrovimo efektyvumą pagal energijos balansą:
        Efektyvumas = (iškrauta + inverterio savivarta + ΔSOC energija) / įkrauta * 100%
        ΔSOC narys būtinas — be jo diena, kurios pabaigoje baterija pilnesnė nei
        ryte, atrodo kaip „nuostolis" (pvz., 2026-07-16: 18.3 įkrauta / 12.2
        iškrauta davė fiktyvius 66.7 %, nors ~6.4 kWh tiesiog liko baterijoje).
        LFP baterija turėtų būti ~92-98%.
        """
        charged    = self.get_float("charge")
        discharged = self.get_float("discharge")
        inv_self   = self.get_float("inv_self")

        if charged < 0.5:
            self.log("[HEALTH] Per mažai įkrauta šiandien, efektyvumo neskaičiuojame.")
            return

        today = datetime.now().date().isoformat()
        snap = self.day_start_soc or {}
        if snap.get("date") != today:
            self.log("[HEALTH] Nėra paros pradžios SOC — efektyvumas šiandien neskaičiuojamas.")
            return

        soc_delta_kwh = (self.get_float("soc") - snap["soc"]) / 100 * BATTERY_CAPACITY_KWH
        efficiency = battery_efficiency_percent(
            charged, discharged, inv_self, soc_delta_kwh
        )
        if efficiency is None:
            self.log("[HEALTH] Efektyvumo skaičiavimui gauti netinkami duomenys.")
            return
        efficiency_valid = is_plausible_efficiency(efficiency)

        today_data = {
            "date":       today,
            "charged":    round(charged, 2),
            "discharged": round(discharged, 2),
            "inv_self":   round(inv_self, 2),
            "soc_delta_kwh": round(soc_delta_kwh, 2),
            "efficiency": round(efficiency, 1),
            "valid": efficiency_valid,
            "temp_max":   round(max([h["temp"] for h in self.temp_history], default=0), 1),
            "temp_min":   round(min([h["temp"] for h in self.temp_history], default=0), 1),
        }

        self.efficiency_data.append(today_data)
        self.save_history()

        self.log(
            f"[HEALTH] Dienos efektyvumas: {efficiency:.1f}% "
            f"(įkrauta: {charged:.1f} kWh, iškrauta: {discharged:.1f} kWh, "
            f"inverterio savivarta: {inv_self:.2f} kWh, ΔSOC: {soc_delta_kwh:+.2f} kWh)"
        )

        # Nefizinis >105 % rezultatas yra duomenų kokybės problema, ne
        # baterijos sveikatos matas. Įrašą paliekame auditui, bet trendams
        # ir mėnesinei ataskaitai jo nenaudojame.
        if not efficiency_valid:
            self.send_alert(
                "efficiency_data_invalid",
                f"⚠️ Nefizinis baterijos balanso rezultatas: {efficiency:.1f}%\n"
                f"Leistina matavimo tolerancija: iki {EFFICIENCY_MAX_VALID:.0f}%\n"
                "Įrašas paliktas istorijoje, bet neįtrauktas į baterijos degradacijos trendą.",
                title="Baterijos matavimo duomenų kokybė",
                level="WARNING"
            )
        elif efficiency < EFFICIENCY_MIN and charged > 2.0:
            self.send_alert(
                "efficiency_low",
                f"📉 Žemas baterijos efektyvumas: {efficiency:.1f}%\n"
                f"Norma: >{EFFICIENCY_MIN}%\n"
                f"Įkrauta: {charged:.1f} kWh, Iškrauta: {discharged:.1f} kWh, "
                f"ΔSOC: {soc_delta_kwh:+.1f} kWh\n"
                f"Gali reikšti baterijos degradaciją.",
                level="WARNING"
            )

    # ============================================================
    #  ILGALAIKĖ SVEIKATA
    # ============================================================

    def get_equivalent_cycles(self):
        """Ekvivalentiniai ciklai = bendra įkrovos energija / naudingoji talpa."""
        total_charge = self.get_float("total_charge", default=0.0)
        return total_charge / BATTERY_USABLE_KWH if total_charge > 0 else 0.0

    def check_long_term_health(self, kwargs):
        """Savaitinė ilgalaikės sveikatos patikra (pirmadienį 08:00)."""
        if datetime.now().weekday() != 0:
            return

        soh    = self.get_float("soh", default=100.0)
        cycles = self.get_equivalent_cycles()

        self.log(f"[HEALTH] SOH: {soh:.1f}%, Ciklai: {cycles:.0f}")

        # SOH patikra
        if soh > 0:
            if soh <= SOH_CRITICAL:
                self.send_alert(
                    "soh_critical",
                    f"🔋 Kritiškai žemas SOH: {soh:.1f}%\n"
                    f"Baterija prarado daugiau nei 25% talpos.\n"
                    f"Rekomenduojama kreiptis į aptarnavimą.",
                    title="Baterijos degradacija",
                    level="ERROR"
                )
            elif soh <= SOH_WARNING:
                self.send_alert(
                    "soh_warning",
                    f"🔋 SOH: {soh:.1f}% — pradeda mažėti talpa.\n"
                    f"Stebėkite dinamiką.",
                    level="WARNING"
                )

        # Ciklų patikra
        if cycles >= CYCLES_WARNING:
            self.send_alert(
                "cycles_warning",
                f"🔄 Ciklų skaičius: {cycles:.0f}\n"
                f"Perspėjimo riba: {CYCLES_WARNING}\n"
                f"LFP baterijos tarnavimo laikas ~6000 ciklų.",
                level="WARNING"
            )

        # Efektyvumo trendas (28 paskutiniai fiziškai galimi matavimai).
        valid_data = plausible_efficiency_rows(self.efficiency_data)
        if len(valid_data) >= 28:
            recent    = [d["efficiency"] for d in valid_data[-7:]]
            older     = [d["efficiency"] for d in valid_data[-28:-7]]
            avg_recent = sum(recent) / len(recent)
            avg_older  = sum(older) / len(older)
            drop = avg_older - avg_recent

            if drop > 3.0:
                self.send_alert(
                    "efficiency_trend",
                    f"📉 Efektyvumo mažėjimo tendencija:\n"
                    f"Prieš mėnesį: {avg_older:.1f}%\n"
                    f"Šią savaitę: {avg_recent:.1f}%\n"
                    f"Kritimas: {drop:.1f}%",
                    level="WARNING"
                )

    # ============================================================
    #  MĖNESINĖ ATASKAITA
    # ============================================================

    def monthly_report(self, kwargs):
        """Mėnesinė kaupiklio sveikatos ataskaita — tik 1-ą mėnesio dieną."""
        if datetime.now().day != 1:
            return

        if len(self.efficiency_data) < 7:
            return

        last_30 = plausible_efficiency_rows(self.efficiency_data[-30:])
        efficiencies = [d["efficiency"] for d in last_30]
        temps_max    = [d["temp_max"] for d in last_30 if d["temp_max"] > 0]

        if not efficiencies:
            return

        avg_eff  = sum(efficiencies) / len(efficiencies)
        avg_temp = sum(temps_max) / len(temps_max) if temps_max else 0
        max_temp = max(temps_max) if temps_max else 0

        soh    = self.get_float("soh", default=0)
        cycles = self.get_equivalent_cycles()

        report = (
            f"📊 Mėnesinė kaupiklio ataskaita\n"
            f"{'─' * 30}\n"
            f"Vidutinis efektyvumas: {avg_eff:.1f}%\n"
            f"Vidutinė temperatūra: {avg_temp:.1f}°C\n"
            f"Maksimali temperatūra: {max_temp:.1f}°C\n"
        )
        if soh > 0:
            report += f"SOH (sveikata): {soh:.1f}%\n"
        if cycles > 0:
            report += f"Ekvivalentiniai ciklai: {cycles:.0f}\n"

        try:
            self.call_service("notify/persistent_notification",
                              title="🔋 Kaupiklio ataskaita", message=report)
        except Exception as e:
            self.log(f"Ataskaitos siuntimo klaida: {e}")
