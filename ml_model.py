"""
ml_model.py — Mašininio mokymosi suvartojimo prognozė
======================================================
Naudoja scikit-learn Random Forest modelį tikslesniam
namų suvartojimo prognozavimui.

Aktyvuoti tik po 60+ dienų duomenų kaupimo!
(consumption_model.py kaupia duomenis automatiškai)

Įdiegimas:
  pip install scikit-learn numpy pandas --break-system-packages

Features (įvestis):
  - Savaitės diena (0-6)
  - Mėnuo (1-12)
  - Valanda (0-23)
  - Oro temperatūra lauke (°C)
  - Ar šventadienis (0/1)
  - SOC rytas (%)
  - Sezonas (0-3)

Output (išvestis):
  - Prognozuojamas dienos suvartojimas kWh

Modelis perrenkina kas savaitę su naujais duomenimis.
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import json
import os

try:
    import numpy as np
    import pickle
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# ============================================================
#  KONFIGŪRACIJA
# ============================================================

MODEL_FILE       = "/config/appdaemon/apps/ml_consumption_model.pkl"
SCALER_FILE      = "/config/appdaemon/apps/ml_scaler.pkl"
TRAINING_FILE    = "/config/appdaemon/apps/consumption_model.json"
ML_HISTORY_FILE  = "/config/appdaemon/apps/ml_history.json"

MIN_TRAINING_DAYS = 60   # minimalus dienų sk. modeliui treniruoti

SENSOR = {
    "outdoor_temp":  "sensor.outdoor_temperature",   # lauko temperatūra
    "soc":           "sensor.solis_battery_soc",
    "season":        "input_select.energy_season",
    "house_load":    "sensor.solis_house_load",
}

# Lietuvos valstybinės šventės (MM-DD formatas)
LT_HOLIDAYS = {
    "01-01", "02-16", "03-11", "04-20", "04-21",
    "05-01", "06-24", "07-06", "08-15", "11-01",
    "12-24", "12-25", "12-26"
}


# ============================================================
#  KLASĖ
# ============================================================

class MLConsumptionModel(hass.Hass):

    def initialize(self):
        self.log("MLConsumptionModel paleidžiamas...")

        if not ML_AVAILABLE:
            self.log(
                "scikit-learn neįdiegta! Paleisk: "
                "pip install scikit-learn numpy --break-system-packages",
                level="ERROR"
            )
            return

        self.model   = None
        self.scaler  = None
        self.history = self.load_history()

        # Bandyti įkelti išsaugotą modelį
        self.load_model()

        # Kaupti realius duomenis kas valandą
        self.run_every(self.collect_hourly_data, "now", 60 * 60)

        # Perrenkinti modelį kas pirmadienį 03:00
        self.run_weekly(self.retrain_model, "mon", "03:00:00")

        # Eksponuoti prognozę į HA kas valandą
        self.run_every(self.update_ha_prediction, "now+60", 60 * 60)

        # Tikslingumo stebėjimas — kas dieną 00:10
        self.run_daily(self.evaluate_accuracy, "00:10:00")

        self.log("MLConsumptionModel paleistas.")

    # ============================================================
    #  FEATURES INŽINERIJA
    # ============================================================

    def get_outdoor_temp(self):
        try:
            val = self.get_state(SENSOR["outdoor_temp"])
            return float(val) if val not in (None, "unavailable") else 10.0
        except (ValueError, TypeError):
            return 10.0

    def get_soc(self):
        try:
            val = self.get_state(SENSOR["soc"])
            return float(val) if val not in (None, "unavailable") else 50.0
        except (ValueError, TypeError):
            return 50.0

    def is_holiday(self, date=None):
        if date is None:
            date = datetime.now().date()
        return date.strftime("%m-%d") in LT_HOLIDAYS

    def season_to_int(self, season):
        mapping = {"žiema": 0, "pavasaris": 1, "vasara": 2, "ruduo": 3}
        return mapping.get(season, 1)

    def build_features(self, date=None, hour=None, outdoor_temp=None, soc=None):
        """Sukuria features vektorių modeliui."""
        if date is None:
            date = datetime.now().date()
        if hour is None:
            hour = datetime.now().hour
        if outdoor_temp is None:
            outdoor_temp = self.get_outdoor_temp()
        if soc is None:
            soc = self.get_soc()

        try:
            season_str = self.get_state(SENSOR["season"]) or "pavasaris"
        except Exception:
            season_str = "pavasaris"

        features = [
            date.weekday(),              # 0-6
            date.month,                  # 1-12
            hour,                        # 0-23
            outdoor_temp,                # °C
            1 if self.is_holiday(date) else 0,  # šventadienis
            soc,                         # %
            self.season_to_int(season_str),     # 0-3
            # Trigonometrinės transformacijos periodiniams duomenims
            round(__import__('math').sin(2 * __import__('math').pi * date.weekday() / 7), 4),
            round(__import__('math').cos(2 * __import__('math').pi * date.weekday() / 7), 4),
            round(__import__('math').sin(2 * __import__('math').pi * date.month / 12), 4),
            round(__import__('math').cos(2 * __import__('math').pi * date.month / 12), 4),
        ]
        return features

    # ============================================================
    #  DUOMENŲ KAUPIMAS
    # ============================================================

    def collect_hourly_data(self, kwargs):
        """Kaupia valandinius duomenis modelio treniravimui."""
        try:
            load_w   = float(self.get_state(SENSOR["house_load"]) or 0)
            load_kwh = load_w / 1000  # kW → kWh per valandą

            record = {
                "date":     datetime.now().date().isoformat(),
                "hour":     datetime.now().hour,
                "kwh":      round(load_kwh, 4),
                "temp":     self.get_outdoor_temp(),
                "soc":      self.get_soc(),
                "weekday":  datetime.now().weekday(),
                "month":    datetime.now().month,
                "holiday":  1 if self.is_holiday() else 0,
            }

            self.history.append(record)

            # Palikti tik 2 metų duomenis (17520 valandų)
            if len(self.history) > 17520:
                self.history = self.history[-17520:]

            # Išsaugoti kas 24 valandas
            if datetime.now().hour == 0:
                self.save_history()

        except Exception as e:
            self.log(f"Duomenų kaupimo klaida: {e}", level="DEBUG")

    # ============================================================
    #  MODELIO TRENIRAVIMAS
    # ============================================================

    def retrain_model(self, kwargs):
        """
        Perrenkina ML modelį su naujausiais duomenimis.
        Veikia kas pirmadienį 03:00.
        """
        self.log("[ML] Modelio perrenkimas pradedamas...")

        # Agregavimas į dieninius duomenis
        daily_data = self.aggregate_daily_data()

        if len(daily_data) < MIN_TRAINING_DAYS:
            self.log(
                f"[ML] Per mažai duomenų: {len(daily_data)} dienų "
                f"(reikia {MIN_TRAINING_DAYS}). Treniravimas atidedamas.",
                level="WARNING"
            )
            return

        # Paruošti X, y
        X, y = [], []
        for day in daily_data:
            try:
                date = datetime.strptime(day["date"], "%Y-%m-%d").date()
                features = self.build_features(
                    date=date,
                    hour=12,
                    outdoor_temp=day.get("avg_temp", 10.0),
                    soc=day.get("morning_soc", 50.0),
                )
                X.append(features)
                y.append(day["total_kwh"])
            except Exception:
                continue

        if len(X) < MIN_TRAINING_DAYS:
            self.log("[ML] Nepakanka tinkamų duomenų treniravimui.")
            return

        X = np.array(X)
        y = np.array(y)

        self.log(f"[ML] Treniravimas su {len(X)} dienų duomenimis...")

        # Skalierius
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # Modelis — Random Forest
        rf_model = RandomForestRegressor(
            n_estimators=200,
            max_depth=8,
            min_samples_split=5,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1,
        )

        # Kryžminė validacija
        cv_scores = cross_val_score(rf_model, X_scaled, y, cv=5, scoring="r2")
        r2_mean   = cv_scores.mean()
        r2_std    = cv_scores.std()

        self.log(f"[ML] Kryžminė validacija R²: {r2_mean:.3f} ± {r2_std:.3f}")

        # Treniruoti su visais duomenimis
        rf_model.fit(X_scaled, y)

        # Bandyti Gradient Boosting palyginimui
        gb_model = GradientBoostingRegressor(
            n_estimators=150,
            learning_rate=0.05,
            max_depth=4,
            random_state=42,
        )
        gb_scores = cross_val_score(gb_model, X_scaled, y, cv=5, scoring="r2")

        # Pasirinkti geresnį modelį
        if gb_scores.mean() > r2_mean + 0.02:
            gb_model.fit(X_scaled, y)
            self.model = gb_model
            model_name = "GradientBoosting"
            r2_final   = gb_scores.mean()
        else:
            self.model = rf_model
            model_name = "RandomForest"
            r2_final   = r2_mean

        self.log(f"[ML] Pasirinktas modelis: {model_name}, R²={r2_final:.3f}")

        # Išsaugoti
        self.save_model()

        # Feature importance
        if hasattr(self.model, "feature_importances_"):
            feature_names = [
                "weekday", "month", "hour", "temp",
                "holiday", "soc", "season",
                "sin_weekday", "cos_weekday", "sin_month", "cos_month"
            ]
            importances = self.model.feature_importances_
            top = sorted(zip(feature_names, importances), key=lambda x: -x[1])[:5]
            self.log(f"[ML] Svarbiausi features: {top}")

        # Pranešimas
        try:
            self.call_service(
                "notify/telegram",
                title="🤖 ML modelis atnaujintas",
                message=(
                    f"Modelis: {model_name}\n"
                    f"Tikslumas R²: {r2_final:.3f}\n"
                    f"Treniravimo duomenys: {len(X)} dienų"
                )
            )
        except Exception:
            pass

    def aggregate_daily_data(self):
        """Agreguoja valandinius duomenis į dieninius."""
        from collections import defaultdict

        daily = defaultdict(list)
        for rec in self.history:
            daily[rec["date"]].append(rec)

        result = []
        for date_str, hours in daily.items():
            if len(hours) < 20:  # nepilna diena
                continue
            result.append({
                "date":        date_str,
                "total_kwh":   round(sum(h["kwh"] for h in hours), 3),
                "avg_temp":    round(sum(h["temp"] for h in hours) / len(hours), 1),
                "morning_soc": next((h["soc"] for h in hours if h["hour"] == 7), 50.0),
            })

        return sorted(result, key=lambda x: x["date"])

    # ============================================================
    #  PROGNOZAVIMAS
    # ============================================================

    def predict(self, date=None, outdoor_temp=None):
        """
        Prognozuoja dienos suvartojimą kWh.
        Grąžina None jei modelis nepakankamai treniruotas.
        """
        if self.model is None or self.scaler is None:
            return None

        if date is None:
            date = datetime.now().date()

        features = self.build_features(date=date, outdoor_temp=outdoor_temp)
        X = np.array([features])
        X_scaled = self.scaler.transform(X)

        prediction = self.model.predict(X_scaled)[0]
        return round(max(0, prediction), 2)

    def predict_tomorrow(self):
        """Prognozuoja rytojaus suvartojimą."""
        tomorrow = datetime.now().date() + timedelta(days=1)
        return self.predict(date=tomorrow)

    def predict_remaining_today(self):
        """Prognozuoja likusį suvartojimą šiandien."""
        if self.model is None:
            return None

        import math
        now          = datetime.now()
        current_hour = now.hour
        today        = now.date()
        daily_pred   = self.predict(date=today)

        if daily_pred is None:
            return None

        # Proporcija pagal likusias valandas
        profile_sum_total    = sum(1 + 0.3 * math.sin(math.pi * h / 12) for h in range(24))
        profile_sum_remaining = sum(1 + 0.3 * math.sin(math.pi * h / 12) for h in range(current_hour, 24))

        remaining = daily_pred * (profile_sum_remaining / profile_sum_total)
        return round(remaining, 2)

    # ============================================================
    #  HA SENSORIAI
    # ============================================================

    def update_ha_prediction(self, kwargs):
        """Eksponuoja ML prognozę į HA sensorius."""
        if self.model is None:
            return

        tomorrow  = self.predict_tomorrow()
        remaining = self.predict_remaining_today()

        if tomorrow is not None:
            self.set_state(
                "sensor.ml_consumption_tomorrow",
                state=tomorrow,
                attributes={
                    "unit_of_measurement": "kWh",
                    "friendly_name": "ML rytojaus suvartojimo prognozė",
                    "icon": "mdi:brain",
                }
            )

        if remaining is not None:
            self.set_state(
                "sensor.ml_consumption_remaining",
                state=remaining,
                attributes={
                    "unit_of_measurement": "kWh",
                    "friendly_name": "ML likusio suvartojimo prognozė",
                    "icon": "mdi:brain",
                }
            )

    # ============================================================
    #  TIKSLUMO STEBĖJIMAS
    # ============================================================

    def evaluate_accuracy(self, kwargs):
        """
        Kiekvieną dieną lygina vakarykštę prognozę su realybe.
        Kaupia tikslumo statistiką.
        """
        if self.model is None:
            return

        yesterday  = (datetime.now() - timedelta(days=1)).date()
        daily_data = self.aggregate_daily_data()

        yesterday_actual = next(
            (d["total_kwh"] for d in daily_data if d["date"] == yesterday.isoformat()),
            None
        )

        if yesterday_actual is None:
            return

        yesterday_pred = self.predict(date=yesterday)
        if yesterday_pred is None:
            return

        error      = abs(yesterday_pred - yesterday_actual)
        error_pct  = (error / yesterday_actual) * 100 if yesterday_actual > 0 else 0

        self.log(
            f"[ML] Vakarykštis tikslumas: prognozė={yesterday_pred:.2f} kWh, "
            f"realybė={yesterday_actual:.2f} kWh, "
            f"paklaida={error:.2f} kWh ({error_pct:.1f}%)"
        )

        # Atnaujinti tikslumo sensorių
        self.set_state(
            "sensor.ml_accuracy_yesterday",
            state=round(100 - error_pct, 1),
            attributes={
                "unit_of_measurement": "%",
                "friendly_name": "ML prognozės tikslumas (vakar)",
                "predicted": yesterday_pred,
                "actual": yesterday_actual,
            }
        )

    # ============================================================
    #  MODELIO IŠSAUGOJIMAS / ĮKĖLIMAS
    # ============================================================

    def save_model(self):
        try:
            with open(MODEL_FILE, "wb") as f:
                pickle.dump(self.model, f)
            with open(SCALER_FILE, "wb") as f:
                pickle.dump(self.scaler, f)
            self.log("[ML] Modelis išsaugotas.")
        except Exception as e:
            self.log(f"[ML] Išsaugojimo klaida: {e}", level="ERROR")

    def load_model(self):
        try:
            if os.path.exists(MODEL_FILE) and os.path.exists(SCALER_FILE):
                with open(MODEL_FILE, "rb") as f:
                    self.model = pickle.load(f)
                with open(SCALER_FILE, "rb") as f:
                    self.scaler = pickle.load(f)
                self.log("[ML] Modelis įkeltas sėkmingai.")
        except Exception as e:
            self.log(f"[ML] Įkėlimo klaida: {e}", level="WARNING")

    def load_history(self):
        try:
            if os.path.exists(ML_HISTORY_FILE):
                with open(ML_HISTORY_FILE, "r") as f:
                    data = json.load(f)
                    self.log(f"[ML] Istorija įkelta: {len(data)} valandų įrašų")
                    return data
        except Exception as e:
            self.log(f"[ML] Istorijos įkėlimo klaida: {e}")
        return []

    def save_history(self):
        try:
            with open(ML_HISTORY_FILE, "w") as f:
                json.dump(self.history, f)
        except Exception as e:
            self.log(f"[ML] Istorijos išsaugojimo klaida: {e}")
