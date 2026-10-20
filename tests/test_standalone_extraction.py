import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_extraction as extraction
import test_capture_extraction as fixtures
from worker_agent.app import create_app
from worker_agent.config import WorkerConfig
from worker_agent.task_runner import TaskManager, TaskConflictError, TaskServiceStoppingError
from worker_agent.task_store import TaskStore


class StandaloneExtractionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExtractionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = WorkerConfig(worker_id='test', host='127.0.0.1', port=5100, token='test',
            project_root=extraction.ROOT, python_executable=Path(sys.executable), data_dir=self.fixture.root)
        self.config.prepare()
        self.store = TaskStore(self.config.database_path)
        self.manager = TaskManager(self.config, self.store, autostart=False)
        self.addCleanup(self.manager.shutdown)
        self.source = dict(task_id='capture-source', items=[vars(self.fixture.entry)],
            browsers=list(extraction.BROWSERS), pcap=True, outputs={}, analysis={'steps': [], 'pipeline': False})
        self.store.create_task(self.source)
        self.store.update_task('capture-source', status='SUCCEEDED', stage='DONE')
        target = self.config.tasks_dir / 'capture-source' / 'fetch_output' / self.fixture.directory.name
        target.parent.mkdir(parents=True)
        self.fixture.directory.rename(target)
        self.fixture.directory = target
        for browser in extraction.BROWSERS:
            self.fixture.capture(browser)
        self.client = create_app(self.config, store=self.store, manager=self.manager).test_client()
        self.auth = {'Authorization': 'Bearer test'}

    def enqueue(self, **options):
        result, _ = self.manager.extractions.submit('capture-source', **options)
        return result['task_id']

    def execute(self, task_id, command, log_path, deadline):
        browser = command[3]
        steps = [arg for arg in command[command.index('--steps') + 1:] if not arg.startswith('--')] if '--steps' in command else None
        self.fixture.run_extract(browser, delete='--delete-pcap' in command, steps=steps)
        return 0

    def test_step_selection_can_run_separately_with_distinct_jobs_and_paths(self):
        flow = self.enqueue(keep_pcap=True, steps=['flow'])
        with self.assertRaises(TaskConflictError):
            self.enqueue(keep_pcap=True, steps=['tls'])
        with self.assertRaises(TaskConflictError):
            self.manager.resume('capture-source', 'token')
        self.assertEqual(self.run_job(flow)['status'], 'SUCCEEDED')
        for unit in self.store.get_task('capture-source')['result']['units']:
            self.assertTrue(unit['checks']['flow_sequences'])
            self.assertNotIn('tls_features', unit['checks'])
            self.assertFalse(unit['artifacts'].get('tls_features'))
        tls = self.enqueue(keep_pcap=True, steps=['tls'])
        self.assertNotEqual(flow, tls)
        self.fixture.calls.clear()
        self.assertEqual(self.run_job(tls)['status'], 'SUCCEEDED')
        self.assertEqual(self.fixture.calls, ['extract_tls.py'] * 3)
        for unit in self.store.get_task('capture-source')['result']['units']:
            self.assertTrue(unit['artifacts']['flow_sequences'])
            self.assertTrue(unit['artifacts']['tls_features'])
            self.assertEqual(unit['status'], 'SUCCEEDED')
        combined = self.enqueue(steps=['tls', 'flow'])
        self.fixture.calls.clear()
        self.assertEqual(self.run_job(combined)['status'], 'SUCCEEDED')
        self.assertEqual(self.fixture.calls, [])

    def test_single_steps_and_empty_selection_api(self):
        for steps in ([], ['bad'], ['tls', 'tls'], 'tls', [False]):
            response = self.client.post('/api/v1/extractions', json={
                'source_task_id': 'capture-source', 'steps': steps}, headers=self.auth)
            self.assertEqual(response.status_code, 400)
        result = self.run_job(self.enqueue(steps=['tls']))
        self.assertEqual(result['steps'], ['tls'])
        self.assertEqual(result['status'], 'SUCCEEDED')
        for unit in self.store.get_task('capture-source')['result']['units']:
            self.assertTrue(unit['checks']['tls_features'])
            self.assertNotIn('flow_sequences', unit['checks'])
            self.assertTrue(unit['checks']['pcap_retired'])

    def run_job(self, task_id):
        with patch.object(self.manager, '_run_command', side_effect=self.execute):
            self.manager._run_task(self.store.get_task(task_id))
        return self.manager.extractions.get(task_id)

    def test_three_browser_sequence_and_tls_deletion_and_source_manifest(self):
        task_id = self.enqueue()
        result = self.run_job(task_id)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['progress']['deleted'], 3)
        self.assertEqual(self.fixture.calls.count('extract_tls.py'), 3)
        source = self.store.get_task('capture-source')
        self.assertFalse(source['request']['analysis']['pipeline'])
        self.assertEqual(source['status'], 'SUCCEEDED')
        for unit in source['result']['units']:
            self.assertTrue(unit['checks']['pcap_retired'])
            self.assertIsNone(unit['artifacts']['pcap'])
            self.assertTrue(unit['artifacts']['flow_sequences'])
            self.assertTrue(unit['artifacts']['tls_features'])
        self.assertEqual(self.enqueue(), task_id)
        self.assertEqual(self.manager.queue_size, 1)  # unchanged admission, no duplicate

    def test_keep_all_preserves_all_three_browsers_and_manifest(self):
        task_id = self.enqueue(keep_pcap=True)
        result = self.run_job(task_id)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['progress']['retained'], 3)
        self.assertEqual(result['progress']['deleted'], 0)
        for unit in self.store.get_task('capture-source')['result']['units']:
            self.assertTrue(unit['artifacts']['pcap'])
            self.assertTrue(unit['artifacts']['tls_features'])
        for browser in extraction.BROWSERS:
            self.assertTrue((self.fixture.directory / f'capture_{browser}.pcap').exists())

    def test_keep_first_two_and_restart_keeps_original_positions(self):
        task_id = self.enqueue(keep_pcap=True, keep_pcap_count=2)
        def yield_after_first(*args):
            self.execute(*args)
            self.manager._queue.put('next-capture')
            return 0
        with patch.object(self.manager, '_run_command', side_effect=yield_after_first):
            self.manager._run_task(self.store.get_task(task_id))
        self.assertEqual(self.manager.extractions.get(task_id)['progress']['retained'], 1)
        self.manager._queue.get_nowait(); self.manager._queue.task_done()
        restored = TaskManager(self.config, self.store, autostart=False)
        self.addCleanup(restored.shutdown)
        self.assertEqual(restored._extraction_queue.get_nowait()[1], task_id)
        with patch.object(restored, '_run_command', side_effect=self.execute):
            restored._run_task(self.store.get_task(task_id))
        result = restored.extractions.get(task_id)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['progress']['retained'], 2)
        self.assertEqual(result['progress']['deleted'], 1)
        self.assertEqual([(self.fixture.directory / f'capture_{b}.pcap').exists() for b in extraction.BROWSERS], [True, True, False])

    def test_limit_larger_than_total_keeps_firefox_too(self):
        result = self.run_job(self.enqueue(keep_pcap=True, keep_pcap_count=100))
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['progress']['retained'], 3)
        self.assertEqual(result['progress']['deleted'], 0)

    def test_failure_outside_retention_limit_is_kept_and_retry_uses_same_policy(self):
        options = dict(keep_pcap=True, keep_pcap_count=1)
        task_id = self.enqueue(**options)
        def command(*args):
            self.fixture.fail_stage = 'extract_tls.py' if args[1][3] == 'firefox' else None
            return self.execute(*args)
        with patch.object(self.manager, '_run_command', side_effect=command):
            self.manager._run_task(self.store.get_task(task_id))
        result = self.manager.extractions.get(task_id)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['progress']['retained'], 1)
        self.assertTrue((self.fixture.directory / 'capture_firefox.pcap').exists())
        with self.assertRaises(TaskConflictError):
            self.enqueue(keep_pcap=True)
        self.assertEqual(self.manager.extractions.get(task_id)['status'], 'PARTIAL')
        self.fixture.fail_stage = None
        self.fixture.calls.clear()
        self.enqueue(**options)
        result = self.run_job(task_id)
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['progress']['retained'], 1)
        self.assertEqual(result['progress']['deleted'], 2)
        self.assertEqual(self.fixture.calls.count('extract_tls.py'), 1)

    def test_retention_options_are_validated_before_enqueue(self):
        for options in ({'keep_pcap': 'true'}, {'keep_pcap': 1}, {'keep_pcap': None},
                        {'keep_pcap_count': 1}, *({'keep_pcap': True, 'keep_pcap_count': n}
                        for n in (0, -1, 1.5, True, '2'))):
            with self.subTest(options=options):
                response = self.client.post('/api/v1/extractions',
                    json={'source_task_id': 'capture-source', **options}, headers=self.auth)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.manager.queue_size, 0)

    def test_retention_does_not_claim_to_restore_previously_deleted_pcap(self):
        self.fixture.run_extract('firefox')
        result = self.run_job(self.enqueue(keep_pcap=True))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['progress']['retained'], 2)
        self.assertEqual(result['progress']['failed'], 1)

    def test_active_capture_cannot_be_selected_and_source_cannot_resume_during_extraction(self):
        for state in ('CAPTURING', 'QUEUED', 'ANALYZING', 'CANCELED', 'FAILED'):
            self.store.update_task('capture-source', status=state)
            self.assertEqual(self.client.post('/api/v1/extractions', json={'source_task_id': 'capture-source'}, headers=self.auth).status_code, 409)
        self.store.update_task('capture-source', status='PARTIAL')
        self.enqueue()
        with self.assertRaises(TaskConflictError):
            self.manager.resume('capture-source', 'token')

    def test_capture_preempts_at_pcap_boundary(self):
        task_id = self.enqueue()
        def command(*args):
            self.execute(*args)
            self.manager._queue.put('next-capture')
            return 0
        with patch.object(self.manager, '_run_command', side_effect=command) as run:
            self.manager._run_task(self.store.get_task(task_id))
            self.assertEqual(run.call_count, 1)
        result = self.manager.extractions.get(task_id)
        self.assertEqual(result['status'], 'QUEUED')
        self.assertEqual(result['progress']['cursor'], 1)
        self.manager._queue.get_nowait(); self.manager._queue.task_done()
        self.assertEqual(self.run_job(task_id)['status'], 'SUCCEEDED')

    def test_waiting_capture_prevents_any_extraction_and_worker_loop_prioritizes_capture(self):
        task_id = self.enqueue()
        self.manager._queue.put('next-capture')
        with patch.object(self.manager, '_run_command') as run:
            self.manager._run_task(self.store.get_task(task_id))
            run.assert_not_called()
        self.manager._queue.get_nowait(); self.manager._queue.task_done()
        payload = dict(self.source, task_id='next-capture')
        self.store.create_task(payload)
        self.manager._queue.put('next-capture')
        order = []
        def consume(task):
            order.append(task['task_id'])
            self.manager._stop_event.set()
        with patch.object(self.manager, '_run_task', side_effect=consume), patch.object(self.manager, '_cleanup_temp_if_due'):
            self.manager._worker_loop()
        self.assertEqual(order, ['next-capture'])

    def test_yielded_extraction_keeps_fifo_order_ahead_of_later_extraction(self):
        first = self.enqueue()
        self.manager._extraction_queue.get_nowait(); self.manager._extraction_queue.task_done()
        second_source = dict(self.source, task_id='later-source')
        self.store.create_task(second_source)
        self.store.update_task('later-source', status='SUCCEEDED', stage='DONE')
        second, _ = self.manager.extractions.submit('later-source')
        self.manager.enqueue_extraction(first)
        self.assertEqual(self.manager._extraction_queue.get_nowait()[1], first)
        self.assertEqual(self.manager._extraction_queue.get_nowait()[1], second['task_id'])

    def test_item_id_with_wiki_separator_is_resolved_by_checkpoint(self):
        item = dict(vars(self.fixture.entry), id='id-wiki-part')
        source = dict(self.source, items=[item])
        for browser in extraction.BROWSERS:
            marker = self.fixture.directory / f'capture_{browser}.complete.json'
            value = json.loads(marker.read_text()); value['item_id'] = item['id']
            marker.write_text(json.dumps(value))
        units = list(self.manager.extractions._units({'task_id': 'capture-source', 'request': source}))
        self.assertEqual(len(units), 3)
        self.assertTrue(all(directory == self.fixture.directory for _, _, directory in units))

    def test_restart_after_deletion_recovers_receipt_and_does_not_repeat_analysis(self):
        task_id = self.enqueue()
        def crash(*args):
            self.execute(*args)
            raise TaskServiceStoppingError()
        with patch.object(self.manager, '_run_command', side_effect=crash):
            self.manager._run_task(self.store.get_task(task_id))
        self.assertEqual(self.manager.extractions.get(task_id)['progress']['cursor'], 0)
        self.assertFalse((self.fixture.directory / 'capture_chrome.pcap').exists())
        restored = TaskManager(self.config, self.store, autostart=False)
        self.addCleanup(restored.shutdown)
        self.assertEqual(restored._extraction_queue.get_nowait()[1], task_id)
        self.assertTrue(restored._queue.empty())
        before = self.fixture.calls.count('extract_tls.py')
        self.assertEqual(self.run_job(task_id)['status'], 'SUCCEEDED')
        self.assertEqual(self.fixture.calls.count('extract_tls.py') - before, 2)

    def test_tls_failure_preserves_pcap_and_retry_reuses_successful_other_browsers(self):
        task_id = self.enqueue()
        def command(*args):
            self.fixture.fail_stage = 'extract_tls.py' if args[1][-2] == 'firefox' else None
            return self.execute(*args)
        with patch.object(self.manager, '_run_command', side_effect=command):
            self.manager._run_task(self.store.get_task(task_id))
        result = self.manager.extractions.get(task_id)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['progress']['failed'], 1)
        self.assertTrue((self.fixture.directory / 'capture_firefox.pcap').exists())
        self.fixture.fail_stage = None
        self.fixture.calls.clear()
        self.assertEqual(self.enqueue(), task_id)
        self.assertEqual(self.run_job(task_id)['status'], 'SUCCEEDED')
        self.assertEqual(self.fixture.calls.count('extract_tls.py'), 1)

    def test_missing_or_wrong_checkpoint_never_launches_analysis(self):
        for b in extraction.BROWSERS:
            p = self.fixture.directory / f'capture_{b}.complete.json'
            value = json.loads(p.read_text()); value['url'] = 'https://wrong.example/'
            p.write_text(json.dumps(value))
        task_id = self.enqueue()
        with patch.object(self.manager, '_run_command') as run:
            self.manager._run_task(self.store.get_task(task_id)); run.assert_not_called()
        self.assertEqual(self.manager.extractions.get(task_id)['status'], 'FAILED')
        self.assertTrue(all((self.fixture.directory / f'capture_{b}.pcap').exists() for b in extraction.BROWSERS))

    def test_auth_pagination_cancel_and_reject_capture_id_on_extraction_endpoint(self):
        self.assertEqual(self.client.get('/api/v1/extractions').status_code, 401)
        task_id = self.enqueue()
        response = self.client.get('/api/v1/extractions?limit=1&offset=0', headers=self.auth).get_json()
        self.assertEqual(response['page']['total'], 1)
        self.assertEqual(response['jobs'][0]['browsers'], list(extraction.BROWSERS))
        self.assertEqual(self.client.get('/api/v1/extractions?offset=x', headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/extractions/capture-source/cancel', headers=self.auth).status_code, 404)
        response = self.client.post(f'/api/v1/extractions/{task_id}/cancel', headers=self.auth)
        self.assertEqual(response.get_json()['status'], 'CANCELED')
        self.assertEqual(self.store.get_task_status('capture-source')['status'], 'SUCCEEDED')
