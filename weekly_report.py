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

Duomenų šaltiniai:
  Savaitė — utility meter'iai energy_*_week (configuration.yaml, šaltiniai
  Solis Modbus total skaitliukai, resetinasi pirmadienį 00:00).
  Diena — Solis Modbus today skaitliukai tiesiogiai.
  Ciklai — ekvivalentiniai, iš total_battery_charge_energy / 14.4 kWh.
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import json
import os


# ============================================================
#  KONFIGŪRACIJA
# ============================================================

# Fallback kainos — realios skaitomos iš input_number.electricity_price_buy/
# sell (dashboard), šios naudojamos tik kol input_number nenustatytas.
ELECTRICITY_PRICE_BUY  = 0.18   # €/kWh — kaina perkant iš tinklo
ELECTRICITY_PRICE_SELL = 0.08   # €/kWh — kaina parduodant į tinklą

# Kelias per __file__ — AppDaemon konteineryje /config rodo į addon'o vidinį
# katalogą, todėl hardcoded /config/appdaemon/... ten neegzistuoja.
REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weekly_reports.json")

# HA persistent notifications (Telegram nesukonfigūruotas; atsiradus — pakeisti čia)
NOTIFY_SERVICE = "notify/persistent_notification"

# Ekvivalentiniai ciklai = bendra įkrovos energija / naudingoji talpa.
# 14.4 = 16 kWh × 90% (ruožas 10–100%; BMS fizinis dugnas 10%, 2026-07-14) —
# suderinta su energy_manager.py ir sensor.battery_equivalent_cycles.
BATTERY_USABLE_KWH = 14.4

SENSOR = {
    # Savaitiniai utility meter'iai (configuration.yaml → utility_meter:,
    # šaltiniai — Solis Modbus total skaitliukai; resetinasi pirmadienį 00:00)
    # 2026-07-22: utility_meter'ių entity_id = vardo slug'as (jie turi name:),
    # NE rakto. Taisyta iš energy_*_week į tikrus savaites_* vardus.
    "pv_week":         "sensor.savaites_pv_generacija",
    "grid_buy_week":   "sensor.savaites_pirkimas_is_tinklo",
    "grid_sell_week":  "sensor.savaites_pardavimas_i_tinkla",
    "house_week":      "sensor.savaites_namu_suvartojimas",
    # Boilerio dar nėra (ESP32 neprijungtas) — kol entity neegzistuoja, bus 0
    "boiler_week":     "sensor.energy_boiler_week",
    # Solcast savaitės prognozės sensoriaus nėra — tikslumo eilutė praleidžiama
    "solcast_week":    "sensor.solcast_forecast_this_week",
    "soc":             "sensor.solis_s6_eh3p_battery_soc",
    "total_charge":    "sensor.solis_s6_eh3p_total_battery_charge_energy",
}

# Dienos suvestinei — tiesiogiai Solis Modbus dienos skaitliukai
DAILY_SENSOR = {
    "pv":     "sensor.solis_s6_eh3p_pv_today_energy_generation",
    "house":  "sensor.solis_s6_eh3p_household_load_today_energy",
    "sell":   "sensor.solis_s6_eh3p_today_energy_fed_into_grid",
    "buy":    "sensor.solis_s6_eh3p_today_energy_imported_from_grid",
    "boiler": "sensor.energy_boiler_today",   # ESP32 ateičiai; kol nėra — 0
}


# ============================================================
#  KLASĖ
# ============================================================

class WeeklyReport(hass.Hass):

    def initialize(self):
        self.log("WeeklyReport paleidžiamas...")

        self.reports = self.load_reports()

        # Savaitinė ataskaita — sekmadieniais 20:00 (savaitinis utility meter
        # resetinasi pirmadienį 00:00, tad sekmadienio vakarą savaitė ~pilna).
        # Anksčiau buvo run_every nuo paleidimo — siųsdavo atsitiktiniu laiku.
        self.run_daily(self.maybe_send_weekly_report, "20:00:00")

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

    def _price(self, entity_id, default):
        """0 — TEISĖTA kaina (ESO pasaugojimo schema: buy=0.0/sell=0.25);
        fallback tik kai entity nepasiekiamas."""
        try:
            raw = self.get_state(entity_id)
            if raw in (None, "unavailable", "unknown", ""):
                return default
            return max(float(raw), 0.0)
        except (ValueError, TypeError):
            return default

    def price_buy(self):
        return self._price("input_number.electricity_price_buy", ELECTRICITY_PRICE_BUY)

    def price_sell(self):
        return self._price("input_number.electricity_price_sell", ELECTRICITY_PRICE_SELL)

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

    def maybe_send_weekly_report(self, kwargs):
        """Kasdien 20:00 — siunčia tik sekmadienį."""
        if datetime.now().weekday() == 6:
            self.send_weekly_report(kwargs)

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
        soc          = self.get_float("soc")
        total_charge = self.get_float("total_charge")
        cycles       = total_charge / BATTERY_USABLE_KWH if total_charge > 0 else 0

        # Finansinis skaičiavimas. Savo reikmėms panaudota saulė = PV − parduota
        # (parduotoji vertinama pardavimo kaina žemiau — kitaip eksportuota kWh
        # užskaitoma dvigubai; formulė kaip sensor.energy_savings_today).
        saved_from_grid = max(pv_kwh - grid_sell, 0.0) * self.price_buy()
        earned_from_sell = grid_sell * self.price_sell()
        cost_from_grid   = grid_buy * self.price_buy()
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
            f"  Ekvival. ciklai:   {cycles:.0f}"
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
        # Solis Modbus dienos skaitliukai
        pv_today     = self._get_today(DAILY_SENSOR["pv"])
        house_today  = self._get_today(DAILY_SENSOR["house"])
        sell_today   = self._get_today(DAILY_SENSOR["sell"])
        buy_today    = self._get_today(DAILY_SENSOR["buy"])
        boiler_today = self._get_today(DAILY_SENSOR["boiler"])
        soc          = self.get_float("soc")

        if pv_today < 0.5:
            return  # debesuota diena, nesiųsti

        # Savo reikmėms = PV − parduota (be dvigubo eksporto užskaitymo)
        saved = max(pv_today - sell_today, 0.0) * self.price_buy()
        net   = saved + sell_today * self.price_sell() - buy_today * self.price_buy()

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
