import json
from pathlib import Path
import tempfile
import unittest
from analysis_scripts.result_layout import create_inputs, detail_directory, page_name


class LayoutTests(unittest.TestCase):
    def test_flat_creation_and_identity_all_browsers(self):
        with tempfile.TemporaryDirectory() as tmp:
            for browser in ('chrome','edge','firefox'):
                out=Path(tmp)/browser
                inputs=create_inputs(out,Path('012-wiki-test-name')/f'capture_{browser}.pcap')
                folder=inputs/f'capture_{browser}_inferred';folder.mkdir()
                self.assertEqual(page_name(folder),'012-wiki-test-name')
                self.assertEqual(detail_directory(out,browser,'inferred'),folder)
                with self.assertRaises(FileExistsError): create_inputs(out,Path('different')/'capture_chrome.pcap')

    def test_missing_identity_and_ambiguous_layout_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);folder=out/'inputs'/'capture_chrome_inferred';folder.mkdir(parents=True)
            with self.assertRaises(FileNotFoundError): page_name(folder)
            legacy=out/'inputs'/'001-wiki-test'/'capture_chrome_inferred';legacy.mkdir(parents=True)
            self.assertEqual(page_name(legacy),'001-wiki-test')
            with self.assertRaises(ValueError): detail_directory(out,'chrome','inferred')

    def test_multi_flow_and_legacy_forward_identity_preserved(self):
        from analysis_scripts.extract_multi_flow import process_dir as multi
        from extract_multi_flow import process_dir as root_multi
        from analysis_scripts.extract_forward_seqs import process_dir as forward
        from extract_forward_seqs import process_dir as root_forward
        from tests.test_header_sequence_extensions import write_flow
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);old=out/'012-wiki-example';old.mkdir()
            flow=old/'capture_firefox.tsv'
            flow.write_text('src_port\tdst_port\tbyte_count\tsni\n5000\t443\t42\texample.test\n')
            detail=old/'capture_firefox_inferred'
            write_flow(detail,'forward',[dict(frame_no='1',category='header',est_h2_payload_range='10-12')])
            expected_multi=multi(old,'firefox');expected_forward=forward(old,'firefox')
            inputs=create_inputs(out/'result',old/'capture_firefox.pcap')
            flow.rename(inputs/flow.name);detail.rename(inputs/detail.name)
            self.assertEqual(multi(inputs,'firefox'),expected_multi)
            self.assertEqual(root_multi(inputs,'firefox'),expected_multi)
            self.assertEqual(forward(inputs,'firefox'),expected_forward)
            self.assertEqual(root_forward(inputs,'firefox'),expected_forward)


if __name__=='__main__': unittest.main()
