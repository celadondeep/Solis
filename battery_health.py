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

Reikalingi HA sensoriai:
  sensor.solis_battery_temperature     — baterijos temperatūra °C
  sensor.solis_battery_soc             — SOC %
  sensor.solis_battery_soh             — SOH % (jei palaiko)
  sensor.solis_battery_cycles          — ciklų skaičius
  sensor.solis_battery_voltage         — įtampa V
  sensor.solis_battery_current         — srovė A
  sensor.solis_today_battery_charge    — šiandien įkrauta kWh
  sensor.solis_today_battery_discharge — šiandien iškrauta kWh
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import json
import os


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

SENSOR = {
    "temp":      "sensor.solis_battery_temperature",
    "soc":       "sensor.solis_battery_soc",
    "soh":       "sensor.solis_battery_soh",
    "cycles":    "sensor.solis_battery_cycles",
    "voltage":   "sensor.solis_battery_voltage",
    "current":   "sensor.solis_battery_current",
    "charge":    "sensor.solis_today_battery_charge",
    "discharge": "sensor.solis_today_battery_discharge",
}

# Istorijos failas
HISTORY_FILE = "/config/appdaemon/apps/battery_history.json"


# ============================================================
#  KLASĖ
# ============================================================

class BatteryHealth(hass.Hass):

    def initialize(self):
        self.log("BatteryHealth stebėjimas paleidžiamas...")

        self.temp_history     = []
        self.efficiency_data  = []
        self.alerts_sent      = {}   # kad nesiųstų to paties alert kartotinai

        self.load_history()

        # Temperatūros stebėjimas — kas 5 min
        self.run_every(self.check_temperature, "now", 5 * 60)

        # Efektyvumo skaičiavimas — kas dieną 23:55
        self.run_daily(self.calculate_daily_efficiency, "23:55:00")

        # SOH ir ciklų patikra — kas savaitę pirmadienį 08:00
        self.run_weekly(self.check_long_term_health, "mon", "08:00:00")

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
                "notify/telegram",
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

    def calculate_daily_efficiency(self, kwargs):
        """
        Apskaičiuoja dienos įkrovimo/iškrovimo efektyvumą.
        Efektyvumas = iškrauta / įkrauta * 100%
        LFP baterija turėtų būti ~95-98%
        """
        charged    = self.get_float("charge")
        discharged = self.get_float("discharge")

        if charged < 0.5:
            self.log("[HEALTH] Per mažai įkrauta šiandien, efektyvumo neskaičiuojame.")
            return

        efficiency = (discharged / charged) * 100 if charged > 0 else 0

        today_data = {
            "date":       datetime.now().date().isoformat(),
            "charged":    round(charged, 2),
            "discharged": round(discharged, 2),
            "efficiency": round(efficiency, 1),
            "temp_max":   round(max([h["temp"] for h in self.temp_history], default=0), 1),
            "temp_min":   round(min([h["temp"] for h in self.temp_history], default=0), 1),
        }

        self.efficiency_data.append(today_data)
        self.save_history()

        self.log(
            f"[HEALTH] Dienos efektyvumas: {efficiency:.1f}% "
            f"(įkrauta: {charged:.1f} kWh, iškrauta: {discharged:.1f} kWh)"
        )

        # Įspėjimas jei efektyvumas per žemas
        if efficiency < EFFICIENCY_MIN and charged > 2.0:
            self.send_alert(
                "efficiency_low",
                f"📉 Žemas baterijos efektyvumas: {efficiency:.1f}%\n"
                f"Norma: >{EFFICIENCY_MIN}%\n"
                f"Įkrauta: {charged:.1f} kWh, Iškrauta: {discharged:.1f} kWh\n"
                f"Gali reikšti baterijos degradaciją.",
                level="WARNING"
            )

    # ============================================================
    #  ILGALAIKĖ SVEIKATA
    # ============================================================

    def check_long_term_health(self, kwargs):
        """Savaitinė ilgalaikės sveikatos patikra."""
        soh    = self.get_float("soh", default=100.0)
        cycles = self.get_float("cycles", default=0)

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

        # Efektyvumo trendas (paskutinės 4 savaitės)
        if len(self.efficiency_data) >= 28:
            recent    = [d["efficiency"] for d in self.efficiency_data[-7:]]
            older     = [d["efficiency"] for d in self.efficiency_data[-28:-7]]
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

        last_30 = self.efficiency_data[-30:]
        efficiencies = [d["efficiency"] for d in last_30 if d["efficiency"] > 0]
        temps_max    = [d["temp_max"] for d in last_30 if d["temp_max"] > 0]

        if not efficiencies:
            return

        avg_eff  = sum(efficiencies) / len(efficiencies)
        avg_temp = sum(temps_max) / len(temps_max) if temps_max else 0
        max_temp = max(temps_max) if temps_max else 0

        soh    = self.get_float("soh", default=0)
        cycles = self.get_float("cycles", default=0)

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
            report += f"Ciklų skaičius: {cycles:.0f}\n"

        try:
            self.call_service("notify/telegram", title="🔋 Kaupiklio ataskaita", message=report)
        except Exception as e:
            self.log(f"Ataskaitos siuntimo klaida: {e}")
