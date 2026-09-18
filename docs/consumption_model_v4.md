# Vartojimo modelio patikra — 2026-09-18

Abiem elektrinėms naudojamas vienas bendras modelis ir tie patys planuotojo moduliai. Skiriasi kiekvienos elektrinės istorija ir du profilio nustatymai. 30 užbaigtų vietinių parų langas išlieka; trūkstamos paros neprilyginamos nuliui ir langas dėl jų nepratęsiamas.

| Nustatymas | Namai | Eimo |
|---|---:|---:|
| Slenkantis langas | 30 parų | 30 parų |
| Savaitės dienos korekcijos stiprumas | 0 | 1, su ankstesniu mažos imties slopinimu |
| Korekcija pagal jau suvartotą paros energiją | iki 0,25 | 0 |
| Šiandienos nuokrypio perkėlimas į rytojų | 0 | 0 |
| Valandų profilis | bendras 30 parų | bendras 30 parų |

## Palyginimas su istorija

Surinktas iki 90 parų HA valandinių statistikų laikotarpis. Pakankamai pilnų parų rasta 73 Namams ir 60 Eimo. Paros prognozės lygintos nuo rugpjūčio 8 d.; valandų profilis ir likusios dienos prognozė — nuo rugpjūčio 18 d. Tikslinė para turi turėti visas fizines valandas. Rytojaus prognozei naudota tik istorija, kuri baigėsi iki prognozės išdavimo dienos pradžios: prognozuojant rugsėjo 19 d. rugsėjo 18 d., paskutinė mokymo para yra rugsėjo 17 d.

Tai retrospektyvus atkūrimas iš dabar prieinamos istorijos, ne anksčiau realiai išsiųstų prognozių žurnalas. Statistikos galėjo būti patikslintos. Visiems kandidatams taikytos vienodos duomenų kokybės išimtys; tikslinės paros nešalintos vien todėl, kad jų prognozės klaida didelė. Mokymo imties išskirtys nustatomos tik iš iki to momento turėtos praeities. Paskutinės 14 kalendorinių parų (rugsėjo 4–17 d.) atskirtos rezultatui patikrinti; tai trumpas patikros laikotarpis, todėl pagerėjimas ateityje negarantuotas.

| Rytojaus prognozė, rugsėjo 4–17 d. | Tinkamų parų | Vidutinė absoliuti klaida, kWh/parą | WAPE |
|---|---:|---:|---:|
| Namai: ankstesnė savaitės dienos korekcija | 14 | 2,392 | 14,29 % |
| Namai: 30 parų vidurkis be korekcijos | 14 | 2,167 | 12,95 % |
| Eimo: ankstesnė savaitės dienos korekcija | 13 | 1,613 | 18,06 % |
| Eimo: 30 parų vidurkis be korekcijos | 13 | 1,692 | 18,94 % |

Namams paprastesnis variantas buvo geresnis ir ankstesnėje palyginimo dalyje: 2,864 vietoje 3,059 kWh/parą. Eimo rezultatai nevienodi: be savaitės dienos korekcijos ankstesnė dalis geresnė, paskutinė — prastesnė. Todėl Eimo paros metodas nekeičiamas. Trumpesnis 14 parų langas paskutiniame laikotarpyje abiem buvo prastesnis. Išbandyti ir 21/42 parų langai, eksponentiniai svoriai, mediana bei praėjusios savaitės reikšmė; nėra pagrindo visoms elektrinėms pakeisti 30 parų langą vienu sudėtingesniu metodu.

Valandų profilių skirstymas į savaitės dienas ar darbo/savaitgalio dienas aiškaus pagerėjimo neparodė. Pagal esamą valandų profilį ir paros metodą patikros laikotarpio valandinė MAE buvo apie 0,34 kWh Namams ir 0,29 kWh Eimo. Likusios dienos patikroje 08/12/16/20 val. stipri 0,75 korekcija Eimo klaidą didino: 1,65 vietoje statinės 1,47 kWh. Namams silpnesnė 0,25 korekcija buvo stabilesnė per visą vertintą laikotarpį. To paties namo ir tos pačios dienos keturi vertinimai nėra keturios nepriklausomos paros.

MAE = vidutinis absoliutus skirtumas; WAPE = absoliučių klaidų suma / faktinės energijos suma. Šie rodikliai vertina atitiktį HA matavimams, o ne įrodo fizinio skaitiklio tikslumą.

## Rasti ir pataisyti trūkumai

