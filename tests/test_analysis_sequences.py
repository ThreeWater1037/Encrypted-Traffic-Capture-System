"""Offline sequence contracts, shared by Chrome, Edge and Firefox captures."""
import csv
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from analysis_scripts import flow_sequences as fs
from analysis_scripts import extract_features as features
from analysis_scripts import infer_packets, batch_process


def packet(frame=1, stream='0', reverse=False, **fields):
    p = dict.fromkeys(fs.FIELDS, '')
    p.update({'frame.number': str(frame), 'frame.time_epoch': str(100 + frame / 10),
              'frame.len': '80', 'ip.src': '10.0.0.1', 'ip.dst': '10.0.0.2',
              'tcp.srcport': '5555', 'tcp.dstport': '8443', 'tcp.stream': stream,
              'tcp.len': '0', 'tcp.flags.syn': '0', 'tcp.flags.ack': '1'})
    if reverse:
        p['ip.src'], p['ip.dst'] = p['ip.dst'], p['ip.src']
        p['tcp.srcport'], p['tcp.dstport'] = p['tcp.dstport'], p['tcp.srcport']
    p.update(fields)
    return p


def connection(stream='0'):
    return [packet(1, stream, **{'tcp.flags.syn': '1', 'tcp.flags.ack': '0'}),
            packet(2, stream, reverse=True, **{'tcp.flags.syn': '1'}),
            packet(3, stream, **{'tcp.len': '100', 'tls.handshake.type': '1',
                   'tls.handshake.extensions_server_name': 'www.forbeschina.com',
                   'tls.handshake.extensions_alpn_str': 'h2,http/1.1'}),
            packet(4, stream, reverse=True, **{'tcp.len': '100', 'tls.handshake.type': '2',
                   'tls.handshake.extensions_alpn_str': 'http/1.1', 'tls.handshake.version': '0x0303'})]


