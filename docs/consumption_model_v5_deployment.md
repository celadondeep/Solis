# Hibridinio vartojimo modelio V5 diegimas

2026-09-18, įdiegta 13:09:58 Lietuvos laiku.

Bendra architektūra ir tikslumo ribos: [V5 modelis](consumption_model_v5.md).

## Įdiegta

11 failų: bendras vartojimo modelis, prognozės skaitytuvas, hibridinis modulis, nekintančių prognozių vertinimas, bendri parametrai, horizonto ir nakties vartotojai, dashboard šablonas ir du sugeneruoti vartojimo rodiniai. Cloud integracija ir jos apklausų / komandų ribos nekeistos.

Prieš įrašymą patikrintos pradinės failų SHA-256 reikšmės; konkurentinių pakeitimų nerasta. Prieš AppDaemon paleidimą patikrinta Python / JSON / dashboard sintaksė. Po įrašymo visų 11 gyvų failų SHA-256 sutapo su paruoštais šaltiniais.

Atsarginė kopija, įskaitant abiejų augančius modelių JSON:
`/homeassistant/.codex-backups/consumption-v5-20260918T100958Z`.

Trumpam sustabdytas ir paleistas tik AppDaemon. Home Assistant ir Solis integracija neperkrauti.

## Patvirtinta gyvoje HA

- 13:15:06 pakartotinėje patikroje abu modeliai atsinaujino pagal numatytą 300 s ciklą; Namų naujų galios matavimų seka išaugo iki 7 taškų / 300 s.
- Abu `consumption_profile` sensoriai publikuoja `model_version=5`, `forecast_status=ok` ir 26 pilnų parų profilį.
- Rytojus: Namai **17,17 kWh**, orientacinis intervalas **13,88–21,80 kWh**; Eimo **10,18 kWh**, intervalas **8,50–14,66 kWh**.
- Abiejų JSON failuose tikrai išsaugota pirmoji V5 prognozė 2026-09-19 su intervalu, palyginimo baze ir `trained_through=2026-09-17`.
- Trumpalaikio kandidato validavimas pradeda rinkti duomenis; jo įjungimo kriterijus dar nepasiektas. Valandinės prognozės fiksuojamos paskutines penkias minutes prieš valandą. Istorinės prognozės neįrašytos kaip tariamai gyvai sukaupti rezultatai.
- Namų horizontas jau skaito `consumption_model=hybrid_v5`; planuotojas ir vykdytojas `ok`, branduolys `normal`.
- **Eimo telemetrija jau prieš diegimą buvo pasenusi.** Vartojimo modelis veikia iš patikimos istorijos, trumpalaikė dalis sustabdyta. Valdymas išlaiko `telemetry_hold`, vykdytojas `paused`. 13:14 patikroje Cloud būsena `backoff`, paskutinė sėkmė 12:05:48, penkios nuoseklios telemetrijos klaidos; suplanuotas bandymas 13:26:53. Per paskutines 5 min. **0 API užklausų ir 0 įrašymo komandų**, per valandą 0 įrašymų. Šis diegimas Cloud ryšio sutrikimo neišsprendžia ir jo būsenos neslepia.
- Abu HA vartojimo rodiniai sėkmingai perskaityti su priverstiniu konfigūracijos atnaujinimu. Dashboard JavaScript patikrintas šešiomis modelio būsenomis; autentifikuota naršyklės ekrano nuotrauka nebuvo gauta.
- HA konfigūracija `valid`; po paleidimo AppDaemon žurnaluose `ERROR` įrašų nerasta. Esami tinklo kokybės įspėjimai ir Eimo ryšio apsauga lieka matomi.

## Kodas ir automatinės patikros

- Solis: [`bf9af92`](https://github.com/celadondeep/Solis/commit/bf9af92d6ebe6d7f9ccc857dac18822cec7fbc1d), šaka `codex/eimo-unified-20260912`; [CI sėkmingas](https://github.com/celadondeep/Solis/actions/runs/35333455485).
- HA konfigūracija: [`d1f1c63`](https://github.com/celadondeep/ha-config/commit/d1f1c636cc0882bd071a42b816a3cd0f7c910ddb), `main`; [CI sėkmingas](https://github.com/celadondeep/ha-config/actions/runs/35333466454).
- Vietoje praėjo 101 testas ir Python kompiliavimo patikra. Privati namų istorija ir galios matavimai į GitHub nekelti.

## Grąžinimas

Sustabdyti AppDaemon, pagal backup `manifest.json` atkurti ankstesnius failus ir pašalinti tik naujai pridėtą `consumption_hybrid.py`, patikrinti sintaksę, paleisti AppDaemon. Modelių JSON kopijos išsaugotos, bet jų atkūrimas ištrintų po diegimo sukauptas prognozes; įprastam kodo grąžinimui jų perrašyti nereikia, V4 ignoruoja naujus papildomus laukus.
