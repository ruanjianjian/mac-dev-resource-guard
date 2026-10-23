#!/usr/bin/env python3
"""Dashboard adapter. Only installed dev-* commands may inspect/reclaim resources.

No resource deletion, process signalling, scheduling or policy decisions live here.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

CACHE_NAMES = {
    'Intermediates.noindex': '编译中间文件', 'Index.noindex': 'Xcode 索引',
    'ModuleCache.noindex': '模块缓存', 'SDKStatCaches.noindex': 'SDK 状态缓存',
}
MEMORY_NAMES = {'gradle': 'Gradle 后台', 'mcp': 'MCP 后台',
                'simulator': '受管模拟器', 'tail': '托管日志'}
REASONS = {
    'explicitly pinned for acceptance/use': '已保护，等待验收或复测',
    'used within retention window': '近期使用，仍在保留期内',
    'build slot is locked': '正在构建，缓存槽已锁定',
    'awaiting_second_idle_check': '等待第二次闲置核对',
    'awaiting_second_check': '等待第二次闲置核对',
    'inspection_unavailable_preserved': '无法核实，保留',
    'pending_readonly_no_retry': '上次请求待核实，保留且不重发',
}


def read_json(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('后端结果过大，已停止展示')
    return json.loads(path.read_text())


def atomic_json(path, body):
    temp = path.with_suffix('.new')
    with open(temp, 'w', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(body, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def json_stream(text):
    decoder, bodies = json.JSONDecoder(), []
    text = text.strip()
    while text:
        body, length = decoder.raw_decode(text)
        if not isinstance(body, dict):
            raise ValueError('后端返回了无法识别的结果')
        bodies.append(body)
        text = text[length:].lstrip()
    if not bodies:
        raise ValueError('后端没有返回结果')
    return bodies


def rows_storage(plan):
    if plan.get('schema_version') != 1 or not isinstance(plan.get('candidates'), list):
        raise ValueError('不兼容的磁盘扫描结果，清理保持禁用')
    rows = []
    for raw in plan['candidates']:
        path = raw['path']
        category = CACHE_NAMES.get(Path(path).name, '其他受保护目录')
        eligible = raw.get('decision') == 'eligible' and Path(path).name in CACHE_NAMES
        reasons = raw.get('reasons', [])
        if not isinstance(reasons, list):
            raise ValueError('无效的磁盘保护结果')
        rows.append(dict(id=path, title=Path(path).name, detail=path, category=category,
                         bytes=int(raw.get('stats', {}).get('allocated_bytes', 0)),
                         eligible=eligible,
                         reason='；'.join(REASONS.get(str(r), str(r)) for r in reasons)
                         or ('符合后端保留期与占用检查' if eligible else '保留：状态不明')))
    return rows


def rows_memory(body):
    required = ('managed_resources', 'external_resources', 'actions', 'pending_requests')
    if any(not isinstance(body.get(k), list) for k in required) or type(body.get('paused')) is not bool:
        raise ValueError('不兼容的内存诊断结果，回收保持禁用')
    rows = []
    for raw in body['managed_resources'] + body['external_resources']:
        eligible = raw.get('eligible') is True and not body['paused']
        reason = str(raw.get('reason') or '状态不明，保留')
        rows.append(dict(id=str(raw['id']), title=str(raw.get('id')),
                         detail=('PID ' + str(raw['pid'])) if raw.get('pid') else '',
                         category=MEMORY_NAMES.get(raw.get('kind'), '其他受管后台'), bytes=None,
                         eligible=eligible,
                         reason='自动回收已暂停，保留' if body['paused'] else REASONS.get(reason, reason)))
    known_pids = {r.get('pid') for r in body['managed_resources'] + body['external_resources'] if r.get('pid')}
    for raw in body.get('top_memory', []):
        if raw.get('pid') in known_pids:
            continue
        # Process names and accounting only. Never expose argv, environment or accounts.
        rows.append(dict(id='process:' + str(raw['pid']), title=str(raw.get('role') or raw.get('name', '进程')),
                         detail='PID {} · MEM {}'.format(raw['pid'], raw.get('mem', '—')),
                         category='现有应用与开发任务', bytes=None, eligible=False,
                         reason=str(raw.get('policy', '归属未核实，保留'))))
    if body.get('protected_devices'):
        rows.append(dict(id='protected-devices', title='{} 台占用中的设备'.format(body['protected_devices']),
                         detail='', category='设备占用保护', bytes=None, eligible=False, reason='已有占用，保留'))
    for index, pending in enumerate(body['pending_requests']):
        rows.append(dict(id='pending:' + str(index), title='待核实请求', detail='',
                         category='待核实', bytes=None, eligible=False, reason='只读核实，不重复发送退出请求'))
    return rows


class Adapter:
    def __init__(self, home=None):
        self.home = Path(home) if home else Path.home()
        self.state = self.home / '.local/state/dev-resource-guard/dashboard'
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)

    def command(self, name, args):
        tool = self.home / '.local/bin' / name
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise ValueError('{} 未安装；请先安装或修复现有工具'.format(name))
        # Wait for cooperative backends; never terminate an in-flight operation on a UI timeout.
        result = subprocess.run([str(tool)] + args, capture_output=True, text=True, shell=False)
        bodies = json_stream(result.stdout)
        if result.returncode and not any('errors' in b for b in bodies):
            raise ValueError('{} 执行失败（退出码 {}），未自动重试'.format(name, result.returncode))
        return bodies

    def preview(self, family):
        operation = self.state / (family + '-operation.json')
        if operation.exists() and read_json(operation).get('status') in ('running', 'unknown'):
            return dict(status='unknown', message='上次操作仍在运行或结果待核实；请勿重复执行。', rows=[])
        token = str(uuid.uuid4())
        if family == 'storage':
            path = self.state / 'storage-plan.json'
            # A busy command must not accidentally reuse a previous output file.
            bodies = self.command('dev-storage', ['plan', '--output', str(path)])
            if any(b.get('status') == 'busy' for b in bodies):
                return dict(status='busy', message='已有磁盘扫描或维护正在运行，请稍后再试。', rows=[])
            if not any(b.get('phase') == 'plan_complete' for b in bodies):
                raise ValueError('扫描未完成；旧结果不会用作本次预览')
            body = read_json(path)
            rows = rows_storage(body)
            note = '仅扫描现有 dev-storage 管理的缓存范围。目录统计量不等于实际可释放空间。'
        else:
            body = self.command('dev-memory', ['inspect', '--json'])[-1]
            rows = rows_memory(body)
            note = '内存守护已暂停；此入口不会恢复调度。' if body['paused'] else '只诊断；闲置证据、冷却和占用保护由现有后端核对。'
        eligible = sum(r['eligible'] for r in rows)
        receipt = dict(family=family, token=token, created=time.time(), consumed=False, eligible=eligible)
        atomic_json(self.state / (family + '-preview.json'), receipt)
        return dict(status='ok', message=note, rows=rows, token=token, created=receipt['created'],
                    eligible=eligible, eligibleBytes=sum(r['bytes'] or 0 for r in rows if r['eligible']))

    def clean(self, family, token):
        preview = read_json(self.state / (family + '-preview.json'))
        if (not token or token != preview.get('token') or preview.get('family') != family
                or preview.get('consumed') or not 0 <= time.time() - preview['created'] <= 300):
            raise ValueError('预览已过期或已执行，请重新扫描后确认')
        if not preview.get('eligible'):
            raise ValueError('没有符合回收条件的项目')
        preview['consumed'] = True
        atomic_json(self.state / (family + '-preview.json'), preview)
        operation = self.state / (family + '-operation.json')
        atomic_json(operation, dict(status='running', at=time.time()))
        try:
            if family == 'storage':
                bodies = self.command('dev-storage', ['maintain', '--apply', '--scan'])
                result = bodies[-1]
                status = result.get('status')
                if status == 'busy':
                    answer = dict(status='busy', message='已有扫描或维护正在运行，本次未执行；请稍后重新预览。')
                elif isinstance(result.get('deleted'), list) and isinstance(result.get('kept'), list) and isinstance(result.get('errors'), list):
                    delta = result.get('observed_free_delta_bytes', 0) / 1073741824
                    rows = []
                    for key, category, outcome in [('deleted', '已清理', '已清理'), ('kept', '复核保留', '保留'), ('errors', '清理失败', '失败')]:
                        for index, item in enumerate(result[key]):
                            rows.append(dict(id=key + ':' + str(index), title=Path(item.get('path', '缓存')).name,
                                             detail=str(item.get('path', '')), category=category, eligible=False,
                                             bytes=item.get('directory_accounting_bytes'), outcome=outcome,
                                             reason=str(item.get('reason') or '后端已确认删除；下次构建可重新生成')))
                    answer = dict(status='error' if result['errors'] else 'ok',
                                  rows=rows,
                                  message='清理 {} 项，复核保留 {} 项，失败 {} 项；磁盘净变化 {:+.2f} GiB。'.format(
                                      len(result['deleted']), len(result['kept']), len(result['errors']), delta))
                else:
                    raise ValueError('缺少清理完成回执，结果待核实')
            else:
                result = self.command('dev-memory', ['clean', '--json'])[-1]
                rows_memory(result)  # Validate required outcome fields, not transport success.
                stopped = sum(a.get('result', {}).get('stopped') is True for a in result['actions'])
                waiting = len(result['pending_requests'])
                rows = []
                for index, action in enumerate(result['actions']):
                    outcome = action.get('result', {})
                    exited = outcome.get('stopped') is True
                    pending = not exited and any(outcome.get(k) is True for k in ('accepted', 'pending', 'unknown'))
                    rows.append(dict(id='action:' + str(index), title=str(action.get('id') or action.get('kind') or '开发后台'),
                                     detail='', category='回收结果', bytes=None, eligible=False,
                                     outcome='已退出' if exited else ('待核实' if pending else '保留'),
                                     reason=REASONS.get(str(outcome.get('reason')), str(outcome.get('reason') or ('确认正常退出' if exited else '未确认退出，保留并只读核实')))))
                answer = dict(status='ok', rows=rows, message='正常退出 {} 项，待核实 {} 项。{}'.format(stopped, waiting,
                              '守护已暂停，本次没有回收。' if result['paused'] else '未通过复核或处于冷却中的资源保留。'))
            atomic_json(operation, answer)
            return answer
        except (OSError, ValueError, KeyError, TypeError) as error:
            atomic_json(operation, dict(status='unknown', at=time.time()))
            return dict(status='unknown', message='操作结果待核实，已禁止重发。' + str(error))

    def run(self, action, token=None):
        family, verb = action.split('-')
        with open(self.state / 'operation.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(status='busy', message='资源看板已有操作正在运行，请稍后再试。', rows=[])
            return self.preview(family) if verb == 'preview' else self.clean(family, token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['storage-preview', 'storage-clean', 'memory-preview', 'memory-clean'])
    parser.add_argument('--token')
    parser.add_argument('--home', type=Path, help='Explicit isolated test home; no command PATH fallback')
    args = parser.parse_args()
    try:
        result = Adapter(args.home).run(args.action, args.token)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = dict(status='error', message=str(error), rows=[])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