class SequenceTests(unittest.TestCase):
    def test_client_offer_not_negotiation(self):
        p = connection()[:3]
        self.assertEqual(fs.connection_meta(p)['app_protocol'], 'unknown')
        self.assertEqual(fs.connection_meta(connection())['app_protocol'], 'h1')

    def test_roles_nonstandard_ports_and_reverse_first(self):
        raw = connection()
        meta = fs.connection_meta(raw[1:])
        self.assertEqual((meta['client_port'], meta['server_port']), ('5555', '8443'))
        self.assertEqual(meta['direction_basis'], 'tcp_syn')

    def test_ipv6_clienthello_fallback(self):
        p = packet(**{'ip.src': '', 'ip.dst': '', 'ipv6.src': '::1', 'ipv6.dst': '::2',
                      'tls.handshake.type': '1'})
        meta = fs.connection_meta([p])
        self.assertEqual(meta['client_ip'], '::1')
        self.assertEqual(meta['direction_basis'], 'tls_client_hello')

    def test_h1_never_h2_inference(self):
        meta = fs.connection_meta(connection())
        p = packet(5, **{'tcp.len': '99', 'tls.record.content_type': '23', 'tls.record.length': '76'})
        row = fs.packet_row(p, meta, 100, 'inferred', infer=infer_packets.infer_packet)
        self.assertEqual(row['inferred_h2_type'], '-')
        self.assertEqual(row['est_plaintext_len'], '-')
        self.assertEqual(row['category'], 'encrypted_application')

    def test_heuristics_explicit_and_tls12_blocked(self):
        meta = fs.connection_meta(connection())
        meta.update(app_protocol='h2', tls_version='TLSv1.3', cipher_suite='0x1301')
        p = packet(5, **{'tcp.len': '99', 'tls.record.content_type': '23', 'tls.record.length': '76'})
        self.assertEqual(fs.packet_row(p, meta, 100, 'inferred')['inferred_h2_type'], '-')
        row = fs.packet_row(p, meta, 100, 'inferred', infer_packets.infer_packet)
        self.assertEqual(row['inferred_h2_type'], 'HEADERS')
        self.assertTrue(row['inference_basis'].startswith('heuristic_'))
        meta['tls_version'] = 'TLSv1.2'
        self.assertEqual(fs.packet_row(p, meta, 100, 'inferred', infer_packets.infer_packet)['est_plaintext_len'], '-')

    def test_syn_not_ack_http1_not_header_length(self):
        raw = connection()
        meta = fs.connection_meta(raw)
        self.assertEqual(fs.packet_row(raw[0], meta, 100, 'decrypted')['category'], 'tcp_control')
        p = packet(5, **{'tcp.len': '45', 'http.request.version': 'HTTP/1.1', 'http.request.method': 'GET'})
        row = fs.packet_row(p, meta, 100, 'decrypted')
        self.assertEqual(row['category'], 'http1_message')
        self.assertEqual(row['h2_frame_len'], '-')

    def test_h2_observed_multiframe(self):
        p = packet(5, **{'tcp.len': '100', 'http2.type': '1,0', 'http2.length': '20,30'})
        raw = connection() + [p]
        meta = fs.connection_meta(raw)
        row = fs.packet_row(p, meta, 100, 'decrypted')
        self.assertEqual(meta['protocol_evidence'], 'dissected_http2')
        self.assertEqual(row['h2_frame_type'], 'HEADERS,DATA')
        self.assertEqual(row['h2_frame_len'], '20,30')

    def test_reused_tuple_distinct_connections(self):
        flows = features.extract_flows(connection('0') + connection('1'))
        filtered = features.filter_by_sni(flows, ['forbeschina.com'])
        self.assertEqual(len(filtered), 4)
        self.assertEqual({f.stream_id for f in filtered}, {'0', '1'})

    def test_tshark_failure_propagates_and_no_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            pcap = Path(temp) / 'sample.pcap'
            pcap.write_bytes(b'pcap')
            with patch.object(fs.subprocess, 'run', return_value=Mock(returncode=2, stderr='bad capture')) as run:
                with self.assertRaisesRegex(RuntimeError, 'bad capture'):
                    fs.read_packets(pcap)
                cmd = run.call_args.args[0]
                self.assertIn('tls.keylog_file:', cmd)
                self.assertIn('WIRESHARK_CONFIG_DIR', run.call_args.kwargs['env'])
                self.assertNotIn('SSLKEYLOGFILE', run.call_args.kwargs['env'])

    def test_embedded_secrets_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'secrets.pcapng'
            p.write_bytes(struct.pack('<IIII', 0x0a0d0d0a, 16, 0x1a2b3c4d, 16) + struct.pack('<III', 10, 12, 12))
            with self.assertRaisesRegex(ValueError, 'embedded secrets'):
                fs.reject_embedded_secrets(p)

    def test_all_browser_labels_both_modes_merge_and_hash_skip(self):
        for browser in ('chrome', 'edge', 'firefox'):
            with self.subTest(browser=browser), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pcap = root / f'capture_{browser}.pcap'
                pcap.write_bytes(b'pcap fixture')
                (root / f'tls_keys_{browser}.log').write_text('fixture key')
                out = root / 'out'
                with patch.object(fs, 'read_packets', return_value=connection()):
                    result = batch_process.process_pcap(pcap, out, {'extract', 'classify', 'infer', 'sequences'})
                self.assertTrue(result['ok'], result['failed'])
                for mode in ('decrypted', 'inferred'):
                    folder = out / 'sequences' / mode
                    with (folder / 'bidirectional.tsv').open() as fh:
                        row = next(csv.DictReader(fh, delimiter='\t'))
                    self.assertEqual(list(row), fs.SUMMARY_FIELDS)
                    self.assertEqual(row['flow'], 'bidirectional_5555_8443')
                    self.assertEqual(sum(int(row[k]) for k in fs.SUMMARY_COUNTS), 4)
                    self.assertEqual(row['header_lens'], '')
                skipped = batch_process.process_pcap(pcap, out, {'extract', 'classify', 'infer', 'sequences'}, True)
                self.assertTrue(skipped['skipped'])
                (out / 'sequences' / 'decrypted' / 'forward.tsv').write_text('truncated')
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    batch_process.process_pcap(pcap, out, {'extract', 'classify', 'infer', 'sequences'}, True)

    def test_missing_key_fails_but_ciphertext_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / 'capture_firefox.pcap'
            pcap.write_bytes(b'pcap')
            with patch.object(fs, 'read_packets', return_value=connection()):
                result = batch_process.process_pcap(pcap, root / 'out', {'extract', 'classify', 'infer', 'sequences'})
            self.assertFalse(result['ok'])
            self.assertTrue((root / 'out/sequences/inferred/bidirectional.tsv').is_file())
            self.assertFalse((root / 'out/complete.json').exists())

    def test_missing_direction_and_duplicate_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flow = root / 'flows'
            flow.mkdir()
            meta = fs.connection_meta(connection())
            row = fs.packet_row(connection()[0], meta, 100, 'decrypted')
            fs.write_flow(flow / 'forward_tcp_0.tsv', {**meta, 'direction': 'forward'}, [row])
            with self.assertRaisesRegex(ValueError, 'Missing direction'):
                fs.summarize(flow, root / 'missing')
            fs.write_flow(flow / 'reverse_tcp_0.tsv', {**meta, 'direction': 'reverse'}, [{**row, 'direction': 'reverse'}])
            with self.assertRaisesRegex(ValueError, 'Duplicate frame'):
                fs.summarize(flow, root / 'duplicate')

    def test_staged_entry_preserves_extract_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / 'capture_edge.pcap'
            pcap.write_bytes(b'pcap')
            (root / 'tls_keys_edge.log').write_text('key')
            out = root / 'out'
            with patch.object(fs, 'read_packets', return_value=connection()):
                self.assertTrue(batch_process.process_pcap(pcap, out, {'extract'})['ok'])
                before = (out / 'capture_edge.tsv').read_bytes()
                result = batch_process.process_pcap(pcap, out, {'classify', 'infer', 'sequences'})
            self.assertTrue(result['ok'])
            self.assertEqual(before, (out / 'capture_edge.tsv').read_bytes())

    def test_unidentified_direction_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / 'capture_chrome.pcap'
            pcap.write_bytes(b'pcap')
            with patch.object(fs, 'read_packets', return_value=[packet()]):
                result = batch_process.process_pcap(pcap, root / 'out', {'extract', 'infer', 'sequences'})
            self.assertFalse(result['ok'])
            self.assertIn('Cannot identify client/server', result['failed'][0])

    def test_legacy_fields_and_bidirectional_header_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flow = root / 'flows'
            flow.mkdir()
            meta = fs.connection_meta(connection())
            # Interleaved frame numbers, including a zero-length reverse header.
            rows = []
            for frame, direction, size in [(1, 'forward', 10), (2, 'reverse', 0), (3, 'forward', 30), (4, 'reverse', 40)]:
                row = fs.packet_row(packet(frame), meta, 100, 'inferred')
                row.update(direction=direction, category='header', est_h2_payload_range=f'{size}-{size}')
                rows.append(row)
            for direction in ('forward', 'reverse'):
                fs.write_flow(flow / f'{direction}_tcp_0.tsv', {**meta, 'direction': direction},
                              [r for r in rows if r['direction'] == direction])
            fs.summarize(flow, root / 'summary', '001-wiki-example')
            for direction, expected in [('forward', '10,30'), ('reverse', '0,40'), ('bidirectional', '10,-0,30,-40')]:
                with (root / 'summary' / f'{direction}.tsv').open() as fh:
                    reader = csv.DictReader(fh, delimiter='\t')
                    self.assertEqual(reader.fieldnames, ['id','wiki_name','flow','sni','handshake','control','header','data','ack','tls_segment','unknown','header_lens'])
                    row = next(reader)
                self.assertEqual(row['id'], '001')
                self.assertEqual(row['wiki_name'], 'example')
                self.assertEqual(row['header_lens'], expected)

    def test_legacy_estimate_semantics_and_decrypted_header(self):
        from analysis_scripts.extract_forward_seqs import process_flow_file
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = fs.connection_meta(connection())
            row = fs.packet_row(packet(), meta, 100, 'inferred')
            row.update(category='header', est_h2_payload_range='61-80')
            path = root / 'forward_tcp_0.tsv'
            fs.write_flow(path, {**meta, 'direction': 'forward'}, [row])
            legacy = process_flow_file(path)
            current = fs.summary_row(meta, [row], 'forward', '001-wiki-example')
            for field in ['sni'] + fs.SUMMARY_COUNTS + ['header_lens']:
                self.assertEqual(current[field], legacy[field])
            row.update(est_h2_payload_range='-', h2_frame_type='SETTINGS,HEADERS', h2_frame_len='6,42')
            self.assertEqual(fs.summary_row(meta, [row], 'forward', 'x')['header_lens'], '42')
            row.update(category='http1_message')
            summary = fs.summary_row(meta, [row], 'forward', 'x')
            self.assertEqual(summary['unknown'], 1)
            self.assertEqual(summary['header_lens'], '')

    def test_original_packet_columns_names_and_comment_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = fs.connection_meta(connection())
            row = fs.packet_row(packet(), meta, 100, 'decrypted')
            expected = {
                'decrypted': ['frame_no','time_rel','pkt_len','tcp_payload_len','tls_content_type','h2_frame_type','h2_frame_len','category'],
                'inferred': ['frame_no','time_rel','pkt_len','tcp_payload_len','tls_content_type','tls_record_len','est_plaintext_len','inferred_h2_type','est_h2_payload_range','category'],
            }
            for mode in expected:
                folder = root / mode
                folder.mkdir()
                for direction, filename in [('forward','forward_5555_8443.tsv'), ('reverse','reverse_8443_5555.tsv')]:
                    self.assertEqual(fs.flow_name(meta, direction) + '.tsv', filename)
                    path = folder / filename
                    fs.write_flow(path, {**meta, 'direction': direction}, [{**row, 'direction': direction}], mode)
                    comments, data = [], []
                    for line in path.read_text().splitlines():
                        if line.startswith('#') and ':' in line:
                            comments.append(line[1:].split(':',1)[0].strip())
                        elif not line.startswith('#'):
                            data.append(line)
                    expected_keys = ['src_ip','dst_ip','src_port','dst_port','transport','tls_version']
                    if mode == 'inferred':
                        expected_keys.append('tls_overhead')
                    expected_keys += ['app_protocol','sni','direction','pkt_count']
                    self.assertEqual(comments, expected_keys)
                    self.assertEqual(next(csv.reader(data, delimiter='\t')), expected[mode])
                    read_meta, rows = fs.read_flow(path)
                    self.assertEqual(read_meta['client_port'], '5555')
                    self.assertEqual(rows[0]['direction'], direction)

    def test_original_filename_collision_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / 'capture_chrome.pcap'
            pcap.write_bytes(b'pcap')
            out = root / 'out'
            with patch.object(fs, 'read_packets', return_value=connection('0') + connection('1')):
                result = batch_process.process_pcap(pcap, out, {'extract', 'infer', 'sequences'})
            self.assertFalse(result['ok'])
            self.assertIn('Legacy filename collision: forward_5555_8443.tsv', result['failed'][0])
            self.assertFalse((out / 'capture_chrome_inferred').exists())


if __name__ == '__main__':
    unittest.main()
