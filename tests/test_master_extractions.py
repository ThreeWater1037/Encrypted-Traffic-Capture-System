import unittest
from unittest.mock import patch

import test_standalone_extraction as fixtures
from master_server.app import create_app
from master_server.config import MasterConfig
from master_server.dispatcher import JobDispatcher
from master_server.schema import validate_job
from master_server.store import MasterStore
from master_server.worker_client import WorkerClient, WorkerRequestError


class MasterExtractionTests(unittest.TestCase):
    def setUp(self):
        self.worker = fixtures.StandaloneExtractionTests()
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        config = MasterConfig(host='127.0.0.1', port=5200, token='master-token',
                              data_dir=self.worker.fixture.root / 'master', bootstrap_worker_enabled=False)
        config.prepare()
        self.store = MasterStore(config.database_path)
        self.store.upsert_machine(dict(machine_id='m', name='Test', base_url='http://test', token='test', enabled=True))
        self.payload = validate_job({
                'job_id': 'capture-source', 'name': '已完成实验', 'items': self.worker.source['items'],
                'targets': [{'machine_id': 'm', 'browsers': ['chrome', 'edge', 'firefox']}],
                'pcap': True, 'analysis': {'steps': []}}, max_items=10)
        self.store.create_job(self.payload)
        self.store.update_job('capture-source', status='SUCCEEDED', stage='DONE')
        worker = self.worker
        class Bridge(WorkerClient):
            def _request(self, method, path, payload=None, **kwargs):
                response = worker.client.open(path, method=method, json=payload, headers=worker.auth)
                if response.status_code >= 400:
                    raise WorkerRequestError(response.get_json().get('message', 'error'), status_code=response.status_code)
                return response.get_json()
        self.bridge = Bridge('http://test', 'test')
        self.dispatcher = JobDispatcher(config, self.store, client_factory=lambda _: self.bridge, autostart=False)
        self.addCleanup(self.dispatcher.shutdown)
        self.client = create_app(config, store=self.store, dispatcher=self.dispatcher).test_client()
        self.auth = {'Authorization': 'Bearer master-token'}

    def test_full_master_worker_contract_and_refreshed_artifacts(self):
        response = self.client.get('/api/v1/extractions/candidates?machine_id=m&limit=1', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['jobs'][0]['browsers'], ['chrome', 'edge', 'firefox'])
        response = self.client.post('/api/v1/machines/m/extractions', json={'job_id': 'capture-source'}, headers=self.auth)
        self.assertEqual(response.status_code, 202)
        task_id = response.get_json()['task_id']
        self.assertEqual(self.worker.run_job(task_id)['status'], 'SUCCEEDED')
        response = self.client.get(f'/api/v1/machines/m/extractions/{task_id}', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['progress']['deleted'], 3)
        units = self.store.get_job('capture-source')['executions']
        for unit in units:
            self.assertIsNone(unit['result']['artifacts']['pcap'])
            self.assertTrue(unit['result']['artifacts']['tls_features'])
        with patch.object(self.bridge, 'get_result') as fetch:
            self.client.get(f'/api/v1/machines/m/extractions/{task_id}', headers=self.auth)
            fetch.assert_not_called()

    def test_retention_contract_reaches_worker_and_synced_manifest(self):
        response = self.client.post('/api/v1/machines/m/extractions', json={
            'job_id': 'capture-source', 'keep_pcap': True, 'keep_pcap_count': 2}, headers=self.auth)
        self.assertEqual(response.status_code, 202)
        result = response.get_json()
        self.assertTrue(result['keep_pcap'])
        self.assertEqual(result['keep_pcap_count'], 2)
        self.assertEqual(self.worker.run_job(result['task_id'])['status'], 'SUCCEEDED')
        response = self.client.get(f'/api/v1/machines/m/extractions/{result["task_id"]}', headers=self.auth)
        self.assertEqual(response.get_json()['progress']['retained'], 2)
        units = self.store.get_job('capture-source')['executions']
        self.assertEqual(sum(bool(u['result']['artifacts']['pcap']) for u in units), 2)

    def test_old_worker_cannot_silently_delete_retention_requests(self):
        with patch.object(self.bridge, 'list_extractions', return_value={'jobs': []}), \
             patch.object(self.bridge, 'submit_extraction') as submit:
            response = self.client.post('/api/v1/machines/m/extractions', json={
                'job_id': 'capture-source', 'keep_pcap': True}, headers=self.auth)
            self.assertEqual(response.status_code, 409)
            submit.assert_not_called()

    def test_invalid_retention_options_rejected_without_worker_call(self):
        with patch.object(self.bridge, 'submit_extraction') as submit:
            for options in ({'keep_pcap': 'true'}, {'keep_pcap_count': 1},
                            {'keep_pcap': True, 'keep_pcap_count': False},
                            {'keep_pcap': True, 'keep_pcap_count': -1}):
                response = self.client.post('/api/v1/machines/m/extractions',
                    json={'job_id': 'capture-source', **options}, headers=self.auth)
                self.assertEqual(response.status_code, 400)
            submit.assert_not_called()

    def test_active_jobs_not_candidates_and_worker_rechecks_live_status(self):
        self.store.update_job('capture-source', status='RUNNING')
        response = self.client.get('/api/v1/extractions/candidates?machine_id=m', headers=self.auth)
        self.assertEqual(response.get_json()['page']['total'], 0)
        self.assertEqual(self.client.post('/api/v1/machines/m/extractions', json={'job_id': 'capture-source'}, headers=self.auth).status_code, 409)
        self.store.update_job('capture-source', status='SUCCEEDED')
        self.worker.store.update_task('capture-source', status='CAPTURING')
        self.assertEqual(self.client.post('/api/v1/machines/m/extractions', json={'job_id': 'capture-source'}, headers=self.auth).status_code, 409)

    def test_old_worker_auth_pagination_and_cancellation(self):
        self.assertEqual(self.client.get('/api/v1/extractions/candidates?machine_id=m').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/extractions/candidates?offset=bad', headers=self.auth).status_code, 400)
        with patch.object(self.bridge, 'list_extractions', side_effect=WorkerRequestError('old Worker', status_code=404)):
            response = self.client.get('/api/v1/machines/m/extractions', headers=self.auth)
            self.assertEqual(response.status_code, 409)
            self.assertIn('尚未升级', response.get_json()['message'])
        result = self.client.post('/api/v1/machines/m/extractions', json={'job_id': 'capture-source'}, headers=self.auth).get_json()
        response = self.client.get('/api/v1/machines/m/extractions?limit=1', headers=self.auth)
        self.assertEqual(response.get_json()['jobs'][0]['job']['name'], '已完成实验')
        response = self.client.post(f'/api/v1/machines/m/extractions/{result["task_id"]}/cancel', headers=self.auth)
        self.assertEqual(response.get_json()['status'], 'CANCELED')

    def test_completed_detail_survives_source_index_sync_failure(self):
        task_id = self.worker.enqueue()
        self.worker.run_job(task_id)
        with patch.object(self.bridge, 'get_result', side_effect=WorkerRequestError('source resumed', status_code=409)):
            response = self.client.get(f'/api/v1/machines/m/extractions/{task_id}', headers=self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['status'], 'SUCCEEDED')
            self.assertIn('sync_error', response.get_json())
        self.store.update_job('capture-source', status='RUNNING')
        with patch.object(self.bridge, 'get_result') as fetch:
            self.client.get(f'/api/v1/machines/m/extractions/{task_id}', headers=self.auth)
            fetch.assert_not_called()
