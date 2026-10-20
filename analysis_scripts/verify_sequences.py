import csv, json, subprocess, sys
from pathlib import Path
try:
    from .extract_forward_seqs import COUNT_FIELDS
    from .flow_validation import TCP_DISPLAY_FILTER
    from .result_layout import detail_directory
except ImportError:
    from extract_forward_seqs import COUNT_FIELDS
    from flow_validation import TCP_DISPLAY_FILTER
    from result_layout import detail_directory

def read_tsv(path):
    with path.open(encoding='utf-8') as f:
        return list(csv.DictReader((line for line in f if not line.startswith('#')), delimiter='\t'))

def verify(output, browser, pcap, modes=('decrypted', 'inferred')):
    seq = output / 'sequences'
    result = {'packet_files': {}, 'sequences': {}, 'packet_categories': {}}
    raw_output = subprocess.run(['tshark','-r',str(pcap),'-Y',TCP_DISPLAY_FILTER,
                                 '-T','fields','-e','frame.number','-e','tcp.len','-e','tcp.flags','-e','tcp.stream',
                                 '-e','ip.src','-e','ip.dst','-e','ipv6.src','-e','ipv6.dst',
                                 '-e','tcp.srcport','-e','tcp.dstport','-e','frame.len','-e','frame.time_relative'],
                                capture_output=True,text=True,check=True,timeout=300).stdout
    raw = {r[0]:(int(r[1]),int(r[2],16),r[3],r[4] or r[6],r[5] or r[7],r[8],r[9],int(r[10]),float(r[11]))
           for line in raw_output.splitlines() if len(r:=line.split('\t'))==12}
    categories = {16:'ack',2:'syn',18:'syn_ack',1:'fin',17:'fin_ack',4:'rst',20:'rst_ack'}
    for suffix in ('flows' if mode == 'decrypted' else 'inferred' for mode in modes):
        files = list(detail_directory(output, browser, suffix).glob('*.tsv'))
        assert files, 'No detailed packet files'
        result['packet_files'][suffix] = len(files)
        packets = [row for path in files for row in read_tsv(path)]
        assert len(packets)==len(raw)==len({r['frame_no'] for r in packets}), 'Packet coverage mismatch'
        for path in files:
            metadata = dict(line[1:].strip().split(':', 1) for line in path.read_text().splitlines()
                            if line.startswith('#') and ':' in line)
            metadata = {k.strip():v.strip() for k,v in metadata.items()}
            rows = read_tsv(path)
            assert all(raw[row['frame_no']][2] == metadata['tcp_stream'] for row in rows), 'Mixed TCP streams'
            assert all(raw[row['frame_no']][3:7] == tuple(metadata[k] for k in ('src_ip','dst_ip','src_port','dst_port'))
                       for row in rows), 'Mixed TCP endpoints'
            assert [int(row['frame_no']) for row in rows] == sorted(int(row['frame_no']) for row in rows)
        counts = {}
        for row in packets:
            length,flags,*_=raw[row['frame_no']]
            assert int(row['tcp_payload_len'])==length
            assert int(row['pkt_len'])==raw[row['frame_no']][7]
            assert abs(float(row['time_rel'])-raw[row['frame_no']][8]) <= 0.000001
            assert int(row['tcp_flags'],16)==flags
            labels=[name for bit,name in ((1,'FIN'),(2,'SYN'),(4,'RST'),(8,'PSH'),(16,'ACK'),
                                         (32,'URG'),(64,'ECE'),(128,'CWR')) if flags & bit]
            labels.extend(f'BIT_0x{bit:03X}' for bit in (256,512,1024,2048) if flags & bit)
            assert row['tcp_flags_text']==('|'.join(labels) or 'NONE')
            if length==0:
                assert row['category']==categories.get(flags,'tcp_other' if flags else 'unknown')
            counts[row['category']]=counts.get(row['category'],0)+1
        result['packet_categories'][suffix]=counts
        result['packet_rows_per_mode']=len(raw)
        result['tcp_flags_verified']=True
    for mode in modes:
        tables = {}
        for direction in ('forward', 'reverse', 'bidirectional'):
            rows = read_tsv(seq/mode/f'{direction}.tsv')
            tables[direction] = rows
            stats = {'rows':len(rows), 'nonempty':sum(bool(r['header_lens']) for r in rows),
                     'lengths':sum(len(r['header_lens'].split(',')) if r['header_lens'] else 0 for r in rows)}
            if mode == 'decrypted':
                stats['protocols'] = {p:sum(r['protocol']==p for r in rows) for p in ('HTTP/1.1','HTTP/2')}
                stats['forbes_lengths'] = sum(len(r['header_lens'].split(',')) if r['header_lens'] else 0 for r in rows if r['sni'].endswith('forbeschina.com'))
            result['sequences'][f'{mode}/{direction}'] = stats
        # Account for every direction and compare conservation of packet classes.
        single = {}
        for direction in ('forward','reverse'):
            for row in tables[direction]:
                parts = row['flow'].split('_')
                ports = (parts[1],parts[2]) if direction=='forward' else (parts[2],parts[1])
                single.setdefault((ports,tuple(parts[3:]),row.get('protocol','')),[]).append(row)
        for row in tables['bidirectional']:
            parts=row['flow'].split('_')
            source=single.pop(((parts[1],parts[2]),tuple(parts[3:]),row.get('protocol','')))
            for column in COUNT_FIELDS:
                assert int(row[column])==sum(int(s[column]) for s in source), (column,row['flow'])
            assert (len(row['header_lens'].split(',')) if row['header_lens'] else 0)==sum(len(s['header_lens'].split(',')) if s['header_lens'] else 0 for s in source)
        assert not single
    return result

if __name__ == '__main__':
    out, browser, pcap = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    result = verify(out, browser, pcap)
    (out/'verification.json').write_text(json.dumps(result, indent=2))
