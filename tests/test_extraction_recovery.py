"""Stream reuse, ICMP quoted headers and non-destructive capture recovery."""
import csv
import hashlib
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from analysis_scripts.flow_validation import split_connections, packet_key
from analysis_scripts.capture_recovery import prepare_capture


def make_capture(path):
    packets = []
    for index, (reverse, flags, seq, ack) in enumerate([
            (False,2,100,0),(True,18,200,101),(False,16,101,201),(True,20,201,101),
            (False,2,9000,0),(True,18,8000,9001),(False,16,9001,8001),(True,20,8001,9001)]):
        src, dst = (bytes((10,0,0,1)), bytes((10,0,0,2)))
        sp, dp = 5000,443
        if reverse: src,dst,sp,dp=dst,src,dp,sp
        tcp=struct.pack('!HHIIBBHHH',sp,dp,seq,ack,0x50,flags,65535,0,0)
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,40,index,0,64,6,0,src,dst)
        packets.append(bytes(12)+b'\x08\x00'+ip+tcp)
    quote=packets[0][14:]
    icmp=struct.pack('!BBHI',3,3,0,0)+quote
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(icmp),99,0,64,1,0,
                   bytes((10,0,0,2)),bytes((10,0,0,1)))
    packets.append(bytes(12)+b'\x08\x00'+ip+icmp)
    with path.open('wb') as f:
        f.write(struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,1))
        for i,p in enumerate(packets):
            f.write(struct.pack('<IIII',i+1,0,len(p),len(p))+p)


class RecoveryTests(unittest.TestCase):
    def test_both_ports_443_use_handshake_direction(self):
        packets = [dict(zip(('ip.src','ip.dst','tcp.srcport','tcp.dstport','tcp.stream'), values),
                        **{'tcp.flags': flags}) for values,flags in
                   [(('scanner','server','443','443','7'),'0x0002'),
                    (('server','scanner','443','443','7'),'0x0014')]]
        meta={packet_key(p)[:4]:dict(src_port='443',dst_port='443',direction='forward') for p in packets}
        flows,names=split_connections(meta,packets)
        self.assertEqual(set(names.values()),{'forward_443_443.tsv','reverse_443_443.tsv'})
        self.assertEqual(flows[packet_key(packets[0])]['direction'],'forward')
        self.assertEqual(flows[packet_key(packets[1])]['direction'],'reverse')
        packets[0]['tcp.flags']='0x0010'
        with self.assertRaisesRegex(ValueError,'Cannot identify client/server'):
            split_connections(meta,packets)

    def test_split_reused_endpoints_keeps_sni_separate(self):
        key=('a','b','5000','443')
        meta={key:dict(src_ip='a',dst_ip='b',src_port='5000',dst_port='443',direction='forward',sni='old')}
        packets=[{'ip.src':'a','ip.dst':'b','tcp.srcport':'5000','tcp.dstport':'443',
                  'tcp.stream':str(i),'tls.handshake.extensions_server_name':sni}
                 for i,sni in ((0,'first.test'),(1,'second.test'))]
        flows,names=split_connections(meta,packets)
        self.assertEqual(len(flows),2)
        for p in packets:
            self.assertEqual(flows[packet_key(p)]['sni'],p['tls.handshake.extensions_server_name'])
        self.assertEqual(set(names.values()),{'forward_5000_443_tcp_0.tsv','forward_5000_443_tcp_1.tsv'})

    @unittest.skipUnless(shutil.which('tshark') and shutil.which('editcap'),'Wireshark tools unavailable')
    def test_real_reused_connections_icmp_and_recovery_all_browsers(self):
        from analysis_scripts import extract_features as features, classify_packets as classify, infer_packets as infer
        from analysis_scripts import extract_header_seqs as seq
        from analysis_scripts.verify_sequences import verify
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); pcap=root/'input.pcap'; make_capture(pcap)
            keys=root/'keys.log'; keys.write_text('')
            for browser in ('chrome','edge','firefox'):
                out=root/browser; data=out/'inputs'/'1-wiki-test'; data.mkdir(parents=True)
                flow=data/f'capture_{browser}.tsv'
                features.process_pcap(pcap,flow,[])
                self.assertEqual(len(list(csv.DictReader(flow.open(),delimiter='\t'))),2)
                classify.process(pcap,keys,flow,data/f'capture_{browser}_flows')
                infer.process(pcap,flow,data/f'capture_{browser}_inferred')
                seq.extract(out/'inputs',browser,seq.MODES,seq.DIRECTIONS,out/'sequences')
                check=verify(out,browser,pcap)
                self.assertEqual(check['packet_rows_per_mode'],8)
                self.assertEqual(check['packet_files'],{'flows':4,'inferred':4})
                self.assertEqual(check['sequences']['inferred/bidirectional']['rows'],2)
                # Moving only the input layer must preserve every sequence byte,
                # including the page id/name, for all three browser labels.
                from analysis_scripts.result_layout import page_metadata
                import json
                before_sequences = {str(p.relative_to(out/'sequences')):p.read_bytes()
                                    for p in (out/'sequences').rglob('*.tsv')}
                for child in data.iterdir():
                    child.rename(data.parent/child.name)
                data.rmdir()
                (out/'inputs'/'page.json').write_text(json.dumps(page_metadata(Path('1-wiki-test')/f'capture_{browser}.pcap')))
                seq.extract(out/'inputs',browser,seq.MODES,seq.DIRECTIONS,out/'sequences')
                self.assertEqual(before_sequences, {str(p.relative_to(out/'sequences')):p.read_bytes()
                                                    for p in (out/'sequences').rglob('*.tsv')})
                self.assertEqual(check, verify(out,browser,pcap))
            truncated=root/'truncated.pcap'; truncated.write_bytes(pcap.read_bytes()[:-7])
            before=(hashlib.sha256(truncated.read_bytes()).hexdigest(),truncated.stat().st_mtime_ns)
            repaired, info=prepare_capture(truncated,root/'recovered.pcapng')
            self.assertTrue(info['partial']); self.assertEqual(info['recovered_frames'],8)
            self.assertEqual(before,(hashlib.sha256(truncated.read_bytes()).hexdigest(),truncated.stat().st_mtime_ns))
            with self.assertRaisesRegex(ValueError,'distinct'):
                prepare_capture(truncated,truncated)
            with self.assertRaisesRegex(ValueError,'distinct'):
                prepare_capture(truncated,repaired)


if __name__=='__main__': unittest.main()
