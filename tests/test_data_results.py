import csv
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests import test_master_server as fixtures
from master_server.app import create_app


class DataResultsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MasterServerTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store; self.client = self.fixture.client
        self.store.upsert_machine({**self.store.get_machine('worker-local'), 'machine_id': 'second', 'name': '第二台'})
        for job in ('a', 'b', 'ungrouped'):
            payload = self.fixture.payload(job)
            payload['items'] = [{'id': str(i), 'name': '页面', 'url': f'https://example.org/{i}'} for i in range(5)]
            payload['targets'] = [{'machine_id': m, 'browsers': ['chrome', 'edge', 'firefox']} for m in ('worker-local', 'second')]
            self.store.create_job(payload)
        result = {'artifacts': {'pcap': {'path': 'fetch_output/1/capture.pcap', 'size': 42},
                                'tls_keylog': {'path': 'fetch_output/1/tls.log', 'size': 20}},
                  'checks': {'pcap': True, 'tls_keylog': True}, 'diagnostic': 'x' * 10000}
        with self.store._connection() as db:
            db.execute('UPDATE executions SET result_json=?,status=\'SUCCEEDED\'', (json.dumps(result),))
            db.execute("UPDATE executions SET worker_task_id='historical-directory' WHERE job_id='a'")
        self.group = self.store.save_collection(None, '新闻采集', ['a', 'b'], 0)['id']

    def get(self, path):
        response = self.client.get('/api/v1/data/' + path)
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()

    def test_verified_pcap_retirement_is_not_reported_as_missing(self):
        result = {'artifacts': {'pcap': None, 'tls_keylog': {'path': 'fetch_output/1/tls.log', 'size': 20},
                               'flow_sequences': {'path': 'fetch_output/1/flow_extraction/chrome/sequences', 'size': 42}},
                  'checks': {'pcap_retired': True, 'tls_keylog': True, 'flow_sequences': True, 'tls_features': True}}
        with self.store._connection() as db:
            db.execute('UPDATE executions SET result_json=?', (json.dumps(result),))
        data = self.get('results?offset=0&limit=20')
        self.assertTrue(all(row['files']['pcap'] == 'retired' for row in data['rows']))
        detail = self.get(f"result?execution_id={data['rows'][0]['execution_id']}")
        pcap = next(row for row in detail['files'] if row['kind'] == 'pcap')
        self.assertEqual(pcap['state'], 'retired')
        self.assertEqual(pcap['path'], '')
        exported = self.client.post('/api/v1/data/export', json={})
        self.assertEqual(exported.status_code, 200)
        self.assertIn('提取完成，原始 PCAP 已删除', exported.data.decode('utf-8-sig'))

    def test_execution_pagination_is_bounded_and_not_full_results(self):
        keys = set()
        for offset in range(0, 90, 20):
            data = self.get(f'results?offset={offset}&limit=20')
            self.assertEqual(data['page']['total'], 90)
            self.assertLessEqual(len(data['rows']), 20)
            self.assertNotIn('diagnostic', json.dumps(data))
            self.assertNotIn('fetch_output', json.dumps(data))
            self.assertNotIn('result_json', json.dumps(data))
            for row in data['rows']:
                self.assertNotIn(row['execution_id'], keys); keys.add(row['execution_id'])
                self.assertEqual(row['files'], {'pcap': 'present', 'tls': 'present'})
        self.assertEqual(len(keys), 90)
        self.assertEqual(self.get('results?offset=900&limit=20')['page']['offset'], 80)

    def test_browser_machine_query_and_group_filter_match_same_records(self):
        for browser in ('chrome', 'edge', 'firefox'):
            query = f'group={self.group}&browser={browser}&machine=second&query=example.org/2'
            rows = self.get('results?' + query)['rows']
            self.assertEqual(len(rows), 2)
            self.assertEqual({r['job_id'] for r in rows}, {'a', 'b'})
            self.assertEqual({r['browser'] for r in rows}, {browser})
            with patch('master_server.app.WorkerClient') as worker:
                overview = self.get('overview?' + query); worker.assert_not_called()
            self.assertEqual(overview['stats'], {'records': 2, 'urls': 1, 'batches': 2})
            self.assertEqual([m['records'] for m in overview['machines']], [2, 2])
        self.assertEqual(self.get('results?group=__ungrouped')['page']['total'], 30)
        self.assertEqual(self.get('results?query=does-not-exist')['rows'], [])

    def test_file_summary_does_not_infer_files_from_success(self):
        with self.store._connection() as db:
            db.execute("UPDATE executions SET result_json=NULL WHERE browser='chrome'")
            db.execute("UPDATE executions SET result_json=? WHERE browser='edge'", (json.dumps({'artifacts': {'pcap': {'path': '/capture.pcap', 'size': 0}}}),))
            db.execute("UPDATE executions SET result_json=? WHERE browser='firefox'", (json.dumps({'artifacts': {'pcap': {'absolute_path': '/capture.pcap', 'size': 100}}, 'checks': {'pcap': False}}),))
        for browser, expected in [('chrome', 'missing'), ('edge', 'invalid'), ('firefox', 'invalid')]:
            row = self.get('results?browser=' + browser)['rows'][0]
            self.assertEqual(row['files'], {'pcap': expected, 'tls': 'missing'})

    def test_detail_export_share_complete_historical_paths_and_unknown_provenance(self):
        response = self.client.post('/api/v1/data/locations', json={'machine_id': 'worker-local', 'root': 'D:\\captures\\tasks'})
        self.assertEqual(response.status_code, 200)
        row = next(r for r in self.get('results?batch=a&browser=firefox')['rows'] if r['machine_id'] == 'worker-local')
        detail = self.get(f'result?execution_id={row["execution_id"]}&group={self.group}')
        self.assertEqual(detail['worker_task_id'], 'historical-directory')
        self.assertEqual(detail['files'][0]['path'], 'D:\\captures\\tasks\\historical-directory\\fetch_output\\1\\capture.pcap')
        self.assertTrue(detail['files'][0]['absolute'])
        self.assertIn('未核验', detail['files'][0]['source'])
        self.assertNotIn('diagnostic', json.dumps(detail))
        response = self.client.post('/api/v1/data/export', json={'group': self.group, 'filters': {'browser': 'firefox'}})
        table = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(table), 41)  # All 20 matching executions, two files each, beyond UI page.
        self.assertTrue(all(row[7] == 'firefox' for row in table[1:]))
        # Both fixture Workers have the same URL; select the actual configured machine by its name.
        selected = next(r for r in table[1:] if r[2] == 'a' and r[5] == detail['machine_name'] and r[9] == 'PCAP')
        self.assertEqual(selected[10], detail['files'][0]['path'])
        unknown = next(r for r in table[1:] if r[5] == '第二台' and r[9] == 'PCAP')
        self.assertEqual(unknown[12], '否'); self.assertIn('完整位置待补充', unknown[11])

    def test_groups_are_paged_without_members_and_edit_metadata_has_revision(self):
        revision = 1
        for i in range(7):
            revision = self.store.save_collection(None, f'其他组{i}', [], revision)['revision']
        data = self.get('groups?limit=3')
        self.assertEqual(data['page']['total'], 8); self.assertEqual(len(data['groups']), 3)
        self.assertNotIn('jobs', json.dumps(data)); self.assertEqual(data['all_count'], 3)
        self.assertEqual(data['ungrouped_count'], 1)
        self.assertEqual(self.get('groups?query=新闻')['groups'][0]['batch_count'], 2)
        group = self.get('groups/' + self.group)
        self.assertEqual(group['job_ids'], ['a', 'b']); self.assertEqual(group['revision'], revision)

    def test_deleted_or_moved_results_are_not_accessible_in_old_scope(self):
        row = self.get('results?batch=a')['rows'][0]
        self.store.update_job('a', status='FAILED'); self.store.delete_job('a')
        self.assertEqual(self.client.get('/api/v1/data/result?execution_id=' + str(row['execution_id'])).status_code, 404)
        self.assertEqual(self.get('overview')['stats']['records'], 60)
        self.store.save_collection(self.group, '新闻采集', [], 1)
        self.assertEqual(self.get('results?group=' + self.group)['page']['total'], 0)

    def test_validation_auth_and_csv_formula_protection(self):
        for root in ('relative', '/tasks/../other', 'C:\\tasks\\..\\other'):
            self.assertEqual(self.client.post('/api/v1/data/locations', json={'machine_id': 'second', 'root': root}).status_code, 400)
        self.assertEqual(self.client.get('/api/v1/data/results?limit=bad').status_code, 400)
        self.assertEqual(self.client.get('/api/v1/data/results?group=missing').status_code, 400)
        self.store.save_collection(self.group, '=formula', ['a', 'b'], 1)
        r = self.client.post('/api/v1/data/export', json={'group': self.group, 'filters': {'browser': 'firefox'}})
        self.assertIn("'=formula", r.data.decode('utf-8-sig'))
        self.assertIn('no-store', r.headers['Cache-Control'])
        app = create_app(replace(self.fixture.config, token='secret'), store=self.store, dispatcher=self.fixture.dispatcher)
        self.assertEqual(app.test_client().get('/api/v1/data/results').status_code, 401)
        self.assertEqual(app.test_client().post('/api/v1/data/locations', json={}).status_code, 401)
