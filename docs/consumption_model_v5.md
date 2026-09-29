# Bendras hibridinis vartojimo modelis V5

2026-09-18. Vienas `energy_system/consumption_hybrid.py` abiem elektrinėms ir būsimiems Modbus / Cloud profiliams. Diegimo patikra pateikiama atskirame `consumption_model_v5_deployment.md`.

## Kas veikia

- Išlieka 30 užbaigtų vietinių parų statistinis pagrindas. Namai naudoja paprastą paros vidurkį, Eimo – vidurkį su prislopinta savaitės dienos įtaka. Tai skiriasi tik profilio parametrais.
- Patikimas šiandienos energijos skaitiklis koreguoja likusią dieną: Namai 0,25 stiprumas, Eimo 0 dėl anksčiau aptiktų Cloud galios integravimo artefaktų. Korekcija ribojama 0,7–1,6 ir nepratęsiama į rytojų.
- Kandidatinė trumpalaikė dalis naudoja paskutinių 20 min. galios nuokrypio medianą. Reikia bent 3 skirtingų šaltinio matavimų ir bent 10 min. aprėpties. Vienas virdulio šuolis nekeičia visos nakties prognozės. Stiprumas 0,35, didžiausias pokytis ±1,5 kW, įtaka per valandą išnyksta.
- Kandidatas pirmiausia vertinamas nekeisdamas valdymo. Jis automatiškai pradedamas taikyti tik sukaupus bent 48 tinkamas valandines prognozes per bent 7 skirtingas užbaigtas paras ir pasiekus bent 5 % mažesnę MAE už tų pačių valandų bazę. Jau įjungtas kandidatas atšaukiamas, jei tampa daugiau kaip 2 % prastesnis. Trūkstant įrodymų naudojama bazė. Šios ribos nėra statistinio reikšmingumo garantija.
- Atskiro vartotojo patikimas planas gali pakeisti jo istorinę viso namo vartojimo dalį. Abiejuose gyvuose profiliuose atskirų skaitiklių ir planų nėra, todėl `APPLIANCES=[]`. Rodomų boilerio kortelių jungikliai nelaikomi fiziniais matavimais.
- Rytojui rodomas orientacinis intervalas pagal 30 dienų prieinamų, pilnų ir patikimų parų priežastinį prognozių pakartojimą. 10 ir 90 procentiliai nėra pažadas, kad tikroji aprėptis jau lygi 80 %. Intervalas diagnostinis; jis nekeičia baterijos saugos ribų.

## Duomenų patikimumas ir veikimas sutrikus ryšiui

Skaitoma vietinė HA būsena kas 60 s. Prognozė publikuojama kas 300 s. Tai nesukuria papildomų SolisCloud užklausų ar inverterio komandų. Esamas vienas paros `recorder/get_statistics` kvietimas ir ribotas pakartojimas lieka; į tą patį kvietimą prireikus įtraukiami komponentų skaitikliai.

Galios matavimą patvirtina jo paties naujumas ir inverterio matavimo laikas. Palaikomi ISO laikas, Unix sekundės bei milisekundės. Pasikartojantis šaltinio laikas nesukuria naujo stebėjimo. Namų naujumo riba 180 s; Eimo – 600 s. Netinkami vienetai, būsimi laikai, nebaigtiniai dydžiai, dideli tarpai ir išjungtas / nepasiekiamas inverteris trumpalaikę dalį sustabdo. Neveikiantis inverteris negali išmokyti tariamo nulinio namų vartojimo.

Dingęs šaltinis tikrinamas vietoje kas minutę, o pirmas pablogėjimas iš karto perpublikuoja prognozę. Publikacijos trumpalaikės dalies galiojimas 360 s: nebeatnaujinant modelio ji pati nustoja galioti. Istorinio profilio mokymo datos vis tiek privalo atitikti esamą 7 dienų ribą; nauja publikacija neatjaunina senų mokymo duomenų.

Po AppDaemon paleidimo galios seka renkama iš naujo. Užfiksuotos prognozės ir vertinimas saugomi modelio JSON atominiu įrašymu. Cloud ryšio algoritmas, komandų eilė ir inverterio valdymo integracija šiame pakeitime nekeisti.

## Viena prognozė visiems vartotojams

`forecast_reader` V5 grąžina bendrą prognozę, jos laiko ribas ir galimybę tiksliai integruoti dalinę valandą. Planuotojas, likęs šiandienos vartojimas ir nakties skaičiavimas naudoja tą patį rezultatą. Ankstesnė atskira planuotojo dienos korekcija ir vieno galios rodmens 0,5 mišinys V5 atveju nebetaikomi antrą kartą.

24 valandų bazinės kreivės išlaiko savo paros energijos sutartį ir 23 / 25 valandų dienų palaikymą. Trumpalaikė dalis dalinama į aiškius 5 min. segmentus. Vartotojų planų pradžios ir pabaigos taip pat įtrauktos į integravimo ribas. Inverterio savivarta pridedama vieną kartą.

## Tikras būsimo tikslumo matavimas

