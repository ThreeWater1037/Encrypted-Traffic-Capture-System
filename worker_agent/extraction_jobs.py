"""Durable, opt-in extraction jobs consumed by the capture Worker's one thread.

They never run beside capture. Capture jobs take priority between PCAP units.
The existing capture-time background pipeline is deliberately independent.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from capture_extraction import BROWSERS, save_json, verified_receipt
from .schema import ValidationError, TASK_ID_RE
from .task_store import TERMINAL_STATUSES, utc_now


def extraction_id(source_id):
    return 'extract-' + hashlib.sha256(source_id.encode()).hexdigest()[:32]


class ExtractionJobs:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.config = manager.config

    def blocks_resume(self, source_id):
        job = self.store.get_task_status(extraction_id(source_id))
        return bool(job and job['status'] not in TERMINAL_STATUSES)

    def submit(self, source_id, *, keep_pcap=False, keep_pcap_count=None):
        from .task_runner import TaskConflictError, QueueFullError
        if not isinstance(source_id, str) or not TASK_ID_RE.fullmatch(source_id):
            raise ValidationError('source_task_id 无效')
        if not isinstance(keep_pcap, bool):
            raise ValidationError('keep_pcap 必须是布尔值')
        if keep_pcap_count is not None and (type(keep_pcap_count) is not int or keep_pcap_count < 1):
            raise ValidationError('keep_pcap_count 必须是正整数或 null（全部保留）')
        if not keep_pcap and keep_pcap_count is not None:
            raise ValidationError('指定保留数量时必须开启 keep_pcap')
        with self.manager._submission_lock:
            source = self.store.get_task(source_id)
            if not source or source['request'].get('kind') == 'extraction':
                raise ValidationError('原采集任务不存在')
            if source['status'] not in {'SUCCEEDED', 'PARTIAL'}:
                raise TaskConflictError('只允许提取已结束的成功或部分成功采集任务')
            if not source['request'].get('pcap'):
                raise ValidationError('原任务没有开启 PCAP 抓包')
            task_id = extraction_id(source_id)
            previous = self.store.get_task_status(task_id)
            if previous:
                existing = self.get(task_id)
                if not existing:
                    raise TaskConflictError('提取任务 ID 与既有采集任务冲突')
                if (existing['keep_pcap'], existing['keep_pcap_count']) != (keep_pcap, keep_pcap_count):
                    raise TaskConflictError('提取任务已存在，重试必须沿用首次提交的 PCAP 保留策略；已删除的 PCAP 无法恢复')
                # Explicit retries keep the same ID and verified receipts. The
                # durable per-unit cursor is reset; verified units are cheap to skip.
                if previous['status'] in {'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'}:
                    self.store.update_task(task_id, status='QUEUED', stage='WAITING_FOR_CAPTURE',
                                           cancel_requested=False, error=None, result_json=None,
                                           finished_at=None)
                    self.manager.enqueue_extraction(task_id)
                return self.get(task_id), False
            if self.manager.queue_size >= self.config.max_queue_size:
                raise QueueFullError('Worker 本地任务队列已满')
            browsers = [b for b in BROWSERS if b in source['request']['browsers']]
            if not browsers:
                raise ValidationError('任务没有支持的浏览器')
            payload = dict(task_id=task_id, kind='extraction', source_task_id=source_id,
                           browsers=browsers, items=[], pcap=False, outputs={},
                           keep_pcap=keep_pcap, keep_pcap_count=keep_pcap_count,
                           analysis={'steps': [], 'pipeline': False, 'delete_pcap': not keep_pcap or keep_pcap_count is not None})
            self.store.create_task(payload)
            (self.config.tasks_dir / task_id).mkdir(parents=True, exist_ok=True)
            self.manager.enqueue_extraction(task_id)
            return self.get(task_id), True

    def get(self, task_id):
        task = self.store.get_task(task_id)
        if not task or task['request'].get('kind') != 'extraction':
            return None
        result = task.get('result') or {}
        current = result.get('current') or {}
        if current.get('directory') and current.get('browser') in BROWSERS:
            try:
                receipt = json.loads((Path(current['directory']) / f'extraction_{current["browser"]}.json').read_text(encoding='utf-8'))
                result['current_stage'] = receipt.get('stage') if receipt.get('status') == 'running' else 'preparing'
            except (OSError, ValueError):
                result['current_stage'] = 'preparing'
        return {k: task[k] for k in ('task_id', 'status', 'stage', 'created_at',
                                     'updated_at', 'started_at', 'finished_at', 'error')} | {
            'source_task_id': task['request']['source_task_id'],
            'browsers': task['request']['browsers'],
            'delete_pcap': not task['request'].get('keep_pcap', False) or task['request'].get('keep_pcap_count') is not None,
            'keep_pcap': task['request'].get('keep_pcap', False),
            'keep_pcap_count': task['request'].get('keep_pcap_count'),
            'progress': result,
            'waiting_for_capture': task['status'] == 'QUEUED',
            'output_root': str(self.config.tasks_dir / task['request']['source_task_id'] / 'fetch_output'),
            'result_layout': '<URL目录>/flow_extraction/<browser>/attempt-*/',
        }

    def list(self, limit=20, offset=0):
        with self.store._connection() as c:
            where = "json_extract(request_json, '$.kind') = 'extraction'"
            total = c.execute('SELECT COUNT(*) FROM tasks WHERE '+where).fetchone()[0]
            rows = c.execute('SELECT task_id FROM tasks WHERE '+where+
                             ' ORDER BY created_at DESC, task_id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        jobs = [self.get(row[0]) for row in rows]
        for job in jobs:
            job['progress'].pop('errors', None)  # bounded details are fetched on click
        return {'jobs': jobs,
                'page': {'total': total, 'offset': offset, 'limit': limit},
                'active_task_id': self.manager.active_task_id, 'pcap_retention_supported': True}

    def _units(self, source):
        root = (self.config.tasks_dir / source['task_id'] / 'fetch_output').resolve()
        # Exact item IDs; never glob with user-controlled patterns. Only direct,
        # real child directories are admitted, never symlinked capture trees.
        directories = {}
        if root.is_dir():
            for path in root.iterdir():
                if path.is_dir() and not path.is_symlink() and path.resolve().parent == root:
                    keys = set()
                    for browser in BROWSERS:
                        marker = path / f'capture_{browser}.complete.json'
                        if marker.is_symlink():
                            continue
                        try:
                            key = json.loads(marker.read_text(encoding='utf-8')).get('item_id')
                            if isinstance(key, str):
                                keys.add(key)
                        except (OSError, ValueError, TypeError, AttributeError):
                            pass
                    if len(keys) == 1:
                        directories.setdefault(keys.pop(), []).append(path)
        for item in source['request']['items']:
            matches = directories.get(item['id'], [])
            directory = matches[0] if len(matches) == 1 else None
            for browser in BROWSERS:
                if browser in source['request']['browsers']:
                    yield item, browser, directory

    def run(self, task):
        from .task_runner import TaskCanceledError, TaskServiceStoppingError
        manager = self.manager
        task_id = task['task_id']
        source_id = task['request']['source_task_id']
        log_path = self.config.tasks_dir / task_id / 'worker.log'
        progress = task.get('result') or dict(cursor=0, total=0, succeeded=0, failed=0,
                                              deleted=0, retained=0, errors=[], current=None)
        progress.setdefault('retained', 0)
        keep_pcap = task['request'].get('keep_pcap', False)
        keep_count = task['request'].get('keep_pcap_count')
        def refresh_manifest():
            manifest = manager._build_manifest(self.config.tasks_dir / source_id, source['request'],
                                               (source.get('result') or {}).get('process_errors', []))
            save_json(self.config.tasks_dir / source_id / 'manifest.json', manifest)
            self.store.update_task(source_id, result_json=manifest)
        try:
            source = self.store.get_task(source_id)
            if not source or source['status'] not in {'SUCCEEDED', 'PARTIAL'}:
                raise RuntimeError('原采集任务尚未完成，拒绝提取')
            if (self.config.tasks_dir / source_id).is_symlink() or (self.config.tasks_dir / source_id / 'fetch_output').is_symlink():
                raise RuntimeError('拒绝符号链接任务目录')
            units = list(self._units(source))
            progress['total'] = len(units)
            self.store.update_task(task_id, status='ANALYZING', stage='ANALYZING',
                                   started_at=task['started_at'] or utc_now(), error=None)
            for index in range(progress['cursor'], len(units)):
                manager._raise_if_canceled(task_id)
                if manager._stop_event.is_set():
                    raise TaskServiceStoppingError()
                # Capture arrivals never overlap extraction; yield at a durable
                # boundary, after all children of the previous unit have exited.
                if not manager._queue.empty():
                    self.store.update_task(task_id, status='QUEUED', stage='WAITING_FOR_CAPTURE', result_json=progress)
                    manager.enqueue_extraction(task_id)
                    return
                item, browser, directory = units[index]
                retain = keep_pcap and (keep_count is None or index < keep_count)
                progress['current'] = {'item_id': item['id'], 'name': item['name'], 'browser': browser,
                                       'directory': str(directory) if directory else None}
                self.store.update_task(task_id, result_json=progress)
                try:
                    if directory is None:
                        raise RuntimeError('找不到唯一采集目录，PCAP 未删除')
                    marker = directory / f'capture_{browser}.complete.json'
                    if marker.is_symlink():
                        raise RuntimeError('拒绝符号链接检查点')
                    checkpoint = json.loads(marker.read_text(encoding='utf-8'))
                    if checkpoint.get('item_id') != item['id'] or checkpoint.get('url') != item['url']:
                        raise RuntimeError('检查点与原任务 URL 不一致')
                    command = [str(self.config.python_executable),
                        str(self.config.project_root / 'capture_extraction.py'), str(directory), browser]
                    if not retain:
                        command.append('--delete-pcap')
                    code = manager._run_command(task_id, command, log_path, time.monotonic() + 6000)
                    if manager._stop_event.is_set():
                        raise TaskServiceStoppingError()
                    manager._raise_if_canceled(task_id)
                    receipt = verified_receipt(directory, browser, full=True)
                    pcap_exists = (directory / f'capture_{browser}.pcap').is_file()
                    retention_valid = pcap_exists if retain else (not pcap_exists and receipt and receipt.get('pcap_deleted'))
                    if code or not receipt or not retention_valid:
                        error = f'提取或 PCAP 保留策略验证失败（退出码 {code}），详见 {directory / ("extraction_"+browser+".json")}'
                        raise RuntimeError(error)
                    progress['succeeded'] += 1
                    progress['retained' if retain else 'deleted'] += 1
                except (TaskCanceledError, TaskServiceStoppingError):
                    raise
                except Exception as exc:
                    progress['failed'] += 1
                    progress['errors'] = (progress['errors'] + [dict(item_id=item['id'], browser=browser, error=str(exc))])[-100:]
                    manager._append_log(log_path, f'{item["id"]}/{browser}: {exc}')
                progress['cursor'] = index + 1
                progress['current'] = None
                self.store.update_task(task_id, result_json=progress)
            if not units:
                raise RuntimeError('任务没有可提取单元')
            # Refresh artifact locations without rewriting the capture request or
            # changing its historical completion/cancellation status.
            refresh_manifest()
            status = 'SUCCEEDED' if not progress['failed'] else ('PARTIAL' if progress['succeeded'] else 'FAILED')
            self.store.update_task(task_id, status=status, stage='DONE', result_json=progress,
                                   error=f'{progress["failed"]} 个单元提取失败，原始 PCAP 保留' if progress['failed'] else None,
                                   finished_at=utc_now())
        except TaskServiceStoppingError:
            self.store.update_task(task_id, status='QUEUED', stage='RESUMING', result_json=progress)
        except TaskCanceledError:
            try:
                refresh_manifest()
            except Exception as exc:
                manager._append_log(log_path, f'结果索引刷新失败，可重试提取修复：{exc}')
            self.store.update_task(task_id, status='CANCELED', stage='CANCELED', result_json=progress, finished_at=utc_now())
        except Exception as exc:
            manager._append_log(log_path, str(exc))
            self.store.update_task(task_id, status='FAILED', stage='DONE', result_json=progress,
                                   error=str(exc), finished_at=utc_now())


def register_routes(app, manager):
    from flask import jsonify, request
    from .task_runner import TaskConflictError, QueueFullError

    @app.errorhandler(TaskConflictError)
    def extraction_conflict(exc):
        return jsonify(error='conflict', message=str(exc)), 409

    @app.get('/api/v1/extractions')
    def list_extractions():
        try:
            limit = min(100, max(1, int(request.args.get('limit', 20))))
            offset = max(0, int(request.args.get('offset', 0)))
        except ValueError:
            return jsonify(message='分页参数必须为整数'), 400
        return jsonify(manager.extractions.list(limit, offset))

    @app.post('/api/v1/extractions')
    def submit_extraction():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(message='请求必须是 JSON 对象'), 400
        try:
            result, created = manager.extractions.submit(data.get('source_task_id'),
                keep_pcap=data.get('keep_pcap', False), keep_pcap_count=data.get('keep_pcap_count'))
        except ValidationError as exc:
            return jsonify(message=str(exc)), 400
        except QueueFullError as exc:
            return jsonify(message=str(exc)), 429
        return jsonify(result), 202 if created or result['status'] == 'QUEUED' else 200

    @app.get('/api/v1/extractions/<extraction_id>')
    def get_extraction(extraction_id):
        result = manager.extractions.get(extraction_id)
        return (jsonify(result), 200) if result else (jsonify(message='提取任务不存在'), 404)

    @app.post('/api/v1/extractions/<extraction_id>/cancel')
    def cancel_extraction(extraction_id):
        if not manager.extractions.get(extraction_id):
            return jsonify(message='提取任务不存在'), 404
        manager.cancel(extraction_id)
        return jsonify(manager.extractions.get(extraction_id))
