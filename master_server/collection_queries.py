"""Read-only collection projections. No Worker requests and no full-result list payloads."""
from __future__ import annotations

import csv
import io
import json
import re
from contextlib import contextmanager

from .schema import ValidationError

TERMINAL = "'SUCCEEDED','PARTIAL','FAILED','CANCELED','INTERRUPTED'"
STATS = f"""COUNT(*) AS total, COUNT(DISTINCT e.url) AS urls,
    COUNT(DISTINCT e.machine_id) AS machines,
    COALESCE(SUM(e.status='SUCCEEDED'),0) AS succeeded,
    COALESCE(SUM(e.status IN ('FAILED','PARTIAL','INTERRUPTED')),0) AS issues,
    COALESCE(SUM(e.status NOT IN ({TERMINAL})),0) AS active"""


@contextmanager
def snapshot(store, group):
    with store._connection() as db:
        db.execute('BEGIN')
        if group not in {'__all', '__ungrouped'} and not db.execute('SELECT 1 FROM collections WHERE id=?', (group,)).fetchone():
            raise ValidationError('总体任务不存在，请刷新')
        yield db


def scope(group):
    base = 'j.deleted_at IS NULL'
    if group == '__all':
        return base, []
    if group == '__ungrouped':
        return base + ' AND NOT EXISTS (SELECT 1 FROM collection_members cm WHERE cm.job_id=j.job_id)', []
    return base + ' AND j.job_id IN (SELECT job_id FROM collection_members WHERE collection_id=?)', [group]


def filters(group, values):
    if not isinstance(values, dict):
        raise ValidationError('filters 必须是对象')
    where, args = scope(group)
    for key, column in [('machine', 'e.machine_id'), ('batch', 'e.job_id'), ('browser', 'e.browser')]:
        value = values.get(key, '')
        if not isinstance(value, str) or len(value) > 128:
            raise ValidationError(f'{key} 格式错误')
        if value:
            where += f' AND {column}=?'; args.append(value)
    status = values.get('status', '')
    query = values.get('query', '')
    if not isinstance(query, str) or len(query) > 2048 or not isinstance(status, str) or len(status) > 64:
        raise ValidationError('查询条件格式错误')
    if status == 'CAPTURED':
        where += " AND (e.status='CAPTURED' OR json_extract(e.result_json,'$.status')='CAPTURED')"
    elif status:
        where += ' AND e.status=?'; args.append(status)
    if query.strip():
        where += ' AND (instr(lower(e.url),lower(?))>0 OR instr(lower(e.item_name),lower(?))>0 OR instr(lower(e.item_id),lower(?))>0)'
        args.extend([query.strip()] * 3)
    return where, args


def overview(store, group):
    where, args = scope(group)
    source = f'FROM jobs j JOIN executions e ON e.job_id=j.job_id WHERE {where}'
    with snapshot(store, group) as db:
        stats = dict(db.execute(f'SELECT {STATS} {source}', args).fetchone())
        machines = []
        for row in db.execute(f'SELECT e.machine_id, {STATS} {source} GROUP BY e.machine_id', args):
            entry = dict(row); machine = db.execute('SELECT name,base_url FROM machines WHERE machine_id=?', (entry['machine_id'],)).fetchone()
            machines.append({'id': entry.pop('machine_id'), 'name': machine['name'], 'url': machine['base_url'], 'stats': entry, 'batches': []})
        by_id = {m['id']: m for m in machines}
        for row in db.execute(f'SELECT DISTINCT e.machine_id,j.job_id,j.name {source} ORDER BY j.job_id', args):
            by_id[row['machine_id']]['batches'].append([row['job_id'], row['name']])
    return {'stats': stats, 'machines': machines}


def row_objects(records, full=False):
    rows = {}
    for record in records:
        e = dict(record)
        key = (e['job_id'], e['item_id'], e['machine_id'])
        if key not in rows:
            rows[key] = {'key': json.dumps(key, ensure_ascii=False),
                         'job': {'job_id': e['job_id'], 'name': e['job_name']},
                         'item': {'id': e['item_id'], 'name': e['item_name'], 'url': e['url']},
                         'machine_id': e['machine_id'], 'machine_name': e['machine_name'],
                         'machine_url': e['machine_url'], 'executions': {}}
        if full:
            raw = e.pop('result_json'); e['result'] = json.loads(raw) if raw else None
        rows[key]['executions'][e['browser']] = e
    return list(rows.values())


