# Alternatyvūs vartojimo prognozavimo modeliai

2026-09-18 papildomai sukurti ir palyginti šeši modelių prototipai su įdiegtu V4. Namams nė vienas šių prototipų nepagerino MAE nei ankstesnėje, nei paskutinėje laikotarpio dalyje. Eimo kai kurie variantai buvo geresni vienoje dalyje ir prastesni kitoje; stabilaus pranašumo valdymo pakeitimui nepakanka.

Tai konkrečių nedidelių prototipų tyrimas, o ne išvada, kad visos regresijos ar mašininio mokymosi metodų šeimos netinka vartojimui prognozuoti. Parametrai nustatyti prieš šio palyginimo vykdymą ir nebuvo ieškoma geriausiai šiam laikotarpiui tinkančios kombinacijos.

## Rezultatai

Vidutinė absoliuti rytojaus paros prognozės klaida, kWh/parą; mažiau yra geriau.

| Modelis | Namai: paskutinė dalis | Eimo: paskutinė dalis | Namai: visas palyginimas | Eimo: visas palyginimas |
|---|---:|---:|---:|---:|
| Įdiegtas V4 | **2,167** | 1,613 | **2,585** | 2,800 |
| Eksponentinis lygio glodinimas | 2,257 | 1,948 | 2,745 | **2,618** |
| Holt slopinama tendencija | 2,299 | 1,968 | 2,783 | 2,680 |
| Adaptyvus lygis ir savaitės dienos | 2,474 | 1,928 | 2,817 | 2,683 |
| Regresija su ankstesnio vartojimo požymiais | 2,502 | **1,587** | 3,176 | 3,633 |
| Nedidelis sprendimų medžių ansamblis | 2,596 | 1,906 | 2,866 | 3,196 |
| Trijų skirtingų prognozių vidurkis | 2,309 | 1,703 | 2,763 | 2,878 |

Paskutinė dalis: rugsėjo 4–17 d., 14 tinkamų parų Namams ir 13 Eimo. Visas porinis palyginimas: 35 paros Namams, 34 Eimo. Kai prototipui dar neužteko mokymo pavyzdžių, atitinkama tikslinė para pašalinta iš visų kandidatų rezultatų, todėl Eimo bendras parų skaičius skiriasi nuo pirminio V4 tyrimo.

Eimo regresijos paskutinės dalies pagerėjimas tik 0,026 kWh/parą. Ankstesnėje dalyje ši regresija klydo 4,900 kWh/parą, o dabartinis modelis — 3,534. Tai nėra pakankamas pagrindas patikėti naujam prototipui baterijos valdymą. Kita vertus, eksponentinis glodinimas sumažino Eimo klaidą visame palyginime, bet paskutinėje dalyje ją padidino maždaug 21 %. Toks nevienodas elgesys leidžia kandidatus tirti toliau, tačiau nevadinti jų jau patikimai geresniais.

## Vertinimo ribos

- Naudotas tas pats jau V4 tyrime matytas istorinis laikotarpis. Tai papildomas tiriamasis palyginimas, ne nauja nepriklausoma patikros imtis.
- Kiekviena rytojaus prognozė mokoma tik iš duomenų iki išdavimo dienos pradžios. Prognozuojant D, paskutinė pilna žinoma para yra D−2. Regresijos mokymo pavyzdžių požymiai taip pat atkuriami pagal jų tuometinį prieinamumą.
- Visiems kandidatams taikytos vienodos iš ankstesnės analizės žinomos matavimo kokybės išimtys. Nė viena tikslinė para neatmesta vien dėl didelės prognozės klaidos.
- Dabartinio modelio 30 parų mokymo langas nekeičiamas. Nauji glodinimo bei regresijos prototipai gali naudoti iki 60 praeities parų. Regresijos požymiai normalizuojami pagal ankstesnių 30 parų vartojimą.
- Eimo faktinės reikšmės tebėra HA galios integralo istorija. Palyginimas neįrodo absoliutaus namo energijos skaitiklio tikslumo.
- Orų ir stambių atskirai matuojamų vartotojų požymiai šiame bandyme nenaudoti. Neprognozuota pagal tikslinės dienos faktiškai įvykusius orus, nes tai suteiktų modeliui prognozavimo metu nežinomą informaciją.

## Rekomenduojama tolesnė kryptis

Perspektyviausias kitas kandidatas šiai sistemai — bendras hibridinis valandinis modelis su kiekvienai elektrinei priskiriamais duomenų šaltiniais:

1. Atskirai prognozuoti namo foninį vartojimą pagal jo istoriją.
2. Pridėti žinomą valdomų stambių vartotojų poreikį, pavyzdžiui, boilerio planą, kai turima pakankama jo veikimo ir energijos apskaita. Šių vartotojų energija pirmiausia turi būti atskirta nuo istorinio fono, kitaip ji būtų suskaičiuota du kartus.
3. Temperatūrą ar kitus išorinius požymius įtraukti tik parodžius jų ryšį su vartojimu. Tikrinimui naudoti prognozės išdavimo metu prieinamą orų prognozę, ne vėliau sužinotą tikrą temperatūrą.
4. Pateikti prognozės neapibrėžtumą ir vertinti jo padengimą pagal būsimas iš anksto užfiksuotas prognozes. Baterijos planuotojui naudingi tiek mažesnio vartojimo ir talpos trūkumo, tiek didesnio vartojimo ir energijos rezervo scenarijai.

Ši hibridinė kryptis yra projektavimo rekomendacija, ne jau įdiegtas ar įrodytai tikslesnis modelis. Šiuo tyrimu gyvas valdymas nepakeistas. Įdiegtas V4 ir jo tikslumo žurnalas veikia toliau; naujos alternatyvos pirmiausia turi būti vertinamos su būsimomis prognozėmis.

## Atkuriamumas

Bandymo įrankis: [tools/consumption_replay](../tools/consumption_replay/README.md). Paleidus perkeltą įrankį, visi prognozių ir metrikų rezultatai tiksliai sutapo su pirmuoju bandymu. Žalių namų vartojimo istorijų saugykloje nėra; įrankis priima atskirai pateikiamus privačius duomenis ir nesijungia prie HA.

[Visos MAE, RMSE, WAPE bei sisteminio poslinkio metrikos](consumption_alternative_metrics_2026-09-18.json).

Metodų šaltiniai: [slenkanti laiko patikra](https://otexts.com/fpp3/tscv.html), [Holt slopinama tendencija](https://otexts.com/fpp3/holt.html), [ankstesnių reikšmių požymiai ir laiko eilučių patikra](https://scikit-learn.org/stable/auto_examples/applications/plot_time_series_lagged_features.html), [prognozių derinimas](https://otexts.com/fpp3/combinations.html).
