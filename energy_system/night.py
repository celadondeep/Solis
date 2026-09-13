"""Nakties ekonomikos ir ryto/vakaro SOC planavimo periferija."""

from datetime import datetime, timedelta

from energy_system.planner import target_soc_for_room


def build_night_mixin(profile):
    KWH_PER_SOC = profile["KWH_PER_SOC"]
    MIN_SOC = profile["MIN_SOC"]
    INVERTER_SELF_KW = profile["INVERTER_SELF_KW"]
    INVERTER_IDLE_W = profile["INVERTER_IDLE_W"]
    INVERTER_OFF_W = profile["INVERTER_OFF_W"]
    PRICE_BUY_DEFAULT = profile["PRICE_BUY_DEFAULT"]
    PRICE_SELL_DEFAULT = profile["PRICE_SELL_DEFAULT"]
    MORNING_PV_THRESHOLD_KW = profile["MORNING_PV_THRESHOLD_KW"]
    SOC_TARGET_CHARGE = profile["SOC_TARGET_CHARGE"]
    BATTERY_DISCHARGE_SIGN = profile["BATTERY_DISCHARGE_SIGN"]
    SENSOR = profile["SENSOR"]
    OUTPUT = profile["OUTPUT"]
    SITE_KEY = profile["KEY"]

    class NightPlanningMixin:

        def consumption_until_time(self, target_dt):
            """Namų suvartojimas (kWh) nuo dabar iki target_dt pagal valandinį
            vartojimo profilį + inverterio savivarta. Naudojama naktinio iškrovimo
            lango dydžiui: kiek baterijos iškraus PATYS NAMAI iki ryto gamybos —
            tiek mažiau reikia eksportuoti 1 kW slotu, tad langas startuoja vėliau
            ir baterija dugne pastovi kuo trumpiau."""
            now = datetime.now().astimezone()
            if not target_dt or target_dt <= now:
                return 0.0
            prof = self.get_state(SENSOR["consumption_profile"],
                                  attribute="hourly_kwh")
            daily = self.get_daily_consumption()

            def kwh_hour(h):
                try:
                    if prof and len(prof) == 24:
                        return float(prof[h])
                except (TypeError, ValueError, IndexError):
                    pass
                return daily / 24.0

            total = 0.0
            t = now
            while t < target_dt:
                nxt = min(target_dt,
                          (t + timedelta(hours=1)).replace(minute=0, second=0,
                                                            microsecond=0))
                frac = (nxt - t).total_seconds() / 3600.0
                total += kwh_hour(t.hour) * frac
                t = nxt
            hours = (target_dt - now).total_seconds() / 3600.0
            return round(total + INVERTER_SELF_KW * hours, 2)


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
                batt_w = self.get_sensor_float(SENSOR["battery_power"])
                batt_discharge_w = batt_w * BATTERY_DISCHARGE_SIGN
                discharge_kw = (batt_discharge_w if batt_discharge_w > 50
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
                OUTPUT["night_economics"],
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
                OUTPUT["shutdown_eta"],
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
                    OUTPUT["morning_on_time"],
                    state=on_time.isoformat(),
                    attributes={
                        "friendly_name": "Inverterio ryto įjungimas",
                        "device_class": "timestamp",
                        "icon": "mdi:weather-sunset-up",
                        "laikas": on_time.strftime("%H:%M"),
                        "pv_slenkstis_kw": MORNING_PV_THRESHOLD_KW,
                    }
                )

            # Suvartojimas iki ryto gamybos — naktinio iškrovimo lango dydžiui.
            cons_to_prod = self.consumption_until_time(on_time)
            self.set_state(
                OUTPUT["cons_until_production"],
                state=str(cons_to_prod),
                attributes={
                    "friendly_name": "Suvartojimas iki ryto gamybos",
                    "unit_of_measurement": "kWh",
                    "state_class": "measurement",
                    "icon": "mdi:home-clock",
                    "iki_laiko": on_time.strftime("%H:%M") if on_time else None,
                }
            )

            self.set_state(
                OUTPUT["correction_factor"],
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


        def morning_room_check(self, kwargs):
            """Ryte tik sumažina tikslą, jei prognozė reikalauja vietos."""
            if self.is_storm_mode():
                return
            soc = self.get_optional_float("soc")
            if soc is None:
                self.log(
                    f"[RYTO VIETA:{SITE_KEY}] SOC nepasiekiamas — tikslas nekeičiamas",
                    level="WARNING",
                )
                return
            if not self.get_state(
                SENSOR["solcast_today_total"], attribute="detailedForecast"
            ):
                self.log(
                    f"[RYTO VIETA:{SITE_KEY}] Prognozė nepasiekiama — "
                    "tikslas nekeičiamas",
                    level="WARNING",
                )
                return

            need, _export, pv_plan = self.battery_room_needed("today")
            headroom = max(0.0, (100.0 - soc) * KWH_PER_SOC)
            self.publish_realization()
            if need <= headroom + 0.3:
                self.log(
                    f"[RYTO VIETA:{SITE_KEY}] OK: reikia {need:.1f} kWh, "
                    f"laisva {headroom:.1f} kWh"
                )
                return

            new_target = target_soc_for_room(
                need, KWH_PER_SOC, self.plan_policy
            )
            if new_target < int(self.last_target_soc):
                self.log(
                    f"[RYTO VIETA:{SITE_KEY}] {self.last_target_soc}% -> "
                    f"{new_target}% (PV planas {pv_plan:.1f} kWh)"
                )
                self.last_target_soc = new_target
                self.save_target_soc()
                self.publish_status()
                self.compute_plan()


        def evening_discharge_cycle(self, kwargs):
            """Perskaičiuoja ryto tikslą pagal realizavimui reikalingą vietą."""
            if not self.get_state(
                SENSOR["solcast_tomorrow"], attribute="detailedForecast"
            ):
                self.log(
                    f"[VAKARO:{SITE_KEY}] Rytojaus prognozė nepasiekiama — "
                    "paliekamas ankstesnis tikslas",
                    level="WARNING",
                )
                return

            soc = self.get_optional_float("soc")
            need, export_est, pv_plan = self.battery_room_needed("tomorrow")
            target_soc = target_soc_for_room(
                need, KWH_PER_SOC, self.plan_policy
            )
            if self.is_storm_mode():
                target_soc = 100

            self.log(
                f"[VAKARO:{SITE_KEY}] PV {pv_plan:.1f} kWh, eksportas "
                f"~{export_est:.1f} kWh, vieta {need:.1f} kWh -> "
                f"target {target_soc}%, SOC "
                f"{soc if soc is not None else 'unknown'}"
            )
            self.last_target_soc = target_soc
            self.save_target_soc()
            self.publish_status()
            self.compute_plan()



    return NightPlanningMixin
