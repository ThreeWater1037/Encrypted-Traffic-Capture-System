"""Retry only failed batch entries, retaining original inputs and old outputs.

Run on the extraction host: python -m analysis_scripts.retry_failed OLD_RUN NEW_RUN
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from .capture_recovery import prepare_capture
from .flow_validation import TCP_DISPLAY_FILTER
from .verify_sequences import verify
from .result_layout import create_inputs

CODE = Path(__file__).resolve().parent
STOP = threading.Event()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def signature(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(4 * 1024**2), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Input changed while hashing: ' + str(path))
    return dict(bytes=after.st_size, mtime_ns=after.st_mtime_ns, sha256=digest.hexdigest())


def check_space(path):
    if shutil.disk_usage(path).free < 8 * 1024**3 or os.statvfs(path).f_favail < 100000:
        STOP.set()
        raise RuntimeError('LOW_SPACE: reserve is 8 GiB and 100000 inodes')
    if STOP.is_set():
        raise RuntimeError('Retry stopped')


def command(args, log):
    with log.open('w') as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 900
        try:
            while process.poll() is None:
                check_space(log.parent)
                if time.monotonic() > deadline:
                    raise TimeoutError('Stage timed out after 900 seconds')
                time.sleep(0.2)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if process.returncode:
            raise RuntimeError('Stage failed: ' + '\n'.join(log.read_text(errors='replace').splitlines()[-15:]))


def retry(entry, stamp):
    pcap, key = Path(entry['pcap']), Path(entry['keylog'])
    result = dict(pcap=str(pcap), keylog=str(key), browser=entry['browser'],
                  previous_error=entry.get('error'), status='failed', ok=False, started=time.time())
    originals = [pcap] + ([key] if key.is_file() else [])
    output = None
    try:
        result['before'] = {str(path): signature(path) for path in originals}
        for path, expected in entry.get('before', {}).items():
            if result['before'].get(path) != expected:
                raise RuntimeError('Input differs from the original batch: ' + path)
        base = pcap.parent / stamp / entry['browser']
        base.mkdir(parents=True, exist_ok=True)
        check_space(base)
        attempt = 1
        while (base / f'attempt_{attempt}').exists():
            attempt += 1
        output = base / f'attempt_{attempt}'
        output.mkdir()
        result['output'] = str(output)
        logs = output / 'logs'; logs.mkdir()
        analysis_input, recovery = prepare_capture(pcap, output/'recovered_input.pcapng')
        result['capture_recovery'] = recovery
        result['analysis_input'] = str(analysis_input)
        raw = subprocess.run(['tshark','-r',str(analysis_input),'-Y',TCP_DISPLAY_FILTER,
                              '-T','fields','-e','frame.number'],capture_output=True,text=True,check=True,timeout=300)
        result['eligible_tcp_packets'] = len(raw.stdout.splitlines())
        excluded = subprocess.run(['tshark','-r',str(analysis_input),'-Y','tcp.port == 443 && (icmp || icmpv6)',
                                   '-T','fields','-e','frame.number'],capture_output=True,text=True,check=True,timeout=300)
        result['excluded_icmp_quoted_tcp_frames'] = excluded.stdout.splitlines()
        if not result['eligible_tcp_packets']:
            result['status'] = 'no_eligible_packets'
            result['limitation'] = 'No real TCP/443 packets in readable input; no sequence can be reconstructed'
        else:
            browser = entry['browser']
            usable_key = key.is_file() and key.stat().st_size > 0
            modes = ('decrypted','inferred') if usable_key else ('inferred',)
            result['modes'] = list(modes)
            result['limitations'] = []
            if not usable_key:
                result['limitations'].append('Missing or empty TLS keylog; decrypted mode unavailable')
            if recovery['partial']:
                result['limitations'].append('Only the readable prefix of the damaged capture is available')
            data = create_inputs(output, pcap)
            flow = data / (pcap.stem+'.tsv')
            stages = [['extract_features.py',str(analysis_input),'--output',str(flow)]]
            if usable_key:
                stages.append(['classify_packets.py',str(analysis_input),str(key),'--flow-tsv',str(flow),
                               '--output-dir',str(data/f'capture_{browser}_flows')])
            stages.extend([
                ['infer_packets.py',str(analysis_input),'--flow-tsv',str(flow),'--output-dir',str(data/f'capture_{browser}_inferred')],
                ['extract_header_seqs.py',str(output/'inputs'),'--browser',browser,'--protocol','both',
                 '--allow-unpaired','--mode','both' if usable_key else 'inferred','--output-dir',str(output/'sequences')]])
            for index, args in enumerate(stages,1):
                command([sys.executable,str(CODE/args[0]),*args[1:]],logs/f'{index:02d}_{args[0]}.log')
            result['verification'] = verify(output,browser,analysis_input,modes)
            save(output/'verification.json',result['verification'])
            result['status'] = 'partial' if result['limitations'] else 'success'
            result['ok'] = result['status'] == 'success'
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        if 'before' in result:
            try:
                result['after'] = {str(path):signature(path) for path in originals}
                result['original_inputs_unchanged'] = result['before'] == result['after']
            except Exception as exc:
                result['preservation_error'] = str(exc)
                result['original_inputs_unchanged'] = False
            if not result['original_inputs_unchanged']:
                STOP.set(); result['status'] = 'failed'; result['ok'] = False
        result['elapsed_seconds'] = round(time.time()-result['started'],3)
        if output is not None:
            save(output/'validation.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old_run',type=Path); parser.add_argument('new_run',type=Path)
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--stamp',default='flow_extraction_retry_20261012')
    args=parser.parse_args()
    args.new_run.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(args.new_run/'run.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    (args.new_run/'pid').write_text(str(os.getpid()))
    os.nice(10)
    latest={}
    for line in (args.old_run/'results.jsonl').read_text().splitlines():
        entry=json.loads(line); latest[entry['pcap']]=entry
    entries=[r for r in latest.values() if not r['ok']]
    save(args.new_run/'inventory.json',entries)
    prior={}
    results_path=args.new_run/'results.jsonl'
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            row=json.loads(line); prior[row['pcap']]=row
    # Completed partial/unavailable outcomes are explicit, not repeated endlessly.
    pending=[r for r in entries if r['pcap'] not in prior or prior[r['pcap']]['status']=='failed']
    if args.limit: pending=pending[:args.limit]
    started=time.time()
    def report(running):
        counts={s:sum(r['status']==s for r in prior.values()) for s in ('success','partial','no_eligible_packets','failed')}
        info=dict(status='RUNNING' if running else ('STOPPED' if STOP.is_set() else
                  'INCOMPLETE' if len(prior)<len(entries) else 'COMPLETED'),
                  original_total=len(latest),original_success=sum(r['ok'] for r in latest.values()),
                  retry_total=len(entries),processed=len(prior),counts=counts,
                  remaining=len(entries)-len(prior),elapsed_seconds=round(time.time()-started),
                  original_inputs_unchanged=all(r.get('original_inputs_unchanged') for r in prior.values()),
                  output_folder=args.stamp)
        save(args.new_run/'progress.json',info)
        if not running: save(args.new_run/'report.json',info)
    report(True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(retry,r,args.stamp):r['pcap'] for r in pending}
        for future in concurrent.futures.as_completed(futures):
            row=future.result(); prior[row['pcap']]=row
            with results_path.open('a',encoding='utf-8') as file:
                file.write(json.dumps(row,ensure_ascii=False)+'\n'); file.flush()
            print(json.dumps({k:row.get(k) for k in ('pcap','status','error','elapsed_seconds')},ensure_ascii=True),flush=True)
            report(True)
    report(False)


if __name__=='__main__': main()
