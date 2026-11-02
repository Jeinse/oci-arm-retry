import copy
import json
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import Mock, patch

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import oci_retry_micro as m
from github_state import GitHubState

PUBLIC = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
    serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
TENANCY = 'ocid1.tenancy.oc1..test'
COMP = 'ocid1.compartment.oc1..test'
AD = 'test:AP-SINGAPORE-1-AD-1'


def settings(approved=True):
    return m.Settings(TENANCY, 'ocid1.user.oc1..test', '00:' * 15 + '00', 'fake',
                      TENANCY, 'ocid1.subnet.oc1.ap-singapore-1.test',
                      'ocid1.image.oc1.ap-singapore-1.test', PUBLIC, 'campaign', approved)


class MemoryStore:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data or m.initial_state())
        self.history = []
        self.fail = False
    def load(self):
        return copy.deepcopy(self.data)
    def save(self, state):
        if self.fail:
            raise RuntimeError('journal unavailable')
        self.data = copy.deepcopy(state)
        self.history.append(copy.deepcopy(state))


class Gateway:
    def __init__(self, capacity='AVAILABLE', existing=False, error=None):
        self.calls, self.inspections = 0, 0
        self.plan = {'existing_target': existing, 'capacity': capacity,
                     'instance_hash': 'hash', 'ad': AD, 'existing_storage_gb': 150}
        self.error = error
    def inspect(self):
        self.inspections += 1
        return copy.deepcopy(self.plan)
    def launch(self, plan, token):
        self.calls += 1
        if self.error:
            raise self.error
        return {'instance_hash': 'hash', 'state': 'PROVISIONING', 'request_id': 'test'}


def execute(operation, store, gateway, approved=True, now=1000, confirmation=m.CONFIRMATION):
    return m.run(operation, store, lambda config: gateway,
                 lambda: settings(approved), confirmation, lambda: now)


