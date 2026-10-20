"""Deletion, restart and three-browser contracts for the background pipeline."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import capture_extraction as extraction
from wiki_fetcher import UrlEntry, WikiFetcher


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = UrlEntry("1", "fixture", "https://example.org/")
        self.directory = self.root / "1-wiki-fixture"
        self.directory.mkdir()
        self.calls = []
        self.fail_stage = None
        self.corrupt_source = False

    def capture(self, browser):
        for name, data in [(f"capture_{browser}.pcap", b"original packet bytes"),
                           (f"tls_keys_{browser}.log", b"secret")]:
            (self.directory / name).write_bytes(data)
        extraction.save_json(self.directory / f"capture_{browser}.complete.json", {
            "item_id": "1", "url": self.entry.url, "browser": browser,
            "artifacts": {f"capture_{browser}.pcap": 21, f"tls_keys_{browser}.log": 6}})

    def stage(self, command, **kwargs):
        self.assertEqual(kwargs["env"]["PYTHONOPTIMIZE"], "0")
        stage = Path(command[1]).name
        self.calls.append(stage)
        if stage == self.fail_stage:
            raise subprocess.CalledProcessError(1, command)
        if stage == "verify_sequences.py":
            output = Path(command[2])
            for mode in ("decrypted", "inferred"):
                directory = output / "sequences" / mode
                directory.mkdir(parents=True)
                for direction in ("forward", "reverse", "bidirectional"):
                    (directory / f"{direction}.tsv").write_text("header\n1\n")
            extraction.save_json(output / "verification.json", {"tcp_flags_verified": True, "packet_rows_per_mode": 1})
        if stage == "extract_tls.py":
            output = Path(command[-1])
            output.parent.mkdir()
            output.write_text('{"flow": 1}\n')
            extraction.save_json(output.with_suffix(".manifest.json"), {"complete": True, "stats": {"output_flows": 1}})
            if self.corrupt_source:
                Path(command[2]).write_bytes(b"changed")
        return subprocess.CompletedProcess(command, 0)

    def run_extract(self, browser="chrome", delete=True, steps=None):
        with patch.object(extraction, "storage_guard"), patch.object(extraction.subprocess, "run", side_effect=self.stage):
            return extraction.extract_one(self.directory, browser, delete, steps=steps)

    def test_tls_only_without_keys_for_all_browsers(self):
        for browser in extraction.BROWSERS:
            with self.subTest(browser=browser):
                self.capture(browser)
                (self.directory / f'tls_keys_{browser}.log').unlink()
                self.calls.clear()
                receipt = self.run_extract(browser, steps=['tls'])
                self.assertEqual(self.calls, ['extract_tls.py'])
                self.assertTrue(receipt['tls_verified'])
                self.assertFalse(receipt['flow_verified'])
                self.assertTrue(receipt['pcap_deleted'])
                self.assertIsNone(extraction.verified_receipt(self.directory, browser, required_steps=['flow']))
                self.assertTrue(extraction.retired_capture(self.directory, browser))

    def test_flow_only_skips_tls_and_can_delete_all_browsers(self):
        for browser in extraction.BROWSERS:
            with self.subTest(browser=browser):
                self.capture(browser)
                self.calls.clear()
                receipt = self.run_extract(browser, steps=['flow'])
                self.assertNotIn('extract_tls.py', self.calls)
                self.assertEqual(self.calls[-1], 'verify_sequences.py')
                self.assertTrue(receipt['flow_verified'])
                self.assertFalse(receipt['tls_verified'])
                self.assertTrue(receipt['pcap_deleted'])
                self.assertIsNone(extraction.verified_receipt(self.directory, browser, required_steps=['tls']))

    def test_separate_runs_merge_outputs_and_reuse_completed_steps(self):
        for browser in extraction.BROWSERS:
            with self.subTest(browser=browser):
                self.capture(browser)
                first = self.run_extract(browser, delete=False, steps=['flow'])
                self.calls.clear()
                second = self.run_extract(browser, delete=False, steps=['tls'])
                self.assertEqual(self.calls, ['extract_tls.py'])
                self.assertTrue(set(first['outputs']).issubset(second['outputs']))
                self.assertNotEqual(second['result_dirs']['flow'], second['result_dirs']['tls'])
                self.calls.clear()
                both = self.run_extract(browser)
                self.assertEqual(self.calls, [])
                self.assertTrue(both['pcap_deleted'])
                self.assertTrue(extraction.verified_receipt(self.directory, browser, full=True, required_steps=['flow', 'tls']))

    def test_failed_addition_preserves_previous_success_and_pcap(self):
        self.capture('firefox')
        first = self.run_extract('firefox', delete=False, steps=['tls'])
        self.fail_stage = 'verify_sequences.py'
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_extract('firefox', steps=['flow'])
        self.assertEqual(extraction.verified_receipt(self.directory, 'firefox', full=True)['outputs'], first['outputs'])
        self.assertTrue((self.directory / 'capture_firefox.pcap').exists())
        self.assertIsNone(extraction.verified_receipt(self.directory, 'firefox', required_steps=['flow']))
        self.fail_stage = None
        self.calls.clear()
        self.run_extract('firefox')
        self.assertNotIn('extract_tls.py', self.calls)

    def test_deleted_single_step_cannot_satisfy_unselected_step(self):
        self.capture('edge')
        self.run_extract('edge', steps=['tls'])
        with self.assertRaises(FileNotFoundError):
            self.run_extract('edge', steps=['flow'])
        self.assertTrue(extraction.verified_receipt(self.directory, 'edge', required_steps=['tls']))
        self.assertIsNone(extraction.verified_receipt(self.directory, 'edge', required_steps=['flow']))

    def test_legacy_receipts_remain_valid(self):
        self.capture('chrome')
        receipt = self.run_extract(delete=False)
        receipt['version'] = 1
        receipt.pop('steps')
        receipt.pop('result_dirs')
        extraction.save_json(extraction.receipt_path(self.directory, 'chrome'), receipt)
        self.calls.clear()
        self.run_extract(steps=['tls'])
        self.assertEqual(self.calls, [])
        self.assertTrue(extraction.retired_capture(self.directory, 'chrome'))

    def test_invalid_steps_never_start_analysis(self):
        self.capture('chrome')
        for steps in ([], ['bad'], ['flow', 'flow'], 'tls', [False], [{}]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                self.run_extract(steps=steps)
        self.assertEqual(self.calls, [])

    def test_all_browsers_delete_only_after_both_stages_and_resume_without_recapture(self):
        for browser in extraction.BROWSERS:
            with self.subTest(browser=browser):
                self.capture(browser)
                self.calls.clear()
                receipt = self.run_extract(browser)
                self.assertTrue(receipt["pcap_deleted"])
                self.assertTrue((self.directory / f"tls_keys_{browser}.log").exists())
                self.assertEqual(self.calls[-2:], ["verify_sequences.py", "extract_tls.py"])
                self.assertTrue(extraction.verified_receipt(self.directory, browser, full=True))
                fetcher = WikiFetcher(self.root, [browser], True)
                self.assertTrue(fetcher._checkpoint_valid(self.entry, self.directory, browser))
                self.calls.clear()
                self.run_extract(browser)
                self.assertEqual(self.calls, [])

    def test_failure_in_either_pipeline_preserves_original_and_key(self):
        for stage in ("extract_features.py", "verify_sequences.py", "extract_tls.py"):
            with self.subTest(stage=stage):
                self.capture("chrome")
                self.fail_stage = stage
                before = extraction.signature(self.directory / "capture_chrome.pcap")
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_extract()
                self.assertEqual(extraction.signature(self.directory / "capture_chrome.pcap"), before)
                self.assertFalse(extraction.retired_capture(self.directory, "chrome"))

    def test_changed_input_is_never_deleted(self):
        self.capture("chrome")
        self.corrupt_source = True
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.run_extract()
        self.assertTrue((self.directory / "capture_chrome.pcap").exists())

    def test_delete_requires_explicit_flag(self):
        self.capture("chrome")
        self.run_extract(delete=False)
        self.assertTrue((self.directory / "capture_chrome.pcap").exists())
        self.assertFalse(extraction.retired_capture(self.directory, "chrome"))

    def test_corrupted_results_block_deletion_even_with_matching_size(self):
        self.capture("chrome")
        receipt = self.run_extract(delete=False)
        result = self.directory / next(p for p in receipt["outputs"] if p.endswith(".tsv"))
        result.write_bytes(b"x" * result.stat().st_size)
        receipt["delete_requested"] = True
        extraction.save_json(extraction.receipt_path(self.directory, "chrome"), receipt)
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            extraction.retire_pcap(self.directory, "chrome", receipt)
        self.assertTrue((self.directory / "capture_chrome.pcap").exists())

    def test_crash_between_verified_receipt_and_delete_recovers(self):
        self.capture("firefox")
        with patch.object(extraction, "retire_pcap", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_extract("firefox")
        self.assertTrue((self.directory / "capture_firefox.pcap").exists())
        self.calls.clear()
        self.run_extract("firefox")
        self.assertEqual(self.calls, [])
        self.assertTrue(extraction.retired_capture(self.directory, "firefox"))

    def test_keep_overrides_pending_deletion_for_all_browsers(self):
        for browser in extraction.BROWSERS:
            with self.subTest(browser=browser):
                self.capture(browser)
                with patch.object(extraction, 'retire_pcap', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_extract(browser)
                self.calls.clear()
                receipt = self.run_extract(browser, delete=False)
                self.assertFalse(receipt['delete_requested'])
                self.assertFalse(receipt['pcap_deleted'])
                self.assertTrue((self.directory / f'capture_{browser}.pcap').exists())
                self.assertEqual(self.calls, [])

    def test_missing_results_do_not_validate_deleted_capture(self):
        self.capture("edge")
        receipt = self.run_extract("edge")
        (self.directory / next(iter(receipt["outputs"]))).unlink()
        self.assertFalse(extraction.retired_capture(self.directory, "edge"))

    def test_storage_failure_preserves_source(self):
        self.capture("chrome")
        with patch.object(extraction, "storage_guard", side_effect=RuntimeError("low space")):
            with self.assertRaisesRegex(RuntimeError, "low space"):
                extraction.extract_one(self.directory, "chrome", True)
        self.assertTrue((self.directory / "capture_chrome.pcap").exists())

    def test_reported_packet_loss_preserves_source(self):
        self.capture("edge")
        marker = self.directory / "capture_edge.complete.json"
        data = json.loads(marker.read_text())
        data["capture_summary"] = {"packets_dropped": 4}
        extraction.save_json(marker, data)
        with self.assertRaisesRegex(RuntimeError, "packet loss"):
            self.run_extract("edge")
        self.assertTrue((self.directory / "capture_edge.pcap").exists())

    def test_recapture_archives_unextracted_pcaps_for_every_browser(self):
        fetcher = WikiFetcher(self.root, list(extraction.BROWSERS), True, extract_background=True)
        for browser in extraction.BROWSERS:
            self.capture(browser)
            before = extraction.signature(self.directory / f"capture_{browser}.pcap")
            fetcher._clear_incomplete_artifacts(self.directory, browser)
            archived = list((self.directory / "failed_captures" / browser).glob(f"*/capture_{browser}.pcap"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(extraction.signature(archived[0]), before)
            self.assertTrue((archived[0].parent / f"tls_keys_{browser}.log").exists())

    def test_queue_enqueues_new_and_reused_captures_for_all_browsers(self):
        from unittest.mock import MagicMock
        fetcher = WikiFetcher(self.root, list(extraction.BROWSERS), True)
        for browser in extraction.BROWSERS:
            self.capture(browser)
        fetcher.extraction_queue = MagicMock()
        result = fetcher._run_entry(self.entry, 1, 1)
        self.assertEqual(result, (0, 3, 0))
        self.assertEqual([c.args[1] for c in fetcher.extraction_queue.enqueue.call_args_list], list(extraction.BROWSERS))

    def test_queue_cannot_start_a_duplicate_source(self):
        self.capture("chrome")
        with extraction.unit_lock(self.directory, "chrome"):
            with self.assertRaises(OSError):
                self.run_extract()
        self.assertTrue((self.directory / "capture_chrome.pcap").exists())

    def test_queue_failure_does_not_block_later_units(self):
        from unittest.mock import MagicMock
        for browser in extraction.BROWSERS:
            self.capture(browser)
        child = MagicMock()
        child.poll.return_value = 1
        child.returncode = 1
        with patch.object(extraction.subprocess, "Popen", return_value=child), \
             patch("process_ownership.LINUX", False):
            queue = extraction.ExtractionQueue(self.root)
            for browser in extraction.BROWSERS:
                queue.enqueue(self.directory, browser)
                queue.enqueue(self.directory, browser)
            self.assertFalse(queue.close())
        self.assertEqual(queue.finished, 3)
        self.assertEqual(len(queue.failures), 3)

    def test_delete_configuration_requires_full_pipeline(self):
        from worker_agent.schema import validate_task_payload, ValidationError
        from worker_agent.config import WorkerConfig
        import sys
        config = WorkerConfig(worker_id="test", host="127.0.0.1", port=5100, token="test",
                              project_root=extraction.ROOT, python_executable=Path(sys.executable), data_dir=self.root)
        request = {"task_id": "test", "items": [vars(self.entry)], "browsers": list(extraction.BROWSERS),
                   "pcap": True, "analysis": {"pipeline": True, "delete_pcap": True}}
        normalized = validate_task_payload(request, config)
        self.assertTrue(normalized["analysis"]["delete_pcap"])
        for analysis in ({"delete_pcap": True}, {"pipeline": "true"},
                         {"pipeline": True, "steps": ["infer"]},
                         {"pipeline": True, "sni_suffixes": ["example.org"]}):
            with self.subTest(analysis=analysis), self.assertRaises(ValidationError):
                validate_task_payload({**request, "analysis": analysis}, config)

    def test_worker_manifest_and_progress_accept_verified_retirement(self):
        from worker_agent.config import WorkerConfig
        from worker_agent.task_runner import TaskManager
        from unittest.mock import MagicMock
        # Match the real Worker directory hierarchy.
        task = self.root / "tasks" / "pipeline-test"
        output = task / "fetch_output"
        output.mkdir(parents=True)
        self.directory.rename(output / self.directory.name)
        self.directory = output / self.directory.name
        request = {"task_id": "pipeline-test", "items": [vars(self.entry)],
                   "browsers": list(extraction.BROWSERS), "pcap": True,
                   "analysis": {"steps": [], "pipeline": True, "delete_pcap": True}}
        for browser in extraction.BROWSERS:
            self.capture(browser)
            self.run_extract(browser)
        config = WorkerConfig(worker_id="fixture", host="127.0.0.1", port=5100, token="test",
                              project_root=extraction.ROOT, python_executable=Path(__import__('sys').executable), data_dir=self.root)
        manager = TaskManager(config, MagicMock(), autostart=False)
        self.addCleanup(manager.shutdown)
        manifest = manager._build_manifest(task, request, [])
        self.assertEqual(manifest["status"], "SUCCEEDED")
        self.assertEqual(manifest["summary"]["succeeded"], 3)
        for unit in manifest["units"]:
            self.assertTrue(unit["checks"]["pcap_retired"])
            self.assertIsNone(unit["artifacts"]["pcap"])
            self.assertIsNotNone(unit["artifacts"]["tls_features"])
        extraction.save_json(output / "capture_progress.json", {
            "run_id": "test", "last_processed_position": 1, "total_urls": 1})
        manager.store.get_task_request.return_value = request
        manager.store.get_task.return_value = {"request": request}
        manager.store.get_task_status.return_value = {"created_at": "test"}
        progress = manager.capture_progress("pipeline-test")
        self.assertEqual(len(progress["units"]), 3)
        self.assertTrue(all(u["checks"]["pcap_retired"] for u in progress["units"]))


if __name__ == "__main__":
    unittest.main()
