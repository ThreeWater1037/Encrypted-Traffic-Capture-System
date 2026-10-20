"""Analysis-facing result indexes. Read Master snapshots; never poll Workers."""
from __future__ import annotations

import csv
import io
import json
import re

from .collection_queries import absolute, artifact_path, filters, snapshot
from .schema import ValidationError

ARTIFACTS = {'pcap': 'PCAP', 'tls_keylog': 'TLS 密钥', 'html': '页面 HTML',
             'body': '页面 HTML', 'report': '抓取报告', 'extract': '提取结果',
             'classify': '分类结果', 'infer': '推断结果', 'extraction': '提取校验凭据',
             'flow_sequences': '流序列', 'tls_features': 'TLS 特征'}
FROM = '''FROM executions e JOIN jobs j ON j.job_id=e.job_id
    JOIN machines m ON m.machine_id=e.machine_id
    LEFT JOIN collection_members cm ON cm.job_id=j.job_id
    LEFT JOIN collections c ON c.id=cm.collection_id'''
COUNTS = 'COUNT(*) AS records, COUNT(DISTINCT e.url) AS urls, COUNT(DISTINCT e.job_id) AS batches'


def pagination(values, maximum=100):
    try:
        return max(0, int(values.get('offset', 0))), min(maximum, max(1, int(values.get('limit', 20))))
    except (ValueError, TypeError):
        raise ValidationError('offset 和 limit 必须是整数')


