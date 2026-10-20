"""Flat result inputs with explicit page identity; legacy reads remain supported."""
import json
from pathlib import Path


def page_metadata(pcap):
    pcap = Path(pcap)
    return {'layout_version': 2, 'page_directory': pcap.parent.name,
            'pcap': str(pcap), 'browser': pcap.stem.removeprefix('capture_')}


def create_inputs(output, pcap):
    inputs = Path(output) / 'inputs'
    inputs.mkdir(parents=True, exist_ok=False)
    (inputs / 'page.json').write_text(json.dumps(page_metadata(pcap), ensure_ascii=False, indent=2),
                                    encoding='utf-8')
    return inputs


def page_name(folder):
    parent = Path(folder).parent
    if parent.name != 'inputs':
        return parent.name
    metadata = json.loads((parent / 'page.json').read_text(encoding='utf-8'))
    name = metadata['page_directory']
    if metadata.get('layout_version') != 2 or not name or name in ('.', '..', 'inputs') or '/' in name or '\\' in name:
        raise ValueError(f'Invalid page identity: {parent}')
    return name


def detail_directory(output, browser, suffix):
    inputs = Path(output) / 'inputs'
    name = f'capture_{browser}_{suffix}'
    candidates = ([inputs / name] if (inputs / name).is_dir() else [])
    candidates += [p / name for p in inputs.iterdir() if p.is_dir() and (p / name).is_dir()]
    if len(candidates) != 1:
        raise ValueError(f'Expected exactly one {name} directory in {inputs}, found {len(candidates)}')
    return candidates[0]