class EngineTests(unittest.TestCase):
    def test_schedule_is_unarmed_by_default(self):
        store, gateway = MemoryStore(), Gateway()
        r = execute('tick', store, gateway)
        self.assertEqual(r['status'], 'SKIPPED_PAUSED')
        self.assertEqual(gateway.inspections, 0)
        self.assertEqual(gateway.calls, 0)

    def test_plan_has_no_launch_or_journal_write(self):
        store, gateway = MemoryStore(), Gateway()
        r = execute('plan', store, gateway, approved=False)
        self.assertEqual(r['status'], 'READ_ONLY_PLAN')
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(store.history, [])

    def test_start_needs_exact_manual_confirmation(self):
        store, gateway = MemoryStore(), Gateway()
        with self.assertRaises(m.GuardError):
            execute('start', store, gateway, confirmation='yes')
        self.assertEqual(gateway.calls, 0)

    def test_free_configuration_requires_owner_approval(self):
        store, gateway = MemoryStore(), Gateway()
        self.assertEqual(execute('start', store, gateway, approved=False)['status'], 'STOPPED_FOR_REVIEW')
        self.assertEqual(gateway.inspections, 0)
        self.assertEqual(gateway.calls, 0)

    def test_one_launch_then_permanent_stop_even_after_out_of_band_deletion(self):
        store, gateway = MemoryStore(), Gateway()
        self.assertTrue(execute('start', store, gateway)['notify'])
        self.assertEqual(gateway.calls, 1)
        self.assertTrue(store.history[0]['launch_pending'])
        self.assertEqual(store.data['mode'], 'completed')
        execute('tick', store, gateway, now=999999)
        execute('start', store, gateway, now=999999)
        self.assertEqual(gateway.calls, 1)

    def test_capacity_snapshot_skips_launch(self):
        store, gateway = MemoryStore(), Gateway('OUT_OF_HOST_CAPACITY')
        execute('start', store, gateway)
        self.assertEqual(store.data['mode'], 'active')
        self.assertFalse(store.data['launch_pending'])
        self.assertEqual(gateway.calls, 0)

    def test_only_definite_capacity_error_continues(self):
        error = oci.exceptions.ServiceError(500, 'InternalError', {}, 'Out of host capacity.')
        store, gateway = MemoryStore(), Gateway(error=error)
        execute('start', store, gateway)
        self.assertEqual(store.data['mode'], 'active')
        self.assertFalse(store.data['launch_pending'])
        execute('tick', store, gateway, now=2800)
        self.assertEqual(gateway.calls, 2)  # Separate runs, separated by 1800s.

    def test_unrelated_capacity_word_does_not_retry(self):
        error = oci.exceptions.ServiceError(400, 'InvalidParameter', {}, 'capacity configuration is invalid')
        store, gateway = MemoryStore(), Gateway(error=error)
        execute('start', store, gateway)
        execute('tick', store, gateway, now=10000)
        self.assertEqual(store.data['mode'], 'uncertain')
        self.assertEqual(gateway.calls, 1)

    def test_429_backs_off_one_hour(self):
        error = oci.exceptions.ServiceError(429, 'TooManyRequests', {}, 'rate limited')
        store, gateway = MemoryStore(), Gateway(error=error)
        execute('start', store, gateway)
        self.assertEqual(store.data['next_allowed_at'], 4600)
        execute('tick', store, gateway, now=2800)
        self.assertEqual(gateway.calls, 1)

    def test_transport_timeout_never_replays(self):
        store, gateway = MemoryStore(), Gateway(error=TimeoutError('secret must not be logged'))
        r = execute('start', store, gateway)
        self.assertNotIn('secret', json.dumps(r))
        execute('tick', store, gateway, now=10000)
        self.assertEqual(store.data['mode'], 'uncertain')
        self.assertEqual(gateway.calls, 1)

    def test_cancelled_job_pending_journal_can_only_reconcile(self):
        state = m.initial_state()
        state.update(mode='active', launch_pending=True, expires_at=999999)
        store, gateway = MemoryStore(state), Gateway()
        execute('tick', store, gateway)
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(store.data['mode'], 'uncertain')

    def test_pending_journal_and_visible_target_completes_without_launch(self):
        state = m.initial_state()
        state.update(mode='active', launch_pending=True, expires_at=999999)
        store, gateway = MemoryStore(state), Gateway(existing=True)
        execute('tick', store, gateway)
        self.assertEqual(store.data['mode'], 'completed')
        self.assertEqual(gateway.calls, 0)

    def test_journal_failure_prevents_launch(self):
        store, gateway = MemoryStore(), Gateway()
        store.fail = True
        with self.assertRaises(RuntimeError):
            execute('start', store, gateway)
        self.assertEqual(gateway.calls, 0)

    def test_pause_and_resume_preserve_minimum_interval(self):
        store, gateway = MemoryStore(), Gateway('OUT_OF_HOST_CAPACITY')
        execute('start', store, gateway)
        execute('pause', store, gateway)
        r = execute('start', store, gateway, now=2000)
        self.assertEqual(r['status'], 'BACKOFF_OR_MINIMUM_30_MIN_INTERVAL')
        self.assertEqual(gateway.calls, 0)

    def test_configuration_changes_halt_scheduled_launch(self):
        state = m.initial_state()
        state.update(mode='active', expires_at=999999, intent_hash='old')
        store, gateway = MemoryStore(state), Gateway()
        execute('tick', store, gateway)
        self.assertEqual(store.data['mode'], 'halted')
        self.assertEqual(gateway.calls, 0)

    def test_campaign_expiry(self):
        state = m.initial_state()
        state.update(mode='active', expires_at=999)
        store, gateway = MemoryStore(state), Gateway()
        execute('tick', store, gateway)
        self.assertEqual(store.data['mode'], 'paused')
        self.assertEqual(gateway.inspections, 0)

    def test_completion_journal_failure_retains_pending_and_prevents_second_launch(self):
        class FailCompletion(MemoryStore):
            def save(self, state):
                if state['mode'] == 'completed':
                    raise RuntimeError('completion write failed after launch')
                super().save(state)
        store, gateway = FailCompletion(), Gateway()
        execute('start', store, gateway)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(store.data['mode'], 'uncertain')
        self.assertTrue(store.data['launch_pending'])
        execute('start', store, gateway, now=10000)
        self.assertEqual(gateway.calls, 1)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.g = object.__new__(m.OciGateway)
        self.g.oci, self.g.settings = oci, settings()
        self.g.no_retry = oci.retry.NoneRetryStrategy()
        for attr, spec in [('identity', oci.identity.IdentityClient), ('compute', oci.core.ComputeClient),
                           ('block', oci.core.BlockstorageClient), ('network', oci.core.VirtualNetworkClient),
                           ('limits', oci.limits.LimitsClient)]:
            setattr(self.g, attr, Mock(spec=spec))
        self.baseline = Obj(id='baseline', shape=m.SHAPE, lifecycle_state='RUNNING',
                            availability_domain=AD, display_name='AMD-2', freeform_tags={})
        self.instances = [self.baseline]
        self.volumes = [Obj(id='v1', size_in_gbs=50, lifecycle_state='AVAILABLE'),
                        Obj(id='v2', size_in_gbs=100, lifecycle_state='AVAILABLE')]
        self.compartments = []
        self.g.network.get_subnet.return_value = Obj(data=Obj(lifecycle_state='AVAILABLE',
            availability_domain=None, prohibit_public_ip_on_vnic=False))
        self.g.compute.get_image.return_value = Obj(data=Obj(lifecycle_state='AVAILABLE', compartment_id=None,
            operating_system='Canonical Ubuntu', operating_system_version='22.04',
            display_name='Canonical-Ubuntu-22.04-2026.10.01-0', size_in_mbs=47694, listing_type='NONE'))
        self.g.limits.get_resource_availability.return_value = Obj(data=Obj(available=1))
        self.g.compute.create_compute_capacity_report.return_value = Obj(data=Obj(shape_availabilities=[
            Obj(instance_shape=m.SHAPE, availability_status='AVAILABLE', available_count=None)]), headers={})
        self.g.all = self.fake_all

    def fake_all(self, method, *args, **kwargs):
        # Bind every call to the real installed SDK signature. No network calls occur.
        import inspect
        import types
        specs = {'list_boot_volumes': oci.core.BlockstorageClient.list_boot_volumes,
                 'list_volumes': oci.core.BlockstorageClient.list_volumes}
        name = method._mock_name
        if name in specs:
            inspect.signature(specs[name]).bind(self.g.block, *args, **kwargs)
        if name == 'list_region_subscriptions':
            return [Obj(region_name=m.REGION, is_home_region=True, status='READY')]
        if name == 'list_availability_domains':
            return [Obj(name=AD)]
        if name == 'list_compartments':
            return self.compartments
        if name == 'list_instances':
            return self.instances if args[0] == TENANCY else []
        if name == 'list_boot_volumes':
            return self.volumes if kwargs['compartment_id'] == TENANCY else []
        if name in {'list_volumes', 'list_boot_volume_backups', 'list_volume_backups'}:
            return []
        if name == 'list_shapes':
            return [Obj(shape=m.SHAPE)]
        raise AssertionError('unexpected API: ' + name)

    def test_150_gb_plus_50_gb_allowed(self):
        plan = self.g.inspect()
        self.assertEqual(plan['proposed_storage_gb'], 200)
        self.g.compute.launch_instance.assert_not_called()

    def test_151_gb_blocks_creation(self):
        self.volumes[1].size_in_gbs = 101
        with self.assertRaisesRegex(m.GuardError, 'STORAGE'):
            self.g.inspect()

    def test_unknown_storage_size_blocks(self):
        self.volumes[1].size_in_gbs = None
        with self.assertRaisesRegex(m.GuardError, 'UNKNOWN_VOLUME_SIZE'):
            self.g.inspect()

    def test_nested_compartments_are_included(self):
        self.compartments = [Obj(id=COMP, lifecycle_state='ACTIVE')]
        original = self.g.all
        def inventory(method, *args, **kwargs):
            if method._mock_name == 'list_volumes' and kwargs.get('compartment_id') == COMP:
                return [Obj(id='extra', size_in_gbs=1, lifecycle_state='AVAILABLE')]
            return original(method, *args, **kwargs)
        self.g.all = inventory
        with self.assertRaisesRegex(m.GuardError, 'STORAGE'):
            self.g.inspect()

    def test_second_existing_e2_blocks(self):
        self.instances.append(Obj(id='other', shape=m.SHAPE, lifecycle_state='STOPPED',
            display_name='other', freeform_tags={}, availability_domain=AD))
        with self.assertRaisesRegex(m.GuardError, 'BASELINE'):
            self.g.inspect()

    def test_name_collision_is_not_success(self):
        self.baseline.display_name = m.NAME
        with self.assertRaisesRegex(m.GuardError, 'TARGET_NAME'):
            self.g.inspect()

    def test_existing_tagged_target_stops_before_headroom_check(self):
        self.instances.append(Obj(id='target', shape=m.SHAPE, lifecycle_state='PROVISIONING',
            display_name=m.NAME, freeform_tags={'managed-by':m.MANAGED_BY,'retry-campaign':'campaign'}))
        self.assertTrue(self.g.inspect()['existing_target'])

    def test_deleted_tagged_target_is_not_replaced(self):
        self.instances.append(Obj(id='target', shape=m.SHAPE, lifecycle_state='TERMINATED',
            display_name=m.NAME, freeform_tags={'managed-by':m.MANAGED_BY,'retry-campaign':'campaign'}))
        with self.assertRaisesRegex(m.GuardError, 'NO_REPLACEMENT'):
            self.g.inspect()

    def test_custom_image_rejected(self):
        self.g.compute.get_image.return_value.data.compartment_id = TENANCY
        with self.assertRaisesRegex(m.GuardError, 'OFFICIAL_UBUNTU'):
            self.g.inspect()

    def test_unapproved_home_region_rejected(self):
        original = self.g.all
        def not_home(method, *args, **kwargs):
            if method._mock_name == 'list_region_subscriptions':
                return [Obj(region_name=m.REGION, is_home_region=False, status='READY')]
            return original(method, *args, **kwargs)
        self.g.all = not_home
        with self.assertRaisesRegex(m.GuardError, 'HOME_REGION'):
            self.g.inspect()

    def test_read_permission_error_blocks(self):
        self.g.limits.get_resource_availability.side_effect = oci.exceptions.ServiceError(403,'NotAuthorized',{},'hidden')
        with self.assertRaises(oci.exceptions.ServiceError):
            self.g.inspect()
        self.g.compute.launch_instance.assert_not_called()

    def test_launch_body_is_fixed_and_sdk_retry_disabled(self):
        self.g.compute.launch_instance.return_value = Obj(data=Obj(id='target',lifecycle_state='PROVISIONING'),headers={})
        self.g.launch({'ad':AD},'token')
        args, kwargs = self.g.compute.launch_instance.call_args
        body = args[0]
        self.assertEqual(body.shape,m.SHAPE)
        self.assertIsNone(body.fault_domain)
        self.assertIsNone(body.shape_config)
        self.assertEqual(body.source_details.boot_volume_size_in_gbs,50)
        self.assertEqual(body.source_details.boot_volume_vpus_per_gb,10)
        self.assertEqual(body.create_vnic_details.subnet_id,self.g.settings.subnet)
        self.assertIsInstance(kwargs['retry_strategy'],oci.retry.NoneRetryStrategy)
        self.assertEqual(kwargs['opc_retry_token'],'token')
        self.g.network.create_vcn.assert_not_called()
        self.g.network.update_security_list.assert_not_called()

    def test_hardware_not_supported_stops(self):
        self.g.compute.create_compute_capacity_report.return_value.data.shape_availabilities[0].availability_status='HARDWARE_NOT_SUPPORTED'
        with self.assertRaisesRegex(m.GuardError,'CAPACITY_STATUS'):
            self.g.inspect()

    def test_contradictory_available_zero_count_stops(self):
        self.g.compute.create_compute_capacity_report.return_value.data.shape_availabilities[0].available_count=0
        with self.assertRaisesRegex(m.GuardError,'CONTRADICTORY'):
            self.g.inspect()

    def test_private_only_subnet_stops(self):
        self.g.network.get_subnet.return_value.data.prohibit_public_ip_on_vnic=True
        with self.assertRaisesRegex(m.GuardError,'PUBLIC_IP'):
            self.g.inspect()


