"""
weekly_report.py — Savaitinė energijos ataskaita
=================================================
Kas sekmadienį 20:00 siunčia ataskaitą į
Telegram arba Facebook Messenger (per Callmebot).

Ataskaita apima:
  - Savaitės generacija iš saulės kWh
  - Namų suvartojimas kWh
  - Atidavimas į ESO tinklą kWh
  - Boilerio suvartotas energijos kWh
  - Kaupiklio ciklai
  - Solcast prognozės tikslumas %
  - Sutaupyta € (skaičiuojama pagal tarifą)
  - Palyginimas su praėjusia savaite

Reikalingi HA sensoriai:
  sensor.solis_total_pv_power          — generacija kWh (utility meter)
  sensor.solis_total_energy_purchased  — pirkta iš tinklo kWh
  sensor.solis_total_energy_sold       — parduota į tinklą kWh
  sensor.solis_house_load_total        — namų suvartojimas kWh
  sensor.solcast_forecast_this_week    — Solcast savaitės prognozė
  sensor.solis_battery_cycles          — ciklai
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import json
import os


# ============================================================
#  KONFIGŪRACIJA
# ============================================================

ELECTRICITY_PRICE_BUY  = 0.18   # €/kWh — kaina perkant iš tinklo
ELECTRICITY_PRICE_SELL = 0.08   # €/kWh — kaina parduodant į tinklą

REPORT_FILE = "/config/appdaemon/apps/weekly_reports.json"

# Telegram arba Callmebot (Facebook Messenger)
NOTIFY_SERVICE = "notify/telegram"   # pakeisti pagal savo

SENSOR = {
    "pv_week":         "sensor.energy_pv_week",          # utility meter
    "grid_buy_week":   "sensor.energy_grid_buy_week",    # utility meter
    "grid_sell_week":  "sensor.energy_grid_sell_week",   # utility meter
    "house_week":      "sensor.energy_house_week",       # utility meter
    "boiler_week":     "sensor.energy_boiler_week",      # utility meter (ESP32)
    "solcast_week":    "sensor.solcast_forecast_this_week",
    "cycles":          "sensor.solis_battery_cycles",
    "soc":             "sensor.solis_battery_soc",
    "pv_actual_week":  "sensor.energy_pv_week",          # realios generacijos utility meter
}


# ============================================================
#  KLASĖ
# ============================================================

class WeeklyReport(hass.Hass):

    def initialize(self):
        self.log("WeeklyReport paleidžiamas...")

        self.reports = self.load_reports()

        # Savaitinė ataskaita — sekmadieniais 20:00
        self.run_weekly(self.send_weekly_report, "sun", "20:00:00")

        # Dienos mini suvestinė — kas dieną 21:00
        self.run_daily(self.send_daily_summary, "21:00:00")

        self.log("WeeklyReport paleistas.")

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

    def load_reports(self):
        try:
            if os.path.exists(REPORT_FILE):
                with open(REPORT_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            self.log(f"Ataskaitų įkėlimo klaida: {e}")
        return []

    def save_reports(self):
        try:
            with open(REPORT_FILE, "w") as f:
                json.dump(self.reports[-52:], f, indent=2)  # palikti metų ataskaitas
        except Exception as e:
            self.log(f"Ataskaitų išsaugojimo klaida: {e}")

    def send_message(self, message, title="Energijos ataskaita"):
        try:
            self.call_service(NOTIFY_SERVICE, title=title, message=message)
            self.log(f"Pranešimas išsiųstas: {title}")
        except Exception as e:
            self.log(f"Pranešimo klaida: {e}", level="WARNING")

    def trend_arrow(self, current, previous):
        """Grąžina rodyklę pagal pokytį."""
        if previous == 0:
            return ""
        diff = current - previous
        pct  = (diff / previous) * 100
        if diff > 0:
            return f"▲ +{pct:.0f}%"
        elif diff < 0:
            return f"▼ {pct:.0f}%"
        return "→ 0%"

    # ============================================================
    #  SAVAITINĖ ATASKAITA
    # ============================================================

    def send_weekly_report(self, kwargs):
        """Pagrindinė savaitinė ataskaita."""
        now = datetime.now()
        week_start = (now - timedelta(days=6)).strftime("%m-%d")
        week_end   = now.strftime("%m-%d")

        # Gauti šios savaitės duomenis
        pv_kwh       = self.get_float("pv_week")
        grid_buy     = self.get_float("grid_buy_week")
        grid_sell    = self.get_float("grid_sell_week")
        house_kwh    = self.get_float("house_week")
        boiler_kwh   = self.get_float("boiler_week")
        solcast_pred = self.get_float("solcast_week")
        cycles       = self.get_float("cycles")
        soc          = self.get_float("soc")

        # Finansinis skaičiavimas
        saved_from_grid = pv_kwh * ELECTRICITY_PRICE_BUY
        earned_from_sell = grid_sell * ELECTRICITY_PRICE_SELL
        cost_from_grid   = grid_buy * ELECTRICITY_PRICE_BUY
        net_saving       = saved_from_grid + earned_from_sell - cost_from_grid

        # Solcast tikslumas
        accuracy_str = ""
        if solcast_pred > 0 and pv_kwh > 0:
            accuracy = (pv_kwh / solcast_pred) * 100
            accuracy_str = f"\nSolcast tikslumas: {accuracy:.0f}%"

        # Palyginimas su praėjusia savaite
        prev_week = self.reports[-1] if self.reports else None
        comparison = ""
        if prev_week:
            pv_trend    = self.trend_arrow(pv_kwh, prev_week.get("pv_kwh", 0))
            save_trend  = self.trend_arrow(net_saving, prev_week.get("net_saving", 0))
            comparison  = f"\nPalyginimas su praėjusia savaite:\n  Generacija: {pv_trend}\n  Taupymas: {save_trend}"

        # Savarankiškumo procentas
        if house_kwh > 0:
            self_sufficiency = min(100, (pv_kwh / house_kwh) * 100)
            suffix_str = f"\nSavarankiškumas: {self_sufficiency:.0f}%"
        else:
            suffix_str = ""

        report = (
            f"☀️ Savaitės energijos ataskaita {week_start}–{week_end}\n"
            f"{'━' * 32}\n"
            f"\n📊 ENERGIJA\n"
            f"  Pagaminta saulės:  {pv_kwh:.1f} kWh\n"
            f"  Namų suvartojimas: {house_kwh:.1f} kWh\n"
            f"  Boileris:          {boiler_kwh:.1f} kWh\n"
            f"  Pirkta iš tinklo:  {grid_buy:.1f} kWh\n"
            f"  Atiduota į tinklą: {grid_sell:.1f} kWh\n"
            f"{suffix_str}"
            f"\n💶 FINANSAI\n"
            f"  Sutaupyta (saulė): {saved_from_grid:.2f} €\n"
            f"  Uždirbta (tinklas):{earned_from_sell:.2f} €\n"
            f"  Sumokėta (tinklas):{cost_from_grid:.2f} €\n"
            f"  Grynasis taupymas: {net_saving:.2f} €\n"
            f"\n🔋 KAUPIKLIS\n"
            f"  SOC šiuo metu:     {soc:.0f}%\n"
            f"  Ciklai iš viso:    {cycles:.0f}"
            f"{accuracy_str}"
            f"{comparison}"
        )

        self.send_message(report, title="☀️ Savaitės ataskaita")

        # Išsaugoti šios savaitės duomenis
        self.reports.append({
            "week":       f"{week_start}–{week_end}",
            "pv_kwh":     round(pv_kwh, 2),
            "house_kwh":  round(house_kwh, 2),
            "grid_buy":   round(grid_buy, 2),
            "grid_sell":  round(grid_sell, 2),
            "net_saving": round(net_saving, 2),
            "cycles":     cycles,
            "date":       now.isoformat(),
        })
        self.save_reports()

    # ============================================================
    #  DIENOS MINI SUVESTINĖ
    # ============================================================

    def send_daily_summary(self, kwargs):
        """
        Trumpa dienos suvestinė kas vakarą 21:00.
        Siunčia tik jei generacija buvo reikšminga (>1 kWh).
        """
        # Dienos utility meter sensoriai
        pv_today     = self._get_today("sensor.energy_pv_today")
        house_today  = self._get_today("sensor.energy_house_today")
        sell_today   = self._get_today("sensor.energy_grid_sell_today")
        buy_today    = self._get_today("sensor.energy_grid_buy_today")
        boiler_today = self._get_today("sensor.energy_boiler_today")
        soc          = self.get_float("soc")

        if pv_today < 0.5:
            return  # debesuota diena, nesiųsti

        saved = pv_today * ELECTRICITY_PRICE_BUY
        net   = saved + sell_today * ELECTRICITY_PRICE_SELL - buy_today * ELECTRICITY_PRICE_BUY

        msg = (
            f"🌤 Dienos suvestinė {datetime.now().strftime('%m-%d')}\n"
            f"Saulė: {pv_today:.1f} kWh | Namai: {house_today:.1f} kWh\n"
            f"Boileris: {boiler_today:.1f} kWh | Tinklas: +{sell_today:.1f}/-{buy_today:.1f}\n"
            f"Taupymas: {net:.2f} € | SOC: {soc:.0f}%"
        )
        self.send_message(msg, title="🌤 Dienos suvestinė")

    def _get_today(self, entity_id, default=0.0):
        try:
            val = self.get_state(entity_id)
            return float(val) if val not in (None, "unavailable", "unknown") else default
        except (ValueError, TypeError):
            return default