def results_page(store, group, values, offset, limit):
    where, args = filters(group, values)
    # Filter executions first: browser + status must match the same execution.
    # Then return all browser statuses for only the selected URL/batch/machine rows.
    matched = f'''SELECT e.job_id,e.item_id,e.machine_id,j.created_at
        FROM jobs j JOIN executions e ON e.job_id=j.job_id WHERE {where}
        GROUP BY e.job_id,e.item_id,e.machine_id'''
    with snapshot(store, group) as db:
        total = db.execute(f'SELECT COUNT(*) FROM ({matched})', args).fetchone()[0]
        offset = min(offset, max(0, (total - 1) // limit) * limit)
        records = db.execute(f'''WITH selected AS (
            {matched} ORDER BY j.created_at DESC,e.job_id,e.item_id,e.machine_id LIMIT ? OFFSET ?
        ) SELECT e.execution_id,e.job_id,e.item_id,e.item_name,e.url,e.machine_id,e.browser,e.status,e.stage,e.updated_at,
            j.name AS job_name,m.name AS machine_name,m.base_url AS machine_url
        FROM selected s JOIN executions e ON e.job_id=s.job_id AND e.item_id=s.item_id AND e.machine_id=s.machine_id
        JOIN jobs j ON j.job_id=e.job_id JOIN machines m ON m.machine_id=e.machine_id
        ORDER BY s.created_at DESC,e.job_id,e.item_id,e.machine_id,e.browser''', [*args, limit, offset]).fetchall()
        return {'rows': row_objects(records), 'page': {'offset': offset, 'limit': limit, 'total': total}}


def result_detail(store, group, job, item, machine):
    where, args = scope(group)
    with snapshot(store, group) as db:
        records = db.execute(f'''SELECT e.*,j.name AS job_name,m.name AS machine_name,m.base_url AS machine_url
            FROM executions e JOIN jobs j ON j.job_id=e.job_id JOIN machines m ON m.machine_id=e.machine_id
            WHERE {where} AND e.job_id=? AND e.item_id=? AND e.machine_id=? ORDER BY e.browser''',
            [*args, job, item, machine]).fetchall()
        rows = row_objects(records, full=True)
        return rows[0] if rows else None


def absolute(path):
    return isinstance(path, str) and bool(re.match(r'^(?:[A-Za-z]:[\\/]|/|\\\\)', path))


def artifact_path(execution, artifact, location):
    for key in ('absolute_path', 'path'):
        if absolute(artifact.get(key)): return artifact[key], 'Worker 上报'
    relative = artifact.get('path', '')
    if not relative: return '', '尚未上报'
    if '..' in re.split(r'[\\/]', relative): return relative, '路径异常，待核验'
    root = execution.get('task_absolute_path', '')
    source = 'Worker 上报目录'
    if not absolute(root):
        root = location.get('root', '')
        task = execution.get('worker_task_id', '')
        if not absolute(root) or not task or re.search(r'[\\/]', task) or task in ('.', '..'):
            return relative, '相对任务目录 · 完整位置待补充'
        separator = '\\' if re.match(r'^[A-Za-z]:|^\\\\', root) else '/'
        root = root.rstrip('/\\') + separator + task
        source = '本地配置推导 · 未核验'
    separator = '\\' if re.match(r'^[A-Za-z]:|^\\\\', root) else '/'
    return root.rstrip('/\\') + separator + re.sub(r'[\\/]', lambda _: separator, relative), source


def csv_export(store, group, values, locations):
    where, args = filters(group, values)
    if not isinstance(locations, dict) or len(locations) > 1000:
        raise ValidationError('位置设置格式错误')
    for value in locations.values():
        if not isinstance(value, dict) or not isinstance(value.get('root', ''), str) or len(value.get('root', '')) > 4096:
            raise ValidationError('任务根目录格式错误')
    # Validate before HTTP headers are sent. Streaming below holds one read snapshot.
    with snapshot(store, group): pass
    def stream():
        buffer = io.StringIO(); writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
        def line(values):
            cells=[]
            for value in values:
                text = '' if value is None else str(value).lower() if isinstance(value, bool) else str(value)
                cells.append("'" + text if re.match(r'^\s*[=+@-]', text) else text)
            writer.writerow(cells); text=buffer.getvalue(); buffer.seek(0); buffer.truncate(); return text
        yield '\ufeff' + line(['总体任务','批次','任务 ID','来源编号','URL','机器','Worker API 地址','浏览器','执行状态','Worker 任务目录名','产物','路径','路径来源','大小 bytes','检查通过'])
        with snapshot(store, group) as db:
            name = '全部数据' if group == '__all' else '未归组任务' if group == '__ungrouped' else db.execute('SELECT name FROM collections WHERE id=?',(group,)).fetchone()[0]
            cursor = db.execute(f'''SELECT e.*,j.name AS job_name,m.name AS machine_name,m.base_url AS machine_url
                FROM jobs j JOIN executions e ON e.job_id=j.job_id JOIN machines m ON m.machine_id=e.machine_id
                WHERE {where} ORDER BY e.job_id,e.item_id,e.machine_id,e.browser''', args)
            for record in cursor:
                e = dict(record); result=json.loads(e['result_json']) if e['result_json'] else {}
                artifacts = [(k,v) for k,v in (result.get('artifacts') or {}).items() if v] or [('未上报产物', {})]
                for kind, artifact in artifacts:
                    path, source=artifact_path(e, artifact, locations.get(e['machine_id'],{}))
                    yield line([name,e['job_name'],e['job_id'],e['item_id'],e['url'],e['machine_name'],e['machine_url'],e['browser'],e['status'],e['worker_task_id'],kind,path,source,artifact.get('size'),(result.get('checks') or {}).get(kind)])
    return stream()