def group_page(store, values):
    offset, limit = pagination(values)
    query = values.get('query', '')
    if not isinstance(query, str) or len(query) > 2048:
        raise ValidationError('查询条件格式错误')
    with snapshot(store, '__all') as db:
        total = db.execute('SELECT COUNT(*) FROM collections WHERE instr(lower(name),lower(?))>0', (query,)).fetchone()[0]
        offset = min(offset, max(0, (total - 1) // limit) * limit)
        groups = [dict(r) for r in db.execute('''SELECT c.id,c.name,COUNT(j.job_id) AS batch_count
            FROM collections c LEFT JOIN collection_members cm ON cm.collection_id=c.id
            LEFT JOIN jobs j ON j.job_id=cm.job_id AND j.deleted_at IS NULL
            WHERE instr(lower(c.name),lower(?))>0 GROUP BY c.id ORDER BY c.created_at DESC,c.id LIMIT ? OFFSET ?''',
            (query, limit, offset))]
        counts = db.execute('''SELECT COUNT(*) AS all_count,COALESCE(SUM(cm.job_id IS NULL),0) AS ungrouped
            FROM jobs j LEFT JOIN collection_members cm ON cm.job_id=j.job_id WHERE j.deleted_at IS NULL''').fetchone()
        return {'groups': groups, 'page': {'offset': offset, 'limit': limit, 'total': total},
                'all_count': counts['all_count'], 'ungrouped_count': counts['ungrouped'],
                'revision': db.execute('SELECT revision FROM collection_revision').fetchone()[0]}


def group_detail(store, group):
    with snapshot(store, group) as db:
        row = db.execute('SELECT id,name FROM collections WHERE id=?', (group,)).fetchone()
        if row is None:
            raise ValidationError('请选择可编辑的总体任务')
        ids = [r[0] for r in db.execute('''SELECT j.job_id FROM jobs j JOIN collection_members cm ON cm.job_id=j.job_id
            WHERE cm.collection_id=? AND j.deleted_at IS NULL ORDER BY j.job_id''', (group,))]
        return {**dict(row), 'job_ids': ids, 'revision': db.execute('SELECT revision FROM collection_revision').fetchone()[0]}


def overview(store, group, values):
    where, args = filters(group, values)
    machine_where, machine_args = filters(group, {**values, 'machine': ''})
    with snapshot(store, group) as db:
        stats = dict(db.execute(f'SELECT {COUNTS} {FROM} WHERE {where}', args).fetchone())
        machines = [dict(r) for r in db.execute(f'''SELECT m.machine_id,m.name,COUNT(*) AS records
            {FROM} WHERE {machine_where} GROUP BY m.machine_id ORDER BY records DESC,m.machine_id''', machine_args)]
        return {'stats': stats, 'machines': machines}


def file_state(path, size, checked):
    if not path:
        return 'missing'
    if checked == 0 or (isinstance(size, (int, float)) and size <= 0):
        return 'invalid'
    return 'present'


def results_page(store, group, values):
    offset, limit = pagination(values)
    where, args = filters(group, values)
    with snapshot(store, group) as db:
        total = db.execute(f'SELECT COUNT(*) {FROM} WHERE {where}', args).fetchone()[0]
        offset = min(offset, max(0, (total - 1) // limit) * limit)
        records = db.execute(f'''SELECT e.execution_id,e.job_id,e.item_id,e.url,e.browser,e.machine_id,
            j.name AS batch_name,j.created_at,m.name AS machine_name,
            json_extract(e.result_json,'$.artifacts.pcap.path') AS pcap_path,
            json_extract(e.result_json,'$.artifacts.pcap.absolute_path') AS pcap_absolute,
            json_extract(e.result_json,'$.artifacts.pcap.size') AS pcap_size,
            json_extract(e.result_json,'$.checks.pcap') AS pcap_checked,
            json_extract(e.result_json,'$.checks.pcap_retired') AS pcap_retired,
            json_extract(e.result_json,'$.artifacts.tls_keylog.path') AS tls_path,
            json_extract(e.result_json,'$.artifacts.tls_keylog.absolute_path') AS tls_absolute,
            json_extract(e.result_json,'$.artifacts.tls_keylog.size') AS tls_size,
            json_extract(e.result_json,'$.checks.tls_keylog') AS tls_checked
            {FROM} WHERE {where} ORDER BY j.created_at DESC,e.job_id,e.item_id,e.machine_id,e.browser,e.execution_id
            LIMIT ? OFFSET ?''', [*args, limit, offset]).fetchall()
        rows = []
        for record in records:
            row = dict(record)
            row['files'] = {}
            for key in ('pcap', 'tls'):
                path = row.pop(key + '_absolute') or row.pop(key + '_path')
                row.pop(key + '_path', None)
                row['files'][key] = file_state(path, row.pop(key + '_size'), row.pop(key + '_checked'))
            if row.pop('pcap_retired'):
                row['files']['pcap'] = 'retired'
            rows.append(row)
        return {'rows': rows, 'page': {'offset': offset, 'limit': limit, 'total': total}}


def locations(store):
    with snapshot(store, '__all') as db:
        return {'machines': [dict(r) for r in db.execute('''SELECT m.machine_id,m.name,m.base_url,
            COALESCE(l.root,'') AS root FROM machines m LEFT JOIN result_locations l ON l.machine_id=m.machine_id
            ORDER BY m.name,m.machine_id''')]}


def save_location(store, payload):
    if not isinstance(payload, dict):
        raise ValidationError('请求体必须是对象')
    machine, root = payload.get('machine_id'), payload.get('root')
    if not isinstance(machine, str) or not isinstance(root, str) or len(root) > 4096:
        raise ValidationError('机器或任务根目录格式错误')
    root = root.strip()
    if root and (not absolute(root) or '..' in re.split(r'[\\/]', root) or any(ord(c) < 32 for c in root)):
        raise ValidationError('请填写 Worker 的绝对任务根目录，不含任务名和 ..')
    with store._connection() as db:
        if not db.execute('SELECT 1 FROM machines WHERE machine_id=?', (machine,)).fetchone():
            raise ValidationError('机器不存在')
        db.execute('''INSERT INTO result_locations VALUES (?,?) ON CONFLICT(machine_id) DO UPDATE SET root=excluded.root''', (machine, root))
    return {'machine_id': machine, 'root': root}


def artifact_rows(execution, root):
    result = json.loads(execution['result_json']) if execution['result_json'] else {}
    artifacts = result.get('artifacts') or {}
    checks = result.get('checks') or {}
    rows = []
    for kind, label in ARTIFACTS.items():
        if kind == 'pcap' and checks.get('pcap_retired'):
            rows.append({'kind': kind, 'label': label, 'path': '',
                         'source': '流序列和 TLS 特征均已验证', 'absolute': False,
                         'size': None, 'state': 'retired'})
            continue
        if kind not in ('pcap', 'tls_keylog') and not artifacts.get(kind):
            continue
        artifact = artifacts.get(kind) or {}
        path, source = artifact_path({**execution, 'task_absolute_path': result.get('task_absolute_path', '')}, artifact, {'root': root})
        if source == '本地配置推导 · 未核验':
            source = '按机器目录配置拼接 · 未核验'
        rows.append({'kind': kind, 'label': label, 'path': path, 'source': source,
                     'absolute': absolute(path) and source != '路径异常，待核验', 'size': artifact.get('size'),
                     'state': file_state(artifact.get('absolute_path') or artifact.get('path'), artifact.get('size'), checks.get(kind))})
    return rows


def result_detail(store, group, execution_id):
    where, args = filters(group, {})
    with snapshot(store, group) as db:
        row = db.execute(f'''SELECT e.*,j.name AS batch_name,m.name AS machine_name,m.base_url AS machine_url,
            COALESCE(c.name,'未归组') AS group_name,COALESCE(l.root,'') AS root
            {FROM} LEFT JOIN result_locations l ON l.machine_id=m.machine_id
            WHERE {where} AND e.execution_id=?''', [*args, execution_id]).fetchone()
        if row is None:
            return None
        execution = dict(row)
        return {key: execution[key] for key in ('execution_id', 'job_id', 'item_id', 'url', 'browser', 'batch_name',
                'machine_id', 'machine_name', 'machine_url', 'worker_task_id', 'group_name')} | {
                    'files': artifact_rows(execution, execution['root'])}


def csv_export(store, group, values):
    where, args = filters(group, values)
    with snapshot(store, group):
        pass

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)

        def line(values):
            cells = []
            for value in values:
                text = '' if value is None else str(value)
                cells.append("'" + text if re.match(r'^\s*[=+@-]', text) else text)
            writer.writerow(cells)
            text = buffer.getvalue()
            buffer.seek(0); buffer.truncate()
            return text

        yield '\ufeff' + line(['总体任务', '批次', '任务 ID', 'URL 编号', 'URL', '机器', '机器地址', '浏览器',
                               'Worker 任务目录名', '文件类型', '文件位置', '路径来源', '完整路径', '大小 bytes', '文件情况'])
        with snapshot(store, group) as db:
            records = db.execute(f'''SELECT e.*,j.name AS batch_name,m.name AS machine_name,m.base_url AS machine_url,
                COALESCE(c.name,'未归组') AS group_name,COALESCE(l.root,'') AS root
                {FROM} LEFT JOIN result_locations l ON l.machine_id=m.machine_id
                WHERE {where} ORDER BY j.created_at DESC,e.job_id,e.item_id,e.machine_id,e.browser,e.execution_id''', args)
            for record in records:
                e = dict(record)
                for f in artifact_rows(e, e['root']):
                    yield line([e['group_name'], e['batch_name'], e['job_id'], e['item_id'], e['url'], e['machine_name'],
                        e['machine_url'], e['browser'], e['worker_task_id'], f['label'], f['path'], f['source'],
                        '是' if f['absolute'] else '否', f['size'], {'present': '已上报', 'missing': '未上报', 'invalid': '空文件或检查未通过', 'retired': '提取完成，原始 PCAP 已删除'}[f['state']]])
    return stream()
