"""Plant isolation, transport bindings and template reuse regressions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / 'work/appdaemon/apps').exists():
    sys.path.insert(0, str(ROOT / 'work/appdaemon/apps'))
else:
    sys.path.insert(0, str(ROOT))
from energy_system.site_registry import SITES, get_site, load_sites, resolve, merge, dashboard_profiles
from energy_system.dashboard_template import build_dashboard
from energy_system.config_validator import validate_profiles


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values(): yield from strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value: yield from strings(v)


class PlantArchitecture(unittest.TestCase):
    def test_existing_limits_preserved(self):
        self.assertEqual((SITES['home']['energy']['PLAN_HARD_FLOOR'], SITES['eimo']['energy']['PLAN_HARD_FLOOR']), (13,6))
        self.assertEqual((SITES['home']['energy']['HEADROOM_EXECUTION_MINUTES'], SITES['eimo']['energy']['HEADROOM_EXECUTION_MINUTES']), (5,30))
        self.assertEqual(SITES['eimo']['energy']['EXECUTOR_SETTLE_SECONDS'],420)

    def test_profiles_are_independent(self):
        a,b=get_site('eimo'),get_site('eimo')
        a['energy']['HORIZON']['comfort_soc']=99
        a['dashboard']['analysis']['quality']['state']='sensor.changed'
        self.assertNotEqual(a['energy']['HORIZON'],b['energy']['HORIZON'])
        self.assertNotEqual(a['dashboard']['analysis']['quality'],b['dashboard']['analysis']['quality'])

    def test_merge_keeps_defaults_without_aliases(self):
        base={'nested':{'a':1,'b':[]}}
        result=merge(base,{'nested':{'a':2}});result['nested']['b'].append(3)
        self.assertEqual(base,{'nested':{'a':1,'b':[]}})

    def test_reference_cycle_and_unknown_fail(self):
        for document in ({'a':{'$ref':'a'}},{'a':{'$ref':'missing'}},{'a':{'$ref':'a','unexpected':1}}):
            with self.assertRaises(ValueError):resolve(document)

    def test_same_label_does_not_select_another_plant(self):
        plant=dashboard_profiles()['eimo'];plant['site_label']='Namai'
        dashboard=build_dashboard(plant)
        self.assertIn(SITES['eimo']['energy']['ACTUATOR']['power'],set(strings(dashboard)))

    def test_common_pages_and_real_power_feedback(self):
        for key,plant in dashboard_profiles().items():
            d=build_dashboard(plant)
            self.assertEqual([v['path'] for v in d['views']],[plant.get('overview_path','energija'),'analize','atsipirkimas','vartojimas'])
            self.assertEqual(plant['power'],SITES[key]['energy']['ACTUATOR']['power'])
            self.assertEqual(plant['analysis']['today']['consumption'],SITES[key]['energy']['SENSOR']['consumption_today'])
            self.assertNotIn('planTime = ts(plan.committed_at)',d['button_card_templates']['se_panel']['custom_fields']['body'])

    def test_portfolio_optional(self):
        plant=dashboard_profiles()['eimo'];plant['finance']=None
        self.assertEqual([v['path'] for v in build_dashboard(plant)['views']],[plant.get('overview_path','energija'),'analize','vartojimas'])

    def test_navigation_supports_more_than_two_plants(self):
        plant=dashboard_profiles()['eimo'];plant['navigation']=[{'title':f'Plant {i}','url':f'plant-{i}'} for i in range(4)]
        self.assertEqual(len(build_dashboard(plant)['views'][0]['badges']),5)

    def test_dashboard_has_no_plant_names_in_logic(self):
        root=Path(sys.modules['energy_system.site_registry'].__file__).parent
        for name in ['dashboard_template.py','dashboard_design.py']:
            text=(root/name).read_text()
            for forbidden in ['1033300254190112','solis_s6_eh3p','sensor.eimo','EXTRA[','other_url']:
                self.assertNotIn(forbidden,text)

    def test_shared_forecast_allowed_but_shared_actuator_rejected(self):
        a,b=get_site('home')['energy'],get_site('eimo')['energy']
        self.assertFalse(validate_profiles([a,b]))
        b['ACTUATOR']['power']=a['ACTUATOR']['power']
        self.assertTrue(any('cross_site_entity' in issue for issue in validate_profiles([a,b])))

    def write_sites(self, directory, sites):
        for key,site in sites.items():
            (directory/(key+'.json')).write_text(json.dumps(site))

    def test_third_cloud_plant_needs_only_data(self):
        sites=copy.deepcopy(SITES)
        # A separate inverter, output namespace, learned state and dashboard.
        txt=json.dumps(sites['eimo']).replace('eimo','third').replace('Eimo','Trečia').replace('1033300254190112','1234567890000003').replace('01KT7EWKQVY3ANB6EDBEZNK39H','third_entry')
        sites['third']=json.loads(txt)
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);self.write_sites(directory,sites)
            loaded=load_sites(directory,defaults={})
        self.assertEqual(len(loaded),3)
        self.assertEqual(loaded['third']['execution']['adapter'],'solis_cloud')
        plant=copy.deepcopy(loaded['third']['dashboard']);plant['finance']=None;plant['navigation']=[]
        self.assertTrue(build_dashboard(plant)['views'])

    def test_duplicate_cloud_target_is_rejected(self):
        sites=copy.deepcopy(SITES)
        second=copy.deepcopy(sites['eimo']);second['energy']['KEY']='third'
        sites['third']=second
        with tempfile.TemporaryDirectory() as temp:
            self.write_sites(Path(temp),sites)
            with self.assertRaisesRegex(ValueError,'duplicate_cloud_target'):
                load_sites(temp,defaults={})

    def test_state_file_collision_is_rejected(self):
        sites=copy.deepcopy(SITES)
        sites['eimo']['consumption']['MODEL_FILE']=sites['home']['consumption']['MODEL_FILE']
        with tempfile.TemporaryDirectory() as temp:
            self.write_sites(Path(temp),sites)
            with self.assertRaisesRegex(ValueError,'shared_state_file'):
                load_sites(temp,defaults={})

    def test_executor_cannot_use_other_sites_plan(self):
        sites=copy.deepcopy(SITES)
        sites['eimo']['execution']['inputs']['plan_entity']=sites['home']['energy']['OUTPUT']['plan']
        with tempfile.TemporaryDirectory() as temp:
            self.write_sites(Path(temp),sites)
            with self.assertRaisesRegex(ValueError,'Executor differs'):
                load_sites(temp,defaults={})


if __name__=='__main__':unittest.main()
