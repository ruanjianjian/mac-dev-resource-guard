#!/usr/bin/env python3
"""Dashboard adapter: installed tools first, bundled runtime for standalone use.

Resource policies and filesystem deletion live in the runtime. Setup mutations
are explicit UI actions, never part of polling or capability inspection.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_status import engine_root, tool_path, run_setup, status as setup_status, inventory

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
    'cleanup_cooldown': '处于十分钟回收冷却期，请稍后重新诊断',
    'legacy_supervisor_paused_preserved': '旧托管进程不支持暂停时单次退出；保持暂停和进程，不发送未知请求。新建托管后台请使用 resource-guard-memory。',
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
        blocked = not body.get('manual_clean_supported')
        eligible = raw.get('eligible') is True and not blocked
        reason = str(raw.get('reason') or '状态不明，保留')
        rows.append(dict(id=str(raw['id']), title=str(raw.get('id')),
                         detail=('PID ' + str(raw['pid'])) if raw.get('pid') else '',
                         category=MEMORY_NAMES.get(raw.get('kind'), '其他受管后台'), bytes=None,
                         eligible=eligible,
                         reason='现有后端不支持独立单次回收，请升级后端；自动暂停保持不变' if blocked else REASONS.get(reason, reason)))
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


KIND_TITLES = {'simulator-delete': '删除模拟器', 'simulator-shutdown': '关闭模拟器', 'task-delete': '删除任务目录',
               'runtime-delete': '删除运行时', 'move': '搬到外接盘', 'prune': '删构建产物', 'tool-cache': '清工具缓存',
               'installer-delete': '删除安装包', 'trash-empty': '清空', 'device-support-delete': '删除',
               'archive-delete': '删除', 'cache-move': '缓存搬到外接盘'}
OUTCOMES = {'pending': '待确认', 'failed': '上次失败', 'ignored': '已忽略', 'done': '已完成', 'aborted': '已放弃', 'skipped': '已跳过'}
TIER_STATUS = {
    'disabled': '自动档未打开：在「接入设置」打开后，空间低于提醒线时会自动搬迁闲置任务目录、删可重新生成的构建产物。',
    'healthy': '空间充足（剩 {free_before} GiB，高于提醒线 {warning_free_gib} GiB），本轮不处理。',
    'busy': '你正在看板里清理，本轮让路。',
    'config_error': '设置有误，本轮未清理：{config_error}',
    'error': '本轮出错：{error}',
}


def tier_modules():
    for folder in ('runtime/storage', 'scripts'):
        if str(engine_root() / folder) not in sys.path:
            sys.path.insert(0, str(engine_root() / folder))
    import ask_tier, auto_tier, tiers_config
    return ask_tier, auto_tier, tiers_config


def checkup_modules():
    tier_modules()
    import checkup, toolcaches
    return checkup, toolcaches


SUGGESTION_TITLES = {'caches': '共享构建缓存', 'clean': '自动清理', 'external': '使用外接盘'}


def gb(value):
    if value is None:
        return '读不到'
    return '{:.1f} TB'.format(value / 1099511627776) if value >= 1099511627776 else '{:.0f} GB'.format(value / 1073741824)


def short(home, path):
    path = str(path)
    return '~' + path[len(str(home)):] if path.startswith(str(home) + '/') else path


def rows_checkup(home, cfg, report, freed, gone):
    """The checkup page in rows; the panel lays them out by category."""
    checkup, _ = checkup_modules()
    rows = []
    for drive in gone:
        what = '、'.join(drive['caches']) or '没有缓存'
        rows.append(dict(id='gone:' + drive['volume'], title='外接盘「{}」未连接'.format(drive['name']), category='提醒',
                         reason='暂时不能用：{}；{} 个任务在上面。用到它们的构建会报「盘未连接」类错误，插回即可。'.format(what, drive['tasks']),
                         detail=drive['volume'], bytes=None, eligible=False, command=None, outcome='未连接'))
    states = checkup.suggestion_state(home, cfg, report)
    effect = dict(caches=sum(c['bytes'] or 0 for c in report['caches'] if c['status'] == 'local') if (cfg['external_root'] or report.get('recommended')) else None,
                  clean=None, external=None)
    drive = next((d for d in report['drives'] if cfg['external_root'] and str(cfg['external_root']).startswith(d['path'] + '/')),
                 report.get('recommended'))
    tools = len({c['tool'] for c in report['caches']})
    short_lines = dict(
        caches='{} 个工具 · Xcode 编译缓存'.format(tools),
        clean='仅过期编译中间缓存 · 保留任务和模拟器',
        external=('未连接' if gone else '剩 ' + gb(drive['free'])) if cfg['external_root'] and drive
        else ('未连接' if cfg['external_root'] else ('用「{}」，剩 {}'.format(drive['name'], gb(drive['free'])) if drive else '没插外接盘')))
    for key in checkup.SUGGESTIONS:
        state = states[key]
        rows.append(dict(id=key, kind='suggestion', title=SUGGESTION_TITLES[key], category='建议', reason=state['detail'], detail=short_lines[key],
                         bytes=effect[key] or None, eligible=state['available'], command=None,
                         outcome='已开启' if state['on'] else ('未开启' if state['available'] else '不可用')))
    for root in report['roots']:
        external = next((str(r['external']) for r in cfg['task_roots'] if str(r['path']) == root['path'] and r['external']), None)
        rows.append(dict(id='root:' + root['path'], title=short(home, root['path']), category='任务目录', bytes=None, eligible=False,
                         command=None, outcome='{} 个任务'.format(root['tasks']),
                         reason='只扫描，收尾和删除先确认',
                         detail=''))
    for drive in report['drives']:
        used = cfg['external_root'] is not None and str(cfg['external_root']).startswith(drive['path'] + '/')
        rows.append(dict(id='drive:' + drive['path'], title=drive['name'], category='外接盘', bytes=drive['free'], eligible=drive['usable'],
                         command=None, outcome='在用' if used else ('推荐' if report.get('recommended') and report['recommended']['path'] == drive['path'] else ('可用' if drive['usable'] else '不可用')),
                         reason=('剩余空间 ' + gb(drive['free'])) if drive['usable'] else drive['reason'], detail=drive['path']))
    for item in report['categories']:
        rows.append(dict(id='space:' + item['id'], title=item['title'], category='空间去哪了', bytes=item['bytes'], eligible=False,
                         command=None, outcome=item['where'], reason=item['handling'] + ('；' + item['note'] if item['note'] else ''),
                         detail=''))
    for cache in report['caches']:
        status = dict(moved='已搬到外接盘', linked='已是链接', custom='你改过位置', local='在内置盘', stays='留在内置盘')[cache['status']]
        rows.append(dict(id='cache:' + cache['key'], title=cache['name'], category='工具缓存', bytes=cache['bytes'], eligible=False,
                         command=None, outcome=status, reason=cache['note'] or short(home, cache['path']),
                         detail=short(home, cache['path']) + (' → ' + cache['dest'] if cache.get('dest') else '')))
    return rows


def rows_pending(pending):
    rows = []
    for item in pending.get('items', []):
        if item['state'] not in ('pending', 'failed', 'ignored'):
            continue
        reason = item['reason'] + ('；上次失败：' + str(item.get('result')) if item['state'] == 'failed' else '')
        rows.append(dict(id=item['id'], kind=item['kind'], title=KIND_TITLES[item['kind']] + '：' + item['title'],
                         category='待确认', reason=reason, detail='会失去：' + item['loses'], loses=item['loses'],
                         bytes=item.get('bytes'), eligible=not item.get('unreachable'), command=None,
                         outcome='外接盘未连接' if item.get('unreachable') else OUTCOMES[item['state']]))
    return rows


def rows_auto(home):
    root = home / '.local/state/dev-resource-guard'
    receipt = read_json(root / 'auto-tier.json') if (root / 'auto-tier.json').is_file() else None
    if receipt is None:
        rows = [dict(id='auto-status', title='自动档', category='自动处理记录', reason='还没有运行过。', detail='',
                     bytes=None, eligible=False, command=None, outcome='未运行')]
        return rows + rows_log(root)
    when = time.strftime('%m-%d %H:%M', time.localtime(receipt['at']))
    status = receipt['status']
    done = [a for a in receipt.get('actions', []) if a['result'] == 'done']
    reason = TIER_STATUS[status].format(**{k: receipt.get(k) for k in ('free_before', 'warning_free_gib', 'config_error', 'error')}) \
        if status in TIER_STATUS else '{} 项完成，可用空间 {} → {} GiB。'.format(len(done), receipt.get('free_before'), receipt.get('free_after'))
    if status == 'managed-caches':
        reason = receipt.get('reason', '仅维护过期编译中间缓存')
    rows = [dict(id='auto-status', title='自动档 · ' + when, category='自动处理记录', reason=reason, detail='',
                 bytes=sum(a.get('bytes') or 0 for a in done) or None, eligible=False, command=None,
                 outcome={'complete': '已完成', 'partial': '部分失败', 'disabled': '未打开', 'healthy': '空间充足',
                          'managed-caches': '安全维护', 'busy': '让路', 'config_error': '设置有误', 'error': '失败'}.get(status, status))]
    for denied in receipt.get('external_denied', []):
        rows.append(dict(id='denied:' + denied['path'], title='外接盘没有访问权限', category='自动处理记录', reason=denied['reason'] + '；此为旧版记录，当前版本不自动删除任务产物',
                         detail=denied['path'], bytes=None, eligible=False, command=None, outcome='需要授权'))
    for path in receipt.get('external_missing', []):
        rows.append(dict(id='external:' + path, title='外接开发盘未连接', category='自动处理记录', reason='旧版记录；当前版本保留任务产物：' + path,
                         detail=path, bytes=None, eligible=False, command=None, outcome='提示'))
    rows += rows_log(root)
    for index, kept in enumerate(receipt.get('kept', [])):
        rows.append(dict(id='kept:{}'.format(index), title='保留：' + Path(kept['path']).name, category='保留原因', reason=kept['reason'],
                         detail=kept['path'], bytes=None, eligible=False, command=None, outcome='保留'))
    return rows


def rows_log(root):
    """Every action taken, newest first: tier moves and prunes, and shared-cache moves."""
    rows = []
    log = root / 'auto-tier-log.jsonl'
    entries = [json.loads(line) for line in log.read_text().splitlines()[-200:] if line.strip()] if log.is_file() else []
    for entry in reversed(entries):
        for index, action in enumerate(entry.get('actions', [])):
            stamp = time.strftime('%m-%d %H:%M', time.localtime(entry['at']))
            rows.append(dict(id='auto:{}:{}'.format(entry['at'], index), title='{}：{}'.format(KIND_TITLES.get(action['kind'], action['kind']), Path(action['path']).name or action['path']),
                             category='自动处理记录', reason=action.get('reason', ''), detail=stamp + ' · ' + action['path'] + (' → ' + action['dest'] if action.get('dest') else ''),
                             bytes=action.get('bytes') or None, eligible=False, command=None, outcome=OUTCOMES.get(action['result'], action['result'])))
    return rows


def fixture_launchctl(home):
    """Isolated tests record environment changes in a file instead of the real login session."""
    record = home / '.fixture-launchctl.json'
    def launchctl(*args):
        env = json.loads(record.read_text()) if record.is_file() else {}
        out = ''
        if args[0] == 'getenv':
            out = env.get(args[1], '')
        elif args[0] == 'setenv':
            env[args[1]] = args[2]
        elif args[0] == 'unsetenv':
            env.pop(args[1], None)
        record.write_text(json.dumps(env))
        return subprocess.CompletedProcess(args, 0, out + '\n' if out else '', '')
    return launchctl


def agent_app(preferred):
    """The agent App to open: the one the agent recorded at install time, else the first one found."""
    apps = [('claude', p) for p in ('/Applications/Claude.app', str(Path.home() / 'Applications/Claude.app'))] + \
           [('codex', p) for p in ('/Applications/ChatGPT.app', '/Applications/Codex.app', str(Path.home() / 'Applications/Codex.app'))]
    found = [a for a in apps if Path(a[1]).is_dir()]
    return next((a for a in found if a[0] == preferred), found[0] if found else None)


def app_version():
    """The installed app's version; 'dev' when run from the source tree."""
    info = Path(__file__).resolve().parent.parent / 'Info.plist'
    try:
        import plistlib
        body = plistlib.loads(info.read_bytes())
        return '{}-{}'.format(body.get('CFBundleShortVersionString'), body.get('CFBundleVersion'))
    except (OSError, ValueError):
        return 'dev'


