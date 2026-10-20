import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_master_server as fixtures
from master_server.store import MasterStore


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MasterServerTests()
        self.fixture.setUp()
        self.client = self.fixture.client
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.tearDown()

    def snapshot(self):
        return self.client.get('/api/v1/collections').get_json()

    def create(self, name='福布斯中国', jobs=None):
        return self.client.post('/api/v1/collections', json={
            'name': name, 'job_ids': jobs or [], 'revision': self.snapshot()['revision']})

    def job(self, job_id, choice=None):
        payload = self.fixture.payload(job_id)
        payload['targets'][0]['browsers'] = ['chrome', 'edge', 'firefox']
        if choice is not None:
            payload['collection'] = choice
        return self.client.post('/api/v1/jobs', json=payload)

    def test_grouped_creation_is_atomic_and_capture_request_unchanged(self):
        response = self.job('first', {'new_name': '福布斯中国'})
        self.assertEqual(response.status_code, 202)
        group = response.get_json()['collection']
        response = self.job('second', {'group_id': group['id']})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.snapshot()['collections'][0]['jobs'], ['first', 'second'])
        # Group metadata must not enter the dispatcher/Worker request contract.
        self.assertNotIn('collection', self.store.get_job('first')['request'])
        self.assertEqual({e['browser'] for e in self.store.get_job('first')['executions']}, {'chrome', 'edge', 'firefox'})
        reopened = MasterStore(self.fixture.config.database_path)
        self.assertEqual(reopened.list_collections(), self.snapshot())
        # A DB failure after group creation rolls back both the group and batch.
        broken = self.fixture.payload('broken')
        broken['targets'][0]['machine_id'] = 'missing'
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_job(broken, collection={'new_name': '回滚'})
        self.assertFalse(self.store.job_id_exists('broken'))
        self.assertEqual(len(self.snapshot()['collections']), 1)

    def test_invalid_assignment_and_duplicate_name_never_enqueue(self):
        self.create()
        for choice, status in [({'group_id': 'missing'}, 400), ({'new_name': '福布斯中国'}, 409),
                               ({}, 400), ({'new_name': 1}, 400), ({'new_name': 'x', 'group_id': 'y'}, 400)]:
            with self.subTest(choice=choice), patch.object(self.fixture.dispatcher, 'enqueue') as enqueue:
                self.assertEqual(self.job('invalid', choice).status_code, status)
                enqueue.assert_not_called()
                self.assertFalse(self.store.job_id_exists('invalid'))
        self.assertEqual(self.job('legacy').status_code, 202)
        self.assertIsNone(self.store.job_collection('legacy'))

    def test_form_creation_and_restart_inherit_group_resume_preserves_membership(self):
        group = self.create().get_json()['id']
        response = self.client.post('/api/v1/jobs/from-file', data={
            'file': (io.BytesIO(b'1\tExample\thttps://example.com/\n'), 'urls.txt'),
            'name': '文件批次', 'targets': json.dumps([{'machine_id': 'worker-local', 'browsers': ['firefox']}]),
            'collection': json.dumps({'group_id': group}),
        })
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()['job_id']
        self.assertEqual(response.get_json()['collection']['id'], group)
        self.store.update_job(job_id, status='FAILED')
        restarted = self.client.post(f'/api/v1/jobs/{job_id}/restart', json={})
        self.assertEqual(restarted.status_code, 202)
        self.assertEqual(restarted.get_json()['collection']['id'], group)
        before = self.snapshot()
        resumed = self.client.post(f'/api/v1/jobs/{job_id}/resume')
        self.assertEqual(resumed.status_code, 202)
        after = self.snapshot()
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual([(g['id'], g['jobs']) for g in before['collections']],
                         [(g['id'], g['jobs']) for g in after['collections']])

    def test_regroup_is_exclusive_and_stale_edits_are_rejected(self):
        self.job('a'); self.job('b')
        first = self.create('第一组', ['a']).get_json()
        old_revision = first['revision']
        second = self.create('第二组', ['a', 'b']).get_json()
        self.assertEqual(next(g for g in second['collections'] if g['id'] == first['id'])['jobs'], [])
        response = self.client.post('/api/v1/collections/' + first['id'], json={
            'name': '第一组', 'job_ids': ['a'], 'revision': old_revision})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.job_collection('a')['id'], second['id'])
        # Removing one member leaves it ungrouped; it never deletes the capture.
        response = self.client.post('/api/v1/collections/' + second['id'], json={
            'name': '第二组重命名', 'job_ids': ['b'], 'revision': second['revision']})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.job_collection('a'))
        self.assertIsNotNone(self.store.get_job('a'))
        self.store.update_job('b', status='FAILED')
        self.store.delete_job('b')
        self.assertTrue(all(not g['jobs'] for g in self.snapshot()['collections']))
        self.assertEqual(self.create('无效组', ['b']).status_code, 400)

    def test_validation_auth_and_no_cache(self):
        for data in [None, {}, {'name': 'x', 'job_ids': [], 'revision': True},
                     {'name': 'x', 'job_ids': ['x', 'x'], 'revision': 0}]:
            self.assertEqual(self.client.post('/api/v1/collections', json=data).status_code, 400)
        response = self.client.get('/api/v1/collections')
        self.assertIn('no-store', response.headers['Cache-Control'])
        from dataclasses import replace
        from master_server.app import create_app
        app = create_app(replace(self.fixture.config, token='secret'), store=self.store, dispatcher=self.fixture.dispatcher)
        self.assertEqual(app.test_client().get('/api/v1/collections').status_code, 401)
        self.assertEqual(app.test_client().post('/api/v1/collections', json={}).status_code, 401)
