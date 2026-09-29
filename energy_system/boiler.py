"""Namų boilerio strateginė ir taktinė periferija."""

from time import monotonic


def soc_drop_rate_10min(samples):
    """Grąžina faktinį SOC kritimo greitį procentais per 10 minučių."""
    if len(samples) < 2:
        return 0.0
    start_time, start_soc = samples[0]
    end_time, end_soc = samples[-1]
    elapsed = float(end_time) - float(start_time)
    if elapsed <= 0:
        return 0.0
    return (float(start_soc) - float(end_soc)) * 600.0 / elapsed


def build_boiler_mixin(profile):
    KWH_PER_SOC = profile["KWH_PER_SOC"]
    BOILER_POWER_KW = profile["BOILER_POWER_KW"]
    ESO_EXPORT_LIMIT_KW = profile["ESO_EXPORT_LIMIT_KW"]
    INVERTER_SELF_KW = profile["INVERTER_SELF_KW"]
    BOILER_TEMP_MAX = profile["BOILER_TEMP_MAX"]
    BALANCE_MIN_KWH = profile["BALANCE_MIN_KWH"]
    SOC_HIGH_THRESHOLD = profile["SOC_HIGH_THRESHOLD"]
    SOC_TARGET_CHARGE = profile["SOC_TARGET_CHARGE"]
    SOC_DROP_RATE_MAX = profile["SOC_DROP_RATE_MAX"]
    SURPLUS_MIN_KW = profile["SURPLUS_MIN_KW"]
    SENSOR = profile["SENSOR"]

    class BoilerMixin:

        def calculate_soc_drop_rate(self, current_soc):
            """SOC kritimas %/10 min pagal tikrą laiko tarpą, ne mėginių skaičių."""
            now = monotonic()
            self.soc_history.append((now, float(current_soc)))
            self.soc_history = [
                sample for sample in self.soc_history
                if now - sample[0] <= 600.0
            ][-121:]
            return soc_drop_rate_10min(self.soc_history)


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

            # Kiek trūksta iki 95% SOC (naudingoji talpa, 1% = 0.16 kWh)
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



    return BoilerMixin