class RepoTests(unittest.TestCase):
    def test_real_sdk_clients_initialize_without_network_or_secret_files(self):
        from dataclasses import replace
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048).private_bytes(
            serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        with patch.object(oci.base_client.BaseClient,'call_api',side_effect=AssertionError('network disabled')):
            gateway=m.OciGateway(replace(settings(),key=key))
        self.assertIsInstance(gateway.compute,oci.core.ComputeClient)

    def test_sdk_pagination_fetches_all_pages(self):
        gateway=object.__new__(m.OciGateway)
        gateway.oci=oci
        gateway.no_retry=oci.retry.NoneRetryStrategy()
        responses=[oci.response.Response(200,{'opc-next-page':'second'},['first'],None),
                   oci.response.Response(200,{},['last'],None)]
        read=Mock(side_effect=responses)
        self.assertEqual(gateway.all(read),['first','last'])
        self.assertEqual(read.call_count,2)
        self.assertEqual(read.call_args.kwargs['page'],'second')

    def test_pagination_429_is_not_retried_by_an_outer_sdk_helper(self):
        gateway=object.__new__(m.OciGateway)
        gateway.no_retry=oci.retry.NoneRetryStrategy()
        read=Mock(side_effect=oci.exceptions.ServiceError(429,'TooManyRequests',{},'limited'))
        with self.assertRaises(oci.exceptions.ServiceError):
            gateway.all(read)
        self.assertEqual(read.call_count,1)
    def test_only_expected_oci_write_calls_in_source(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path('oci_retry_micro.py').read_text())
        changes = set()
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                if node.func.attr.startswith(('create_','update_','delete_','terminate_','instance_action','launch_')):
                    changes.add(node.func.attr)
        self.assertEqual(changes,{'create_compute_capacity_report','launch_instance'})

    def test_workflow_schedule_manual_gates_and_no_pat(self):
        import yaml
        from pathlib import Path
        text=Path('.github/workflows/oci_retry_micro.yml').read_text()
        workflow=yaml.safe_load(text)
        triggers=workflow.get('on',workflow.get(True))
        self.assertEqual(triggers['schedule'][0]['cron'],'17,47 * * * *')
        self.assertEqual(triggers['workflow_dispatch']['inputs']['operation']['default'],'plan')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertNotIn('GH_PAT',text)
        self.assertNotIn('workflow_run',triggers)
        self.assertFalse(Path('.github/workflows/oci_retry.yml').exists())

    def test_settings_reject_other_region_and_private_ssh_key(self):
        env={'OCI_USER':'user','OCI_FINGERPRINT':'fp','OCI_TENANCY':TENANCY,'OCI_PRIVATE_KEY':'private',
             'OCI_SUBNET_ID':'ocid1.subnet.oc1.ap-singapore-1.test','OCI_IMAGE_ID':'ocid1.image.oc1.ap-singapore-1.test',
             'OCI_SSH_PUBLIC_KEY':PUBLIC,'GITHUB_REPOSITORY':'Jeinse/oci-arm-retry'}
        self.assertEqual(m.Settings.from_env(env).subnet,env['OCI_SUBNET_ID'])
        with self.assertRaises(m.GuardError):
            m.Settings.from_env({**env,'OCI_REGION':'us-ashburn-1'})
        with self.assertRaises(m.GuardError):
            m.Settings.from_env({**env,'OCI_SSH_PUBLIC_KEY':'-----BEGIN PRIVATE KEY-----'})

    def test_github_state_uses_blob_sha_for_compare_and_swap(self):
        store=GitHubState('Jeinse/oci-arm-retry','fake-token','a'*40,'https://github.com/example')
        store.sha='expected-blob'; store.branch_exists=True
        store.request=Mock(return_value={'content':{'sha':'new-blob'}})
        store.save(m.initial_state())
        args=store.request.call_args.args
        self.assertEqual(args[0],'PUT')
        self.assertEqual(args[2]['sha'],'expected-blob')
        self.assertEqual(args[2]['branch'],'e2-retry-state')


if __name__ == '__main__':
    unittest.main()