- Pirmoji rytojaus prognozė fiksuojama nekeičiant ankstesnės: taikinys D mokosi tik iš duomenų iki D−2. Saugoma bazė, intervalas ir išleidimo laikas. Užbaigus parą skaičiuojami MAE, RMSE, WAPE, poslinkis ir faktinė intervalo aprėptis.
- Per paskutines 5 min. iki kitos fizinės valandos pradžios fiksuojama viena jos prognozė: kandidatas, identiška bazė be galios korekcijos ir realiai taikytas variantas. Viduryje jau prasidėjusios valandos prognozė nefiksuojama.
- Faktas imamas tik iš pilnos užbaigtos paros recorder duomenų. Dubliuota žiemos laiko valanda nevertinama atskirai, nes šis suvestinės formatas abi valandas sujungia. Duomenų kokybės klaidos nėra nulinės prognozavimo klaidos.
- Valandinis žurnalas saugomas 14 dienų. Įjungimo kriterijus naudoja tik tas valandas, kuriomis turėjome tinkamą, nenulinę kandidato korekciją. Persimokymas nekeičia jau užfiksuotų prognozių.

## Istorinis patikrinimas ir sprendimas

Dvi 2026-09-16–17 paros tikrintos su HA 5 min. galios vidurkiais, inverterio įjungimo istorija ir Eimo šaltinio laiko istorija. Šie vidurkiai nėra žali gyvos sekos matavimai; Namų šaltinio heartbeat istorija atskirai nebuvo patikrinta. Gretimos tikrinamos valandos persidengia. Rezultatai orientaciniai ir nėra nepriklausomas būsimo pagerėjimo įrodymas.

| Elektrinė | Tinkami 1 h langai | Bazės MAE, kWh | Kandidato MAE, kWh |
|---|---:|---:|---:|
| Namai | 305 | 0,3522 | 0,3795 |
| Eimo | 152 | 0,1764 | 0,1763 |

Todėl nauja galios korekcija neįjungta aklai: įdiegta automatinė būsimų prognozių patikra. Ilgesnio laikotarpio bazinis taškinis modelis nepakeistas prastesniu statistiniu ar ML metodu.

Istorinių intervalų nuoseklus tikrinimas, kiekvienam taikiniui kalibruojant tik pagal ankstesnes 30 kalendorinių dienų: Namai 69,23 % aprėptis per 52 taikinius, Eimo 84,21 % per 38. Rugsėjo 4–17 imtyje atitinkamai 78,57 % / 14 parų ir 100 % / 13 parų. Todėl vartotojui rodoma „orientacinis intervalas“, o ne garantuotas 80 % tikslumas. Naujas būsimas vertinimas pradeda kauptis nuo diegimo.

Principai: [laiko eilučių slenkantis tikrinimas](https://otexts.com/fpp3/tscv.html), [prognozės intervalai](https://otexts.com/fpp3/prediction-intervals.html). Gyvai nereikia naujų Python paketų.

## Pasirenkamų vartotojų planų sutartis

Į konkrečios elektrinės `consumption.APPLIANCES` galima įtraukti objektą:

```json
{"key":"water_heater","energy_sensor":"sensor.water_heater_energy","plan_entity":"sensor.water_heater_load_plan","max_kw":2.2}
```

Tai pavyzdys, šių objektų gyvai neįjungta. Energijos skaitiklis privalo būti atskiras ir turėti kWh recorder statistiką. Istorinė dalis apskaičiuojama iš bent 14 tų pačių pilnų parų, kaip viso namo modelis. Skaitikliai ir planų objektai negali kartotis, sudėtos dalys negali viršyti bendro vartojimo.

Plano objekto atributai: `generated_at`, `valid_until`, `covered_dates` (aiškiai šiandiena ir / arba rytojus), `slots` su `start`, `end`, `power_kw`. Visi laikai su zona. Ne daugiau 96 nesikertančių intervalų, galia iki nustatyto `max_kw`, ne senesnė kaip 1 h publikacija. Tuščias `slots` su aiškia datos aprėptimi reiškia suplanuotą išjungimą; trūkstamas ar pasenęs planas išlaiko istorinę dalį. Nepadengiamos datos ir persidengiantys intervalai atmetami.

Kad tas pats prietaisas nebūtų suskaičiuotas ir plane, ir bendroje galioje, veikiant atskiriems planams viso namo trumpalaikė / kumuliacinė korekcija sustabdoma. Prognozė tada yra likęs istorinis fonas ir tiksliai suplanuota komponento energija. Ateityje atskirai matuojamo fono korekciją galima pridėti turint patvirtintus momentinius komponentų matavimus.

## Patikros ir grąžinimas

101 tikslinis ir ankstesnių bendrų modulių testas: pasenęs Cloud matavimas esant naujai HA publikacijai, dubliai, Unix / ISO, vienas šuolis, tarpai, galiojimo pabaiga, komponentų persidengimas, dalinės valandos, nakties savivarta, DST, duomenų nutekėjimo į praeities prognozes prevencija, nekintantys žurnalai ir automatinis kandidato priėmimas / atmetimas. Dashboard JavaScript tikrinamas su gyvos būsenos formatu. Diegimas su pradinių SHA-256 patikra ir atsargine kodų bei modelių kopija; perkraunamas tik AppDaemon.
