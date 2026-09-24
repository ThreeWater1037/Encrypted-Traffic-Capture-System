from __future__ import annotations

import io
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch

from werkzeug.serving import make_server

from master_server.schema import new_job_id, named_task_id
from master_server.store import MasterStore
from master_server.worker_client import WorkerClient
from tests import test_master_server as master_fixtures
from tests import test_worker_agent as worker_fixtures


class JobManagementTests(unittest.TestCase):
    def setUp(self):
        self.master = master_fixtures.MasterServerTests()
        self.master.setUp()
        self.addCleanup(self.master.tearDown)

    def test_soft_delete_keeps_uploads_executions_and_worker_resources(self):
        response = self.master.client.post('/api/v1/jobs/from-file', data={
            'name': '保留资源实验', 'file': (io.BytesIO(b'1\tExample\thttps://example.com/\n'), 'input.tsv'),
            'targets': '[{"machine_id":"worker-local","browsers":["chrome"]}]',
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()['job_id']
        job = self.master.store.get_job_control(job_id)
        upload = self.master.config.data_dir / job['source_path']
        task_id = self.master.store.get_worker_task_id(job_id, 'worker-local')
        artifact = self.master.config.data_dir / 'worker-fixture' / task_id / 'capture.pcap'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'original capture bytes')
        self.master.store.update_job(job_id, status='PARTIAL')
        url = '/api/v1/jobs/' + quote(job_id, safe='')
        with patch('master_server.app.WorkerClient') as worker_client:
            for _ in range(2):
                deleted = self.master.client.delete(url)
                self.assertEqual(deleted.status_code, 200)
                self.assertTrue(deleted.get_json()['resources_preserved'])
            worker_client.assert_not_called()
        self.assertTrue(upload.is_file())
        self.assertEqual(artifact.read_bytes(), b'original capture bytes')
        self.assertEqual(self.master.store.get_worker_task_id(job_id, 'worker-local'), task_id)
        self.assertEqual(self.master.client.get('/api/v1/jobs').get_json()['page']['total'], 0)
        for suffix in ('', '/results', '/logs'):
            self.assertEqual(self.master.client.get(url + suffix).status_code, 404)
        for action in ('cancel', 'resume', 'restart'):
            self.assertEqual(self.master.client.post(url + '/' + action, json={}).status_code, 404)
        self.assertFalse(self.master.store.resume_job(job_id, 'resume-test'))
        payload = self.master.payload(job_id)
        self.assertEqual(self.master.client.post('/api/v1/jobs', json=payload).status_code, 409)
        reopened = MasterStore(self.master.config.database_path)
        self.assertEqual(reopened.list_jobs(), [])
        self.assertTrue(reopened.job_id_exists(job_id))

    def test_delete_active_job_requires_cancellation(self):
        payload = self.master.payload('active-delete')
        self.master.store.create_job(payload)
        for status in ('CREATED', 'RUNNING', 'CANCELING'):
            self.master.store.update_job(payload['job_id'], status=status)
            response = self.master.client.delete('/api/v1/jobs/active-delete')
            self.assertEqual(response.status_code, 409)
            self.assertIsNotNone(self.master.store.get_job_status(payload['job_id']))
        self.master.store.update_job(payload['job_id'], status='CANCELED')
        self.assertEqual(self.master.client.delete('/api/v1/jobs/active-delete').status_code, 200)
        self.assertEqual(self.master.client.delete('/api/v1/jobs/missing').status_code, 404)

    def test_sort_applies_before_pagination_and_filtering(self):
        for job_id, name, status, created in (
            ('a', 'Zulu', 'SUCCEEDED', '2026-01-01'),
            ('b', 'Alpha', 'RUNNING', '2026-01-03'),
            ('c', 'Bravo', 'FAILED', '2026-01-02'),
        ):
            payload = self.master.payload(job_id); payload['name'] = name
            self.master.store.create_job(payload)
            with self.master.store._connection() as connection:
                connection.execute('UPDATE jobs SET created_at=?, updated_at=?, status=? WHERE job_id=?', (created, created, status, job_id))
        for sort, expected in (
            ('created_desc', ['b', 'c', 'a']), ('created_asc', ['a', 'c', 'b']),
            ('updated_desc', ['b', 'c', 'a']), ('name_asc', ['b', 'c', 'a']),
            ('name_desc', ['a', 'c', 'b']), ('status', ['b', 'c', 'a']),
        ):
            with self.subTest(sort=sort):
                ids = []
                for offset in range(3):
                    response = self.master.client.get('/api/v1/jobs', query_string={'sort': sort, 'offset': offset, 'limit': 1}).get_json()
                    ids.extend(job['job_id'] for job in response['jobs'])
                self.assertEqual(ids, expected)
        response = self.master.client.get('/api/v1/jobs?sort=name_asc&status=FAILED&query=bravo').get_json()
        self.assertEqual([job['job_id'] for job in response['jobs']], ['c'])
        self.assertEqual(response['running_count'], 1)
        self.assertEqual(self.master.client.get('/api/v1/jobs?sort=created_at;DROP%20TABLE%20jobs').status_code, 400)

    def test_generated_names_are_readable_unique_and_safe(self):
        ids = {new_job_id('中文采集实验') for _ in range(100)}
        self.assertEqual(len(ids), 100)
        for value in ids:
            self.assertRegex(value, r'^中文采集实验-[0-9a-f]{16}$')
        for name in ('中文' * 60, '../a/b\\c:*?<>|', 'CON.txt', '...', '😀😀'):
            task_id = new_job_id(name)
            self.assertLessEqual(len(task_id), 64)
            self.assertLessEqual(len(task_id.encode('utf-8')), 113)
            self.assertNotRegex(task_id, r'[/\\:*?<>|]')
            self.assertFalse(task_id.startswith('.'))
        self.assertTrue(new_job_id('CON.txt').startswith('task-CON.txt-'))

    def test_new_worker_directories_and_legacy_mapping(self):
        payload = self.master.payload(); payload.pop('job_id'); payload['name'] = '中文采集实验'
        response = self.master.client.post('/api/v1/jobs', json=payload)
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()['job_id']
        task_id = self.master.store.get_worker_task_id(job_id, 'worker-local')
        self.assertRegex(task_id, r'^中文采集实验-[0-9a-f]{16}$')
        self.assertNotEqual(task_id, named_task_id(payload['name'], f'{job_id}\0another-machine'))
        # Existing execution mappings must win, even after upgrading the naming rule.
        legacy = MasterStore.worker_task_id(job_id, 'worker-local')
        with self.master.store._connection() as connection:
            connection.execute('UPDATE executions SET worker_task_id=? WHERE job_id=?', (legacy, job_id))
        self.master.dispatcher._run_job(job_id)
        self.assertEqual(self.master.fake_worker.task_id, legacy)
        self.assertEqual(self.master.store.get_worker_task_id(job_id, 'worker-local'), legacy)

    def test_chinese_ids_work_over_real_worker_http_and_create_named_directory(self):
        worker = worker_fixtures.WorkerAgentApiTests(); worker.setUp()
        self.addCleanup(worker.tearDown)
        server = make_server('127.0.0.1', 0, worker.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = WorkerClient(f'http://127.0.0.1:{server.server_port}', 'test-token')
            task_id = new_job_id('中文采集实验')
            payload = worker.payload(task_id)
            client.submit_task(payload)
            self.assertTrue((worker.config.tasks_dir / task_id).is_dir())
            self.assertEqual(client.get_task(task_id)['task_id'], task_id)
            self.assertEqual(client.get_capture_progress(task_id)['task_id'], task_id)
            self.assertEqual(client.get_log(task_id, tail_lines=10)['tail_lines'], 10)
            client.cancel_task(task_id)
            self.assertEqual(client.get_task(task_id)['status'], 'CANCELED')
            client.resume_task(task_id, 'resume-naming-test')
            self.assertEqual(client.get_task(task_id)['status'], 'QUEUED')
        finally:
            server.shutdown(); thread.join(timeout=5); server.server_close()

    def test_legacy_database_gets_delete_column_without_losing_jobs(self):
        self.master.store.create_job(self.master.payload('legacy-job'))
        with self.master.store._connection() as connection:
            connection.execute('ALTER TABLE jobs DROP COLUMN deleted_at')
        reopened = MasterStore(self.master.config.database_path)
        self.assertIsNotNone(reopened.get_job_status('legacy-job'))
        reopened.update_job('legacy-job', status='FAILED')
        self.assertEqual(reopened.delete_job('legacy-job'), 'deleted')


if __name__ == '__main__':
    unittest.main()
