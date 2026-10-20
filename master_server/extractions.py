"""Standalone extraction UI API; Worker remains authoritative for admission."""
from flask import jsonify, request
from .schema import ValidationError
from .worker_client import WorkerRequestError


def register_routes(app, store, dispatcher):
    synced = {}

    def machine_client(machine_id):
        machine = store.get_machine(machine_id)
        if not machine or not machine['enabled']:
            raise ValidationError('请选择已启用的机器')
        return dispatcher._client_factory(machine)

    def page_args():
        try:
            return min(100, max(1, int(request.args.get('limit', 20)))), max(0, int(request.args.get('offset', 0)))
        except ValueError:
            raise ValidationError('分页参数必须为整数')

    def mapping(machine_id, source_id):
        with store._connection() as c:
            row = c.execute('''SELECT j.job_id, j.name, j.status FROM jobs j JOIN executions e ON e.job_id=j.job_id
                WHERE e.machine_id=? AND e.worker_task_id=? AND j.deleted_at IS NULL LIMIT 1''', (machine_id, source_id)).fetchone()
        return dict(row) if row else None

    def worker_error(exc):
        if exc.status_code == 404:
            return jsonify(message='该 Worker 尚未升级独立提取接口，或提取记录不存在；请勿为此中断正在运行的抓包任务'), 409
        return jsonify(message=str(exc)), exc.status_code if exc.status_code in {400, 409, 429} else 502

    @app.get('/api/v1/extractions/candidates')
    def extraction_candidates():
        machine_id = request.args.get('machine_id', '')
        limit, offset = page_args()
        query = '%' + request.args.get('query', '').strip() + '%'
        # Pagination happens in SQL; never send all URL executions to the UI.
        sql = '''FROM jobs j JOIN executions e ON e.job_id=j.job_id
            WHERE j.deleted_at IS NULL AND j.status IN ('SUCCEEDED','PARTIAL')
              AND json_extract(j.request_json, '$.pcap')=1 AND e.machine_id=?
              AND (j.name LIKE ? OR j.job_id LIKE ?)
            GROUP BY j.job_id, e.worker_task_id'''
        with store._connection() as c:
            params = (machine_id, query, query)
            total = c.execute('SELECT COUNT(*) FROM (SELECT j.job_id '+sql+')', params).fetchone()[0]
            rows = c.execute('''SELECT j.job_id, j.name, j.status, j.created_at,
                e.worker_task_id, COUNT(*) AS units, GROUP_CONCAT(DISTINCT e.browser) AS browsers '''+
                sql+' ORDER BY j.created_at DESC, j.job_id LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        return jsonify(jobs=[dict(row) | {'browsers': row['browsers'].split(',')} for row in rows],
                       page={'limit': limit, 'offset': offset, 'total': total})

    @app.get('/api/v1/machines/<machine_id>/extractions')
    def list_extractions(machine_id):
        limit, offset = page_args()
        try:
            result = machine_client(machine_id).list_extractions(limit=limit, offset=offset)
            for row in result.get('jobs', []):
                row['job'] = mapping(machine_id, row['source_task_id'])
            return jsonify(result)
        except WorkerRequestError as exc:
            return worker_error(exc)

    @app.post('/api/v1/machines/<machine_id>/extractions')
    def submit_extraction(machine_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('job_id'), str):
            raise ValidationError('需要 job_id')
        keep_pcap, keep_count = data.get('keep_pcap', False), data.get('keep_pcap_count')
        if not isinstance(keep_pcap, bool):
            raise ValidationError('keep_pcap 必须是布尔值')
        if keep_count is not None and (type(keep_count) is not int or keep_count < 1):
            raise ValidationError('keep_pcap_count 必须是正整数或 null（全部保留）')
        if not keep_pcap and keep_count is not None:
            raise ValidationError('指定保留数量时必须开启 keep_pcap')
        source_id = store.get_worker_task_id(data['job_id'], machine_id)
        job = mapping(machine_id, source_id)
        if not job or job['status'] not in {'SUCCEEDED', 'PARTIAL'}:
            return jsonify(message='只能选择已结束的成功或部分成功任务'), 409
        try:
            client = machine_client(machine_id)
            # Older Workers silently ignore unknown fields and always delete.
            # Check support before enqueueing any retention request.
            if keep_pcap and client.list_extractions(limit=1).get('pcap_retention_supported') is not True:
                return jsonify(message='该 Worker 尚未支持 PCAP 保留策略，请升级后再提交'), 409
            result = client.submit_extraction(source_id, keep_pcap=keep_pcap, keep_pcap_count=keep_count)
            result['job'] = job
            return jsonify(result), 202
        except WorkerRequestError as exc:
            return worker_error(exc)

    @app.get('/api/v1/machines/<machine_id>/extractions/<extraction_id>')
    def get_extraction(machine_id, extraction_id):
        try:
            client = machine_client(machine_id)
            result = client.get_extraction(extraction_id)
            job = mapping(machine_id, result['source_task_id'])
            result['job'] = job
            key = (machine_id, extraction_id)
            if (job and job['status'] in {'SUCCEEDED', 'PARTIAL'}
                    and result['status'] in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED'}
                    and synced.get(key) != result['updated_at']):
                try:
                    source = client.get_result(result['source_task_id'])
                    if source.get('result') and source['status'] in {'SUCCEEDED', 'PARTIAL'}:
                        store.update_worker_results(job['job_id'], machine_id, top_status=source['status'],
                            units=source['result'].get('units', []), error=source.get('error'))
                        synced[key] = result['updated_at']
                except WorkerRequestError as exc:
                    result['sync_error'] = '原任务文件索引暂未同步，刷新详情可重试：' + str(exc)
            return jsonify(result)
        except WorkerRequestError as exc:
            return worker_error(exc)

    @app.post('/api/v1/machines/<machine_id>/extractions/<extraction_id>/cancel')
    def cancel_extraction(machine_id, extraction_id):
        try:
            return jsonify(machine_client(machine_id).cancel_extraction(extraction_id))
        except WorkerRequestError as exc:
            return worker_error(exc)
