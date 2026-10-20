import csv
import io
import json
import unittest
from unittest.mock import patch
from tests import test_master_server as fixtures


class CollectionQueryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MasterServerTests(); self.f.setUp()
        self.client = self.f.client; self.store = self.f.store
        self.store.upsert_machine({**self.store.get_machine('worker-local'), 'machine_id': 'second', 'name': 'Second worker'})
        for job in ('a', 'b'):
            payload = self.f.payload(job)
            for item in payload['items']: item['url'] = 'https://same.example/'
            payload['targets'] = [{'machine_id': m, 'browsers': ['chrome','edge','firefox']} for m in ('worker-local','second')]
            self.client.post('/api/v1/jobs',json=payload)
        with self.store._connection() as db:
            db.execute("UPDATE executions SET status='SUCCEEDED',result_json=?", (json.dumps({'status':'SUCCEEDED', 'artifacts': {'pcap': {'path':'fetch_output/capture.pcap','size':42}}, 'checks': {'pcap':True}, 'large_diagnostic':'x'*10000}),))
            db.execute("UPDATE executions SET status='FAILED' WHERE job_id='a' AND item_id='1' AND machine_id='worker-local' AND browser='firefox'")
            db.execute("UPDATE executions SET worker_task_id='historical-directory' WHERE job_id='a'")
        self.group=self.client.post('/api/v1/collections',json={'name':'=总体任务','job_ids':['a','b'],'revision':0}).get_json()['id']
        self.base='/api/v1/collections/'+self.group

    def tearDown(self): self.f.tearDown()

    def test_overview_counts_urls_exactly_and_never_contacts_workers(self):
        with patch('master_server.app.WorkerClient') as worker:
            data=self.client.get(self.base+'/overview').get_json(); worker.assert_not_called()
        self.assertEqual(data['stats'], {'total':24,'urls':1,'machines':2,'succeeded':23,'issues':1,'active':0})
        self.assertEqual(len(data['machines']),2)
        self.assertTrue(all(len(m['batches'])==2 for m in data['machines']))
        self.assertNotIn('large_diagnostic',json.dumps(data))

    def test_collection_metadata_and_batch_pagination_are_lightweight_and_scoped(self):
        before = self.client.get('/api/v1/collections').get_json()
        self.assertEqual(before['collections'][0]['batch_count'], 2)
        self.assertEqual(before['ungrouped']['batch_count'], 0)
        rows = []
        for offset in (0, 1):
            data = self.client.get(f'/api/v1/jobs?collection={self.group}&limit=1&offset={offset}&sort=name_asc').get_json()
            self.assertEqual(data['page']['total'], 2)
            self.assertEqual(len(data['jobs']), 1)
            rows.extend(data['jobs'])
        self.assertEqual(len({j['job_id'] for j in rows}), 2)
        self.assertNotIn('large_diagnostic', json.dumps(rows))
        self.assertEqual(self.client.get('/api/v1/jobs?collection=__ungrouped').get_json()['page']['total'], 0)
        self.store.update_job('a', status='FAILED')
        after = self.client.get('/api/v1/collections').get_json()
        self.assertNotEqual(before['collections'][0]['signature'], after['collections'][0]['signature'])
        self.assertEqual(after['collections'][0]['issue_count'], 1)
        data = self.client.get(f'/api/v1/jobs?collection={self.group}&status=FAILED&query=a&offset=999&limit=1').get_json()
        self.assertEqual(data['page']['offset'], 0)
        self.assertEqual([j['job_id'] for j in data['jobs']], ['a'])
        summary = self.client.get('/api/v1/jobs/a?unit=summary').get_json()
        self.assertEqual(summary['summary']['total'], 12)
        self.assertEqual(summary.get('items', []), [])
        self.assertNotIn('large_diagnostic', json.dumps(summary))

    def test_large_url_set_has_bounded_pages_with_all_three_browser_statuses(self):
        payload = self.f.payload('large')
        payload['items'] = [{'id': str(i), 'name': f'URL {i}', 'url': f'https://example.com/{i}'} for i in range(125)]
        payload['targets'] = [{'machine_id': 'worker-local', 'browsers': ['chrome', 'edge', 'firefox']}]
        self.assertEqual(self.client.post('/api/v1/jobs', json=payload).status_code, 202)
        base = '/api/v1/collections/__ungrouped'
        self.assertEqual(self.client.get(base + '/overview').get_json()['stats']['total'], 375)
        keys = set()
        for offset in range(0, 125, 20):
            r = self.client.get(base + f'/results?offset={offset}&limit=20')
            data = r.get_json()
            self.assertEqual(data['page']['total'], 125)
            self.assertLessEqual(len(data['rows']), 20)
            self.assertLess(len(r.data), 60000)
            for row in data['rows']:
                self.assertEqual(set(row['executions']), {'chrome', 'edge', 'firefox'})
                self.assertNotIn(row['key'], keys)
                keys.add(row['key'])
        self.assertEqual(len(keys), 125)

    def test_pages_keep_batch_item_machine_identity_and_exclude_heavy_results(self):
        rows=[]
        for offset in range(0,8,2):
            response=self.client.get(self.base+f'/results?offset={offset}&limit=2')
            self.assertLess(len(response.data),10000)
            data=response.get_json(); self.assertEqual(data['page']['total'],8)
            self.assertEqual(len(data['rows']),2); rows.extend(data['rows'])
        self.assertEqual(len({r['key'] for r in rows}),8)
        self.assertTrue(all(len(r['executions'])==3 for r in rows))
        self.assertNotIn('result_json',json.dumps(rows)); self.assertNotIn('large_diagnostic',json.dumps(rows))
        self.assertNotIn('worker_task_id',json.dumps(rows))
        detail=self.client.get(self.base+'/result?job_id=a&item_id=1&machine_id=worker-local').get_json()
        self.assertEqual(detail['executions']['firefox']['worker_task_id'],'historical-directory')
        self.assertEqual(detail['executions']['chrome']['result']['artifacts']['pcap']['size'],42)

    def test_combined_filters_match_one_execution_and_captured_evidence(self):
        data=self.client.get(self.base+'/results?browser=chrome&status=FAILED').get_json()
        self.assertEqual(data['page']['total'],0)
        data=self.client.get(self.base+'/results?browser=firefox&status=FAILED&machine=worker-local&batch=a&query=same.example').get_json()
        self.assertEqual(data['page']['total'],1); self.assertEqual(len(data['rows'][0]['executions']),3)
        with self.store._connection() as db:
            db.execute("UPDATE executions SET status='ANALYZING',result_json=? WHERE job_id='a' AND item_id='1' AND machine_id='worker-local' AND browser='firefox'", (json.dumps({'status':'CAPTURED'}),))
        data=self.client.get(self.base+'/results?browser=firefox&status=CAPTURED').get_json()
        self.assertEqual(data['page']['total'],1)
        for query in ('limit=bad','offset=bad'):
            self.assertEqual(self.client.get(self.base+'/results?'+query).status_code,400)

    def test_export_streams_all_filtered_rows_and_retains_path_provenance(self):
        r=self.client.post(self.base+'/export',json={'filters':{'browser':'firefox'},'locations':{'worker-local':{'root':'D:\\captures\\tasks'}}})
        self.assertEqual(r.status_code,200); self.assertTrue(r.data.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
        self.assertEqual(len(rows),9) # 8 rows, including rows beyond a two-row UI page
        self.assertEqual(rows[1][0], "'=总体任务")
        self.assertTrue(all(row[7]=='firefox' for row in rows[1:]))
        selected=next(row for row in rows[1:] if row[2]=='a' and row[5]==self.store.get_machine('worker-local')['name'])
        self.assertEqual(selected[11],'D:\\captures\\tasks\\historical-directory\\fetch_output\\capture.pcap')
        self.assertEqual(selected[12],'本地配置推导 · 未核验')
        self.assertIn('no-store',r.headers['Cache-Control'])

    def test_deleted_and_moved_batches_leave_scope_without_losing_records(self):
        self.store.update_job('a',status='FAILED'); self.store.delete_job('a')
        self.assertEqual(self.client.get(self.base+'/overview').get_json()['stats']['total'],12)
        self.assertEqual(self.client.get(self.base+'/result?job_id=a&item_id=1&machine_id=worker-local').status_code,404)
        revision=self.store.list_collections()['revision']
        self.client.post(self.base,json={'name':'新名','job_ids':[],'revision':revision})
        self.assertEqual(self.client.get(self.base+'/results').get_json()['page']['total'],0)
        self.assertEqual(self.client.get('/api/v1/collections/__ungrouped/results').get_json()['page']['total'],4)
        self.assertEqual(self.client.get(self.base+'/result?job_id=b&item_id=1&machine_id=worker-local').status_code,404)