1. `hourly_kwh` yra faktinis valandos vidurkis grafikams, bet planuotojas jį naudojo kaip šiandienos prognozę ir prarasdavo paros korekciją. V4 atskirai skelbia pagal datą susietus `daily_kwh` ir 24 `hourly_kw`; abu planavimo vartotojai naudoja bendrą datos tikrinimo funkciją.
2. Nakties įvertis per vidurnaktį nekaitaliojo šiandienos ir rytojaus paros sumų; atsarginiame kelyje inverterio savivarta galėjo būti pridėta du kartus. Dabar ji integruojama vieną kartą. Laikas integruojamas UTC, todėl 23/25 valandų paros išlaiko paros energiją.
3. Eimo einamosios paros įvertis priklausė nuo vienkartinio istorijos nuskaitymo ties vidurnakčiu. Dabar naudojamas jau esantis atskiras paros skaitiklis; nesėkmingas vidurnakčio nuskaitymas nebeužblokuoja visos paros. Pasenusi ar ankstesnės dienos reikšmė nekoreguoja prognozės.
4. Nulinė autoritetingos valandinės istorijos para galėjo palikti seną teigiamą atsarginį paros įrašą. Dabar jis pašalinamas iš mokymo; kokybės problema rodoma atskirai. Po nulinės paros vienai valandai priskiriamas didelis energijos kiekis taip pat pažymimas kaip abejotinas.
5. Pakartotinis sensoriaus paskelbimas nebeatjaunina mokymo istorijos. Atskirai tikrinamos paskutinės faktinės paros ir valandų profilio datos; iki 7 parų galima naudoti aiškiai pažymėtą išsaugotą profilį, senesnis įvertis planuotojui netinka.
6. Likusios dienos prognozės sensorius ir planuotojas naudoja tą pačią korekcijos formulę bei elektrinės nustatymą. Rytojaus prognozei šiandienos nuokrypis nebeperkeliamas. Momentinės apkrovos pataisa planuotojuje išlieka trumpalaikė ir leidžiama tik esant šviežiam ryšio patvirtinimui.
7. ESO CSV iš tinklo paimtai energijai pagal nutylėjimą nebenaudojamas kaip viso namo vartojimo mokymo šaltinis. Orų papildymas, kurio prognozė nenaudoja, pagal nutylėjimą išjungtas.

## Matavimų ribos

Eimo `eimo_house_energy` gaunamas integruojant `familyLoadPower`, o ne iš patikrinto nepriklausomo namo energijos skaitiklio. Retesni ar pasenę Cloud galios matavimai gali iškreipti tiek paros sumą, tiek jos paskirstymą valandoms. Rugpjūčio 23 d. valandinė suma buvo 0; rugpjūčio 24 d. — 111,19 kWh, iš kurių 109,43 kWh priskirta vienai valandai. Tikslaus fizinio vartojimo iš tokio epizodo atkurti negalima.

Patikrinti ir jau esantys tiesioginiai Solis alternatyvūs skaitikliai. Per paskutines 30 parų paros skaitiklio statistikoje buvo 54 valandos su neigiamu pokyčiu; bendrame skaitiklyje matyti 1 kWh žingsniai ir apie 1974 kWh šuolis rugpjūčio 19 d. Jų reikšmės su integralu nesutampa. Todėl šio pakeitimo metu šaltiniai automatiškai nesukeisti ir skaitikliai nekalibruoti pagal nepatvirtintą etaloną. Patikimesniam absoliučiam tikslumui reikia sulyginti su patikrintu viso namo energijos apskaitos šaltiniu, įskaitant apskaitos ribas ir inverterio nuostolius.

## Gyvas tikslumo stebėjimas

Vartojimo modelio JSON faile laikomas `forecast_ledger`: kartą prognozuojamai rytojaus datai išsaugomas išdavimo laikas, metodas, prognozė, ankstesnio metodo palyginimo prognozė ir paskutinė mokymo data. Persimokymas ar perkrovimas išduotos prognozės neperrašo. Vidurnakčio publikacija atnaujina datas, o prognozės fiksavimas laukia einamosios dienos vietinės istorijos atnaujinimo, įprastai 00:20.

Kai para pasibaigia ir gaunama pilna jos valandinė istorija, skaičiuojami 30 kalendorinių parų MAE, RMSE, WAPE ir poslinkis. Tikslinės didelio vartojimo paros automatiškai neišmetamos kaip prognozės išskirtys. Neužpildyta ar aiškiai abejotina faktinė istorija atskirai išvardijama, nesuteikiant jai nulinės klaidos. Iki 14 patikrintų parų rezultatas laikomas preliminariu; kol nėra nė vienos, rodomas rezultatų kaupimas.

Abiejų elektrinių „Vartojimo“ kortelė rodo vidutinę klaidą, patikrintų prognozių skaičių ir duomenų kokybės išimtis. Vietinės HA istorijos užklausa tebėra ribota ir nesikreipia į SolisCloud. Publikacija kas 5 minutes bei vidurnakčio datų pakeitimas papildomų inverterio užklausų nesiunčia.

## Patikra ir atkūrimas

Pakeisti bendri AppDaemon moduliai, elektrinių nustatymai ir abi sugeneruotos „Vartojimo“ kortelės. Inverterio komunikacijos modulis ir komandų eilė nekeisti. Prieš diegimą išsaugoti keičiami failai bei abu išmokti modeliai; HA Core neperkrautas. Gyvai patikrinti datuoti profiliai, išlikęs prognozių žurnalas, planuotojai ir vykdytojai. Galutiniai commitai ir CI rezultatai pateikiami HA konfigūracijos diegimo įraše.

Atsarginė kopija: `/homeassistant/.codex-backups/consumption-v4-20260918T043000Z`. Atkūrimui sustabdyti AppDaemon, iš `files/` atkurti tik manifeste išvardytus failus, pašalinti du naujus modulius, jei jų prieš diegimą nebuvo, ir prireikus atkurti abu modelio JSON. Po to paleisti AppDaemon bei patikrinti būsenas. Senesnės kopijos atkūrimas prarastų po jos sukūrimo sukauptus prognozių žurnalo įrašus.

Metodikos šaltiniai: [prognozių tikrinimas slenkančia laiko pradžia](https://otexts.com/fpp3/tscv.html), [HA vietinės statistikos veiksmas](https://www.home-assistant.io/actions/recorder.get_statistics/), [HA energijos integravimas iš galios](https://www.home-assistant.io/integrations/integration/).
