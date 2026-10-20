"""Recover readable capture records into a new file; never modify the source."""
import subprocess
from pathlib import Path


def probe(pcap, tshark='tshark'):
    args = [tshark, '-r', str(pcap), '-T', 'fields', '-e', 'frame.number',
            '-e', 'frame.len', '-e', 'frame.cap_len', '-e', 'frame.time_epoch']
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    rows = result.stdout.splitlines()
    return dict(returncode=result.returncode, stderr=result.stderr, frames=len(rows), records=rows)


def prepare_capture(source, destination, tshark='tshark', editcap='editcap'):
    source, destination = Path(source), Path(destination)
    info = probe(source, tshark)
    if info['returncode'] == 0:
        return source, {k: v for k, v in info.items() if k != 'records'} | {'partial': False}
    diagnostic = info['stderr'].lower()
    if not any(s in diagnostic for s in (
            'cut short in the middle of a packet', 'appears to be damaged or corrupt')):
        raise RuntimeError('TShark could not read input: ' + info['stderr'])
    if not info['frames']:
        raise ValueError('No readable capture records: ' + info['stderr'])
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('Recovery requires a new, distinct output file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([editcap, '-F', 'pcapng', '-r', str(source), str(destination),
                             f"1-{info['frames']}"], capture_output=True, text=True, timeout=300)
    if (result.returncode and not any(s in result.stderr.lower() for s in (
            'cut short in the middle of a packet', 'appears to be damaged or corrupt'))) or not destination.is_file():
        raise RuntimeError('editcap recovery failed: ' + result.stderr)
    recovered = probe(destination, tshark)
    if recovered['returncode'] or recovered['records'] != info['records']:
        raise ValueError('Recovered records do not match the readable source prefix')
    return destination, dict(partial=True, source_error=info['stderr'],
                            recovered_frames=recovered['frames'], editcap_stderr=result.stderr)
