# SOC buferis — 2026-09-10

Gyvai pritaikyta bendra 4.2-soc-buffer versija. Ši šaka išsaugo dabar HA veikiančią bendrų modulių architektūrą ir tikslines YAML pataisas; main nebuvo perrašyta.

- Normalaus planavimo zona: konfigūruotas SOCmin + 15 p. p. iki SOCmax − 15 p. p. Namai 27–85 %, Eimo 20–85 % pagal patikros metu galiojusias ribas. Tai minkštas planavimo tikslas; nakties vietos saulei planas gali rinktis žemesnį SOC, laikydamasis vietinio saugaus dugno.
- Dienos Feed-in Priority nebeblokuoja prognozės leidžiamo TOU iškrovimo. Virš viršutinės pageidaujamos ribos +2 p. p. trumpas buferis gali palaikyti eksportą per PV/vartojimo svyravimus, su <=0,75 kWh žingsniu ir vietine SOC sustojimo riba.
- Namų modelis naudoja šviežią BMS srovę × baterijos įtampą dabartinio intervalo krovimo priėmimui; Eimo specifinė srovės kreivė nebuvo išgalvota. 15 p. p. vieta taip pat mažina darbą prie viršutinio SOC.
- Audros/aktyvaus ESO rezervo režimu įprastas buferis neveikia. Eksportas atveriamas tik nuo 95 %, turint šviežius SOC ir trijų fazių tinklo matavimus. Eksporto galios ribos nepakeistos, iškrovimo slotas išjungtas.
- Atominiuose Executor iškrovimas įjungiamas tik gavus tikslų SOC cut-off readback; prieš ribos pakeitimą aktyvus nesutampantis slotas išjungiamas.
- Bendrame skydelio šablone SOC baterijos ikona, judėjimas tik šviežiems matavimams, sukeistos Šiandien tinklo ikonėlės, trijų PCC grafikų linijos 1 px.

Patikrinta: 11 naujos politikos ir 6 šablono Python testai, 24 JS krovimo/iškrovimo/SOC/pasenimo scenarijai, Jinja per HA, konfigūracija valid, rašytų failų SHA256 sutampa. Po pritaikymo ir galutinės patikros abu Planner/Executor ok, abiejų Shadow match. Visame senajame testų rinkinyje išliko 3 jau prieš pakeitimą buvusios problemos (hard_floor=12 lūkestis, MRO lūkestis be HorizonMixin, absoliutus /homeassistant kelias). Jokių naujų AppDaemon programinių išimčių. Tikro skydelio screenshot šiame HA išjungtas; vaizdo logika tikrinta vykdant JS.

Gyva atsarginė kopija: /homeassistant/.codex-rollback/soc_buffer_20260910; manifest.json saugo prieš/po SHA256. Atšaukimui grąžinti tik manifest nurodytus failus, naują soc_buffer.py pašalinti, iš naujo įkelti scripts/automations ir AppDaemon; storage skydeliams naudoti prieš-pakeitimo config hash saugotą HA API.