def fixture_simctl(home):
    """Isolated UI tests read listings from the fixture and never touch real simulators."""
    def simctl(arguments):
        if arguments[:2] in (['list', 'devices'], ['runtime', 'list']):
            path = home / ('.fixture-simctl-' + arguments[0] + '.json')
            return path.read_text() if path.is_file() else ('{"devices": {}}' if arguments[0] == 'list' else '{}')
        raise RuntimeError('隔离界面不操作真实模拟器')
    return simctl


class Adapter:
    def __init__(self, home=None):
        self.home = Path(home) if home else Path.home()
        self.isolated = home is not None
        self.state = self.home / '.local/state/dev-resource-guard/dashboard'
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.state.parent / 'standalone.json'
        if not marker.exists() and not any((self.home / '.local/state' / name).exists() for name in ('dev-memory', 'dev-storage')):
            atomic_json(marker, {'schema': 1, 'home': str(self.home.resolve())})

    def command(self, name, args):
        tool = self.home / '.local/bin' / name
        # Completing installation opts into the bundled protocol without replacing
        # legacy commands. Both implementations retain the same protection state.
        active = (self.home / '.local/state/dev-resource-guard/installation.json').is_file()
        if active:
            command = [sys.executable, str(tool_path(name))]
        elif tool.exists() or tool.is_symlink():
            if not tool.is_file() or not os.access(tool, os.X_OK):
                raise ValueError('{} 现有命令不可执行，请先修复；未绕过现有工具'.format(name))
            command = [str(tool)]
        else:
            command = [sys.executable, str(tool_path(name))]
        # Wait for cooperative backends; never terminate an in-flight operation on a UI timeout.
        env = dict(os.environ)
        if self.isolated:
            env['HOME'] = str(self.home)
            env.pop('DEV_RESOURCE_CONFIG', None)
            env.pop('DEV_MEMORY_STATE_DIR', None)
            env.pop('DEV_MEMORY_BROKER_SOURCE', None)
            if name == 'dev-memory' and args[0] in ('inspect', 'clean-once'):
                args = args + ['--owned-only']
        result = subprocess.run(command + args, capture_output=True, text=True, shell=False, env=env)
        bodies = json_stream(result.stdout)
        if result.returncode and not any('errors' in b or b.get('status') == 'rejected' for b in bodies):
            raise ValueError('{} 执行失败（退出码 {}），未自动重试'.format(name, result.returncode))
        return bodies

    def preview(self, family):
        operation = self.state / (family + '-operation.json')
        if operation.exists() and read_json(operation).get('status') in ('running', 'unknown'):
            return dict(status='unknown', message='上次操作仍在运行或结果待核实；请勿重复执行。', rows=[])
        token = str(uuid.uuid4())
        if family == 'common':
            sys.path.insert(0, str(engine_root() / 'runtime/storage'))
            import common
            body = common.preview(self.home)
            atomic_json(self.state / 'common-plan.json', body)
            rows = rows_storage(body)
            settings = self.state / 'task.json'
            task = read_json(settings).get('path') if settings.exists() else None
            rows += inventory(self.home, task)
            errors = body.get('activity_errors', [])
            note = '已有 Xcode 缓存：只清理至少 7 天未用的四类中间目录；Products、源码、设备及会话保留。'
            if errors:
                note += ' 占用检查不完整，清理禁用：' + '；'.join(errors)
            elif not body['candidates']:
                note += ' 默认 DerivedData 没有匹配项；这不表示磁盘没有其他占用。'
        elif family == 'storage':
            path = self.state / 'storage-plan.json'
            # A busy command must not accidentally reuse a previous output file.
            bodies = self.command('dev-storage', ['plan', '--output', str(path)])
            if any(b.get('status') == 'busy' for b in bodies):
                return dict(status='busy', message='已有磁盘扫描或维护正在运行，请稍后再试。', rows=[])
            if not any(b.get('phase') == 'plan_complete' for b in bodies):
                raise ValueError('扫描未完成；旧结果不会用作本次预览')
            body = read_json(path)
            body['manual_apply_supported'] = any(b.get('manual_apply_supported') is True for b in bodies)
            rows = rows_storage(body)
            if not body['manual_apply_supported']:
                for row in rows:
                    if row['eligible']:
                        row.update(eligible=False, reason='现有工具不支持精确预览清理；请在接入设置升级本工具，其他实现由原工具升级。')
            note = '仅扫描现有 dev-storage 管理的缓存范围。目录统计量不等于实际可释放空间。'
        else:
            body = self.command('dev-memory', ['inspect', '--json'])[-1]
            rows = rows_memory(body)
            note = '单次回收不改变自动维护开关；闲置证据、冷却和占用保护仍会复核。'
            if not body.get('manual_clean_supported'):
                note = '现有后端版本不支持限定预览对象的单次回收，需升级；不会替您恢复自动维护。'
            if not any(r['eligible'] for r in rows):
                note += ' 当前没有已证实闲置的后台。未接入设备占用或 MCP 协议的资源会保留，详情见接入设置。'
        eligible = sum(r['eligible'] for r in rows)
        created = body.get('created', time.time()) if family in ('storage', 'common') else time.time()
        receipt = dict(family=family, token=token, created=created, consumed=False, eligible=eligible,
                       ids=[r['id'] for r in rows if r['eligible']], manual=body.get('manual_clean_supported', False))
        receipt['manual_storage'] = body.get('manual_apply_supported', False)
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
            if family in ('storage', 'common'):
                if family == 'common':
                    sys.path.insert(0, str(engine_root() / 'runtime/storage'))
                    import common
                    plan = read_json(self.state / 'common-plan.json')
                    plan['candidates'] = [c for c in plan['candidates'] if c['path'] in preview['ids']]
                    result = common.apply(self.home, plan)
                else:
                    if not preview.get('manual_storage'):
                        raise ValueError('旧后端不支持限定预览的清理，请升级后重新扫描')
                    args = ['apply-plan', '--input', str(self.state / 'storage-plan.json')]
                    bodies = self.command('dev-storage', args)
                    result = bodies[-1]
                status = result.get('status')
                if status == 'busy':
                    answer = dict(status='busy', message='已有扫描或维护正在运行，本次未执行；请稍后重新预览。')
                elif status == 'rejected':
                    answer = dict(status='error', message=str(result.get('message', '后端已拒绝，未执行；请重新扫描。')))
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
                if not preview.get('manual'):
                    raise ValueError('旧后端不支持单次回收，请升级后重新诊断')
                args = ['clean-once', '--json']
                if preview.get('manual'):
                    for resource in preview['ids']:
                        args += ['--id', resource]
                result = self.command('dev-memory', args)[-1]
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
                              '自动维护仍暂停。' if result['paused'] and result.get('manual') else '未通过复核或处于冷却中的资源保留。'))
            atomic_json(operation, answer)
            return answer
        except (OSError, ValueError, KeyError, TypeError) as error:
            atomic_json(operation, dict(status='unknown', at=time.time()))
            return dict(status='unknown', message='操作结果待核实，已禁止重发。' + str(error))

    def tiers(self, family, verb, token, ids):
        ask_tier, auto_tier, tiers_config = tier_modules()
        if family == 'auto':
            return dict(status='ok', message='自动档每次处理了什么、为什么保留，都记在这里。', rows=rows_auto(self.home))
        receipt_path = self.state / 'pending-preview.json'
        if verb == 'ignore':
            ask_tier.ignore(self.home, set(ids))
        if verb in ('preview', 'ignore'):
            rows = rows_pending(ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending()))
            token = str(uuid.uuid4())
            atomic_json(receipt_path, dict(token=token, created=time.time(), ids=[r['id'] for r in rows if r['eligible']]))
            message = '{} 项等你确认。勾选后确认，执行前会逐项再核对。'.format(sum(r['outcome'] == '待确认' for r in rows)) if rows else '没有需要你确认的清理。'
            eligible = [r for r in rows if r['eligible']]
            return dict(status='ok', message=message, rows=rows, token=token, created=time.time(),
                        eligible=len(eligible), eligibleBytes=sum(r.get('bytes') or 0 for r in eligible))
        preview = read_json(receipt_path) if receipt_path.is_file() else {}
        receipt_path.unlink(missing_ok=True)   # one confirmation per preview
        if (not token or token != preview.get('token') or time.time() - preview.get('created', 0) > 300
                or not ids or not set(ids) <= set(preview.get('ids', []))):
            return dict(status='rejected', message='确认已过期或勾选项不在列表里，未执行；请重新打开待确认。', rows=[])
        sys.path.insert(0, str(engine_root() / 'scripts'))
        from service import operation_lock
        from portable_config import configure
        settings = configure(self.home)
        cfg = tiers_config.load(self.home, owned=[settings.cache_roots[0], settings.state_root, self.state.parent])
        simctl = fixture_simctl(self.home) if self.isolated else auto_tier.default_simctl
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(operation_lock(self.home, 'periodic-storage'))
            except ValueError:
                return dict(status='busy', message='后台清理正在运行，未执行；请稍后再确认。',
                            rows=rows_pending(ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())))
            results = ask_tier.execute(self.home, cfg, settings, list(ids), simctl)
        rows = [dict(id=r['id'], title=r.get('title', r['id']), category='执行结果', reason=r['reason'], detail='',
                     bytes=r.get('bytes'), eligible=False, command=None, outcome=r['outcome']) for r in results]
        done = sum(r['outcome'] == '已完成' for r in results)
        return dict(status='ok', message='已完成 {} 项，跳过或失败 {} 项。'.format(done, len(results) - done), rows=rows)

    def checkup(self, verb, ids, path):
        checkup, toolcaches = checkup_modules()
        _, auto_tier, tiers_config = tier_modules()
        sys.path.insert(0, str(engine_root() / 'scripts'))
        from portable_config import configure
        settings = configure(self.home)
        owned = [settings.cache_roots[0], settings.state_root, self.state.parent]
        cfg = tiers_config.load(self.home, owned=owned)
        volumes, info, is_mount = Path('/Volumes'), checkup.discover.disk_info, os.path.ismount
        launchctl = toolcaches.default_launchctl
        if self.isolated:
            volumes = self.home / 'Volumes'
            fixture = self.home / '.fixture-diskutil.json'
            details = json.loads(fixture.read_text()) if fixture.is_file() else {}
            info = lambda mount: details.get(Path(mount).name, {})
            is_mount = lambda mount: Path(mount).name in details
            launchctl = fixture_launchctl(self.home)
        if verb == 'status':   # a drive was unplugged: what does it take away?
            gone = [d for d in checkup.disconnected(self.home, cfg, is_mount) if not path or d['volume'] == path]
            rows = [dict(id='gone:' + d['volume'], title='外接盘「{}」已断开'.format(d['name']), category='提醒',
                         reason='暂时不能用：{}；{} 个任务在上面。用到它们的构建会报错，插回即可。'.format('、'.join(d['caches']) or '没有缓存', d['tasks']),
                         detail=d['volume'], bytes=None, eligible=False, command=None, outcome='未连接') for d in gone]
            return dict(status='ok' if rows else 'skip', message=rows[0]['reason'] if rows else '', rows=rows)
        notes = []
        if verb == 'apply':
            cfg, notes = checkup.apply(self.home, ids, owned, launchctl=launchctl)
            if not self.isolated:
                subprocess.run(['/bin/launchctl', 'kickstart', 'gui/{}/local.dev-resource-guard.storage'.format(os.getuid())],
                               capture_output=True)
        elif verb == 'off':
            notes = [checkup.turn_off(self.home, ids[0] if ids else '', owned, launchctl=launchctl)]
            cfg = tiers_config.load(self.home, owned=owned)
        report = checkup.load_report(self.home)
        version = app_version()
        if verb == 'firstrun' and report and report.get('notified_version') == version:
            return dict(status='skip', message='', rows=[])
        fresh = report is not None and time.time() - report.get('at', 0) < 600
        if verb == 'scan' or verb == 'firstrun' and not fresh or report is None or time.time() - report.get('at', 0) > 6 * 3600:
            # Only a scan started from the open panel may touch Downloads (macOS asks about it once).
            report = checkup.scan(self.home, cfg, volumes=volumes, info=info, is_mount=is_mount, foreground=verb in ('scan', 'preview'))
        elif verb in ('apply', 'off'):
            report['caches'] = toolcaches.inspect(self.home, cfg['external_root'], touch_external=False)
        ask_tier, _, _ = tier_modules()
        pending = ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())
        pending_bytes = sum(i.get('bytes') or 0 for i in pending['items'] if i['state'] == 'pending')
        freed = checkup.estimate(report, cfg, pending_bytes)
        if verb == 'firstrun':
            report['notified_version'] = version
            atomic_json(checkup.report_path(self.home), report)
            return dict(status='ok', message=checkup.first_run_message(report, freed), rows=[], eligibleBytes=freed)
        message = ''
        if freed >= 1073741824 and not all(checkup.suggestion_state(self.home, cfg, report)[k]['on'] for k in checkup.SUGGESTIONS):
            message += '按推荐全部开启后，预计可腾出约 {}。'.format(gb(freed))
        if notes:
            message += '；'.join(notes)
        rows = rows_checkup(self.home, cfg, report, freed, checkup.disconnected(self.home, cfg, is_mount))
        return dict(status='ok', message=message, rows=rows, eligibleBytes=freed,
                    created=report.get('at'))

    def plan(self, verb, value):
        """space-target-plan and agent-handoff: the user's target, the three-part plan, handing work to the agent."""
        _, auto_tier, tiers_config = tier_modules()
        sys.path.insert(0, str(engine_root() / 'scripts'))
        import service, plan as space_plan
        from portable_config import configure
        settings = configure(self.home)
        owned = [settings.cache_roots[0], settings.state_root, self.state.parent]
        cfg = tiers_config.load(self.home, owned=owned)
        if verb == 'handoff':
            app = agent_app(cfg['agent_app'])
            name = {'claude': 'Claude', 'codex': 'ChatGPT（Codex）'}.get(app[0], '你的 agent') if app else '你的 agent'
            return dict(status='ok' if app else 'missing', message=space_plan.AGENT_PROMPT,
                        rows=[dict(id='app', title=name, detail=app[1] if app else '', category='agent', bytes=None,
                                   eligible=bool(app), reason='', command=None, outcome=None)])
        if verb == 'target':
            service.set_target(self.home, value)
            settings = configure(self.home)
        plan = service.current_plan(self.home, cfg, settings)
        if plan is None:
            return dict(status='ok', message='', rows=[])
        rows = [dict(id='target', title='目标 {:.0f} GB'.format(plan['target_gib']), category='目标', bytes=plan['gap_bytes'],
                     eligible=False, command=None, detail='', outcome='还差' if plan['gap_bytes'] else '已达到',
                     reason='全做完能到 {}'.format(gb(plan['reach_bytes'])))]
        for section in plan['sections']:
            rows.append(dict(id=section['id'], title=section['title'], category='计划', bytes=section['bytes'], eligible=False,
                             command=None, detail='', outcome=None, reason='、'.join(i['title'] for i in section['items'][:2])))
            for index, entry in enumerate(section['items']):
                rows.append(dict(id='{}:{}'.format(section['id'], index), title=entry['title'], category=section['id'],
                                 bytes=entry['bytes'] or None, eligible=False, command=None, detail=entry.get('path') or '',
                                 outcome=None, reason=entry['reason']))
        for index, entry in enumerate(plan['suggestions']):
            rows.append(dict(id='suggest:{}'.format(index), title=entry['title'], category='建议', bytes=entry['bytes'] or None,
                             eligible=False, command=None, detail='', outcome=None, reason=entry['reason']))
        message = '全做完能到 {}'.format(gb(plan['reach_bytes']))
        if plan['shortfall_bytes']:
            message += '，离目标还差 {}'.format(gb(plan['shortfall_bytes']))
        return dict(status='ok', message=message, rows=rows, eligibleBytes=plan['reach_bytes'], created=plan['at'])

    def run(self, action, token=None, path=None, ids=None):
        family, verb = action.split('-', 1)
        if family == 'plan':
            return self.plan(verb, path)
        if family == 'checkup' and verb in ('preview', 'scan', 'firstrun', 'status'):
            # Read-only: a long background checkup must not make the panel's buttons answer "busy".
            return self.checkup(verb, [], path)
        with open(self.state / 'operation.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(status='busy', message='资源看板已有操作正在运行，请稍后再试。', rows=[])
            if family in ('pending', 'auto'):
                return self.tiers(family, verb, token, [i for i in (ids or '').split(',') if i])
            if family == 'checkup':
                return self.checkup(verb, [i for i in (ids or '').split(',') if i], path)
            if family == 'setup' and verb in ('auto-enable', 'auto-disable', 'ask-enable', 'ask-disable'):
                _, _, tiers_config = tier_modules()
                kind, operation = verb.split('-')
                tiers_config.set_flag(self.home, kind + '_enabled', operation == 'enable')
                return setup_status(self.home)
            if family == 'setup':
                if self.isolated and verb not in ('preview', 'install', 'upgrade'):
                    raise ValueError('隔离界面不操作真实 launchd；调度使用独立测试服务验证。')
                if verb == 'activate':
                    result = subprocess.run([sys.executable, str(engine_root() / 'scripts/service.py'), 'activate'],
                                            capture_output=True, text=True)
                    if result.returncode: raise ValueError(result.stderr.strip())
                elif verb == 'install':
                    run_setup(self.home, ['install'])
                elif verb == 'upgrade':
                    run_setup(self.home, ['upgrade'])
                elif verb != 'preview':
                    kind, operation = verb.split('-')
                    if (self.home / '.local/state/dev-resource-guard/installation.json').is_file():
                        sys.path.insert(0, str(engine_root() / 'scripts'))
                        from service import set_enabled
                        set_enabled(self.home, kind, operation == 'enable')
                    else:
                        run_setup(self.home, [('enable' if operation == 'enable' else 'disable') + '-scheduler', kind])
                    if kind == 'memory' and not (self.home / '.local/state/dev-resource-guard/installation.json').is_file():
                        tool = self.home / '.local/bin/dev-memory'
                        result = subprocess.run([str(tool), 'resume' if operation == 'enable' else 'pause'], capture_output=True, text=True)
                        if result.returncode:
                            raise ValueError('调度配置已变化，但内存暂停状态更新失败，请刷新核对。')
                return setup_status(self.home)
            if family == 'task':
                target = Path(path or '').expanduser()
                if not path or not target.is_dir() or target.is_symlink():
                    raise ValueError('请选择真实的任务目录')
                atomic_json(self.state / 'task.json', dict(path=str(target.resolve())))
                return dict(status='ok', message='已保存任务目录；扫描已有缓存时一并统计，只提供核对命令。', rows=[])
            if family == 'project':
                sys.path.insert(0, str(engine_root() / 'scripts'))
                from service import integrate
                integrate(self.home, Path(path or ''))
                return setup_status(self.home)
            return self.preview(family) if verb == 'preview' else self.clean(family, token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['storage-preview', 'storage-clean', 'common-preview', 'common-clean', 'memory-preview', 'memory-clean',
                                          'setup-preview', 'setup-install', 'setup-upgrade', 'setup-memory-enable', 'setup-memory-disable',
                                          'setup-storage-enable', 'setup-storage-disable', 'setup-activate', 'task-select', 'project-integrate',
                                          'setup-auto-enable', 'setup-auto-disable', 'setup-ask-enable', 'setup-ask-disable',
                                          'pending-preview', 'pending-clean', 'pending-ignore', 'auto-preview',
                                          'checkup-preview', 'checkup-scan', 'checkup-apply', 'checkup-off', 'checkup-firstrun',
                                          'checkup-status', 'plan-preview', 'plan-target', 'plan-handoff'])
    parser.add_argument('--token')
    parser.add_argument('--path')
    parser.add_argument('--ids', help='Comma-separated pending ids the user ticked')
    parser.add_argument('--home', type=Path, help='Explicit isolated test home; no command PATH fallback')
    args = parser.parse_args()
    try:
        if args.home:
            os.environ['HOME'] = str(args.home.resolve())
            os.environ.pop('DEV_RESOURCE_CONFIG', None)
            os.environ.pop('DEV_MEMORY_STATE_DIR', None)
        result = Adapter(args.home).run(args.action, args.token, args.path, args.ids)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = dict(status='error', message=str(error), rows=[])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
