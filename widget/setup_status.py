"""Desktop capability checks and explicit setup actions; no automatic setup on launch."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from datetime import datetime


def row(key, title, reason, detail='', size=None, command=None):
    return dict(id=key, title=title, category='接入状态', reason=reason, detail=detail,
                bytes=size, eligible=False, command=command, outcome='说明')


def engine_root():
    bundled = Path(__file__).resolve().parent / 'engine'
    return bundled if bundled.is_dir() else Path(__file__).resolve().parents[1]


def tool_path(name):
    relative = {'dev-memory': 'runtime/memory/scripts/dev_memory.py', 'dev-storage': 'runtime/storage/cli.py'}
    return engine_root() / relative[name]


def run_setup(home, arguments):
    result = subprocess.run([sys.executable, str(engine_root() / 'scripts/setup.py'), '--home', str(home)] + arguments,
                            text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or '配置失败；未标记为已启用')
    return result.stdout.strip()


def loaded(home, kind):
    label = 'local.dev-resource-guard.' + kind
    path = home / 'Library/LaunchAgents' / (label + '.plist')
    if not path.exists():
        return False
    if home.resolve() != Path.home().resolve() or sys.platform != 'darwin':
        return False
    return subprocess.run(['/bin/launchctl', 'print', 'gui/{}/{}'.format(os.getuid(), label)],
                          capture_output=True).returncode == 0


def tier_rows(home):
    """Switches and state of the tiered cleanup; the settings file stays the single source."""
    for folder in ('runtime/storage', 'scripts'):
        if str(engine_root() / folder) not in sys.path:
            sys.path.insert(0, str(engine_root() / folder))
    import ask_tier, tiers_config
    settings_file = tiers_config.settings_path(home)
    edit = 'open -e ' + shlex.quote(str(settings_file))
    try:
        cfg = tiers_config.load(home)
    except ValueError as error:
        bad = row('tier-config', '分档清理设置', '设置有误，分档清理本轮不执行：{}'.format(error), str(settings_file), command=edit)
        bad['outcome'] = '设置有误'
        return [bad]
    roots = '；'.join(str(r['path']) + (' → 外接盘 ' + str(r['external']) if r['external'] else '') for r in cfg['task_roots'])
    auto = row('auto-tier', '自动档（不丢东西的清理）',
               ('已打开：' if cfg['auto_enabled'] else '未打开：打开后，') +
               '每 12 小时只清理至少 7 天未用的编译中间缓存；保留任务目录、模拟器、全局工具缓存和受保护内容。', str(settings_file), command=edit)
    auto['outcome'] = '已打开' if cfg['auto_enabled'] else '未打开'
    pending = ask_tier.load(home, ask_tier.PENDING, ask_tier.empty_pending())
    count = sum(1 for i in pending['items'] if i['state'] == 'pending' and not i.get('unreachable'))
    ask = row('ask-tier', '询问档（会丢东西的清理先问你）',
              ('已打开：' if cfg['ask_enabled'] else '未打开：') + '关机满 {} 天的模拟器、开着 {} 小时没人连的模拟器、闲置满 {} 天且已全部推送的任务目录、没人用的运行时，先通知你确认。当前 {} 项待确认。'.format(
                  cfg['ask']['simulator_unused_days'], cfg['ask']['simulator_unattached_hours'], cfg['ask']['task_idle_days'], count),
              str(settings_file), command=edit)
    ask['outcome'] = '已打开' if cfg['ask_enabled'] else '未打开'
    result = [auto, ask]
    error = home / '.local/state/dev-resource-guard/ask-error.json'
    if error.is_file():
        try:
            failed = row('ask-error', '询问档最近一轮', '扫描出错：' + json.loads(error.read_text()).get('error', ''))
            failed['outcome'] = '失败'
            result.append(failed)
        except (OSError, ValueError):
            pass
    return result


def status(home):
    sys.path.insert(0, str(engine_root() / 'scripts'))
    from service import FAMILIES, schedulers
    installation = home / '.local/state/dev-resource-guard/installation.json'
    active = installation.is_file()
    rows = [row('common', '已有 Xcode 缓存', '已内置：扫描默认 DerivedData 中四类中间缓存，无需改构建脚本；手动确认清理。')]
    rows[0]['outcome'] = '已内置'
    for name in ('dev-storage', 'dev-memory'):
        tool = home / '.local/bin' / name
        exists = tool.exists() or tool.is_symlink()
        rows.append(row(name, name, '看板使用内置完整协议；原命令、pin 与占用记录保留。' if active else
                        ('复用现有命令；完成安装可启用内置完整协议。' if exists else '使用 App 内置实现；完成安装后自动维护。'), str(tool)))
        rows[-1]['outcome'] = '内置引擎' if active else ('复用' if exists else '使用内置')
    paused = (home / '.local/state/dev-memory/paused').exists()
    for kind, title in (('memory', '自动内存回收'), ('storage', '每 12 小时磁盘维护')):
        jobs = schedulers(home, kind)
        enabled = any(j['loaded'] for j in jobs)
        is_paused = paused if kind == 'memory' else (home / '.local/state/dev-resource-guard/storage-paused').exists()
        reason = '已加载：' + '、'.join(j['label'] for j in jobs if j['loaded']) if enabled else '未启用：尚未加载定时任务'
        if kind == 'memory' and paused:
            reason += '；自动回收暂停，支持单次回收的后端仍可手动操作'
        if kind == 'storage':
            reason += ('；本工具维护包含托管槽和至少七天未用的已有 Xcode 中间缓存；任务目录和设备保留'
                       if any(j['ours'] for j in jobs) else '；旧调度清理范围未确认' if jobs else '；完成安装后覆盖托管槽和已有 Xcode 中间缓存')
        rows.append(row(kind, title, reason))
        rows[-1]['outcome'] = '暂停' if enabled and is_paused else ('已加载' if enabled else '未启用')
    tiers = tier_rows(home)
    ask_on = any(r['id'] == 'ask-tier' and r['outcome'] == '已打开' for r in tiers)
    rows.append(row('devices', '模拟器自动收尾',
                    '询问档已打开：久未使用的模拟器需要你在「待确认」里确认后才会关闭或删除；系统标为不可用的由自动档清理。' if ask_on
                    else '已检测到 devlease；实际可用性与占用须在诊断时复核。' if (home / '.local/bin/devlease').is_file()
                    else '未接入设备占用协议：不会关闭或删除模拟器。磁盘页仍可查看其占用。'))
    rows.extend(tiers)
    rows.append(row('broker', 'MCP 自动收尾', '需要兼容 broker；未接入的实例保留，不强杀。'))
    if active:
        info = json.loads(installation.read_text())
        rows.append(row('installation', '安装结果', info.get('error', '完整引擎已安装；调度状态以上方实际检查为准。'), info.get('runtime', '')))
        rows[-1]['outcome'] = {'active':'已完成','partial':'部分完成','configuring':'正在配置'}.get(info.get('status'),'未知')
    auto = home / '.local/state/dev-resource-guard/automatic-storage.json'
    failure = home / '.local/state/dev-resource-guard/storage-scheduler.err'
    data = None
    if auto.is_file():
        try:
            data = json.loads(auto.read_text())
        except (OSError, ValueError):
            data = dict(at=auto.stat().st_mtime, status='unreadable', results=[], error='回执不可读，不视为成功')
    # A run that dies before writing a receipt only reaches launchd's error log.
    crashed = (failure.is_file() and failure.stat().st_size > 0 and
               (data is None or failure.stat().st_mtime > data.get('at', 0) + 60))
    if crashed:
        with failure.open('rb') as stream:  # launchd only appends; the tail is enough
            stream.seek(max(0, failure.stat().st_size - 4096))
            lines = [line for line in stream.read().decode(errors='replace').splitlines() if line.strip()]
        when = datetime.fromtimestamp(failure.stat().st_mtime).astimezone().strftime('%m-%d %H:%M')
        rows.append(row('automatic-last', '自动清理最近结果', '定时清理报错（{}）：{}'.format(when, lines[-1][-300:] if lines else '无输出'), str(failure)))
        rows[-1]['outcome'] = '失败'
    elif data is not None:
        results = data.get('results', [])
        completed = sum(len(r.get('result', {}).get('deleted', [])) for r in results)
        amount = sum(e.get('directory_accounting_bytes', 0) for r in results for e in r.get('result', {}).get('deleted', []))
        when = datetime.fromtimestamp(data['at']).astimezone().strftime('%m-%d %H:%M')
        reason = '{} 项完成；{}；大小为目录计量。'.format(completed, when)
        if data.get('error'):
            reason += '失败原因：' + data['error'] + '。'
        skipped = ['{}已跳过：{}'.format(FAMILIES.get(r['family'], r['family']), r['skipped']) for r in results if r.get('skipped')]
        reason += '；'.join(skipped)
        rows.append(row('automatic-last', '自动清理最近结果', reason, size=amount))
        outcome = {'complete':'已完成','running':'运行中','paused':'已暂停','error':'失败','busy':'等待空闲','unreadable':'失败'}.get(data.get('status'),'未知')
        if outcome == '已完成' and skipped:
            outcome = '已跳过' if len(skipped) == len(results) else '部分跳过'
        rows[-1]['outcome'] = outcome
    project = home / '.local/state/dev-resource-guard/project.json'
    if project.is_file():
        info = json.loads(project.read_text())
        settings = __import__('portable_config').configure(home)
        manifests = []
        for p in (settings.state_root / 'slots').glob('*.json'):
            value = json.loads(p.read_text())
            if value.get('worktree') == info['path']: manifests.append(value)
        builds = sum(m.get('successful_builds', 0) for m in manifests)
        reused = any(m.get('successful_builds', 0) >= 2 for m in manifests)
        rows.append(row('project', '项目构建缓存', '已验证同槽重复构建' if reused else
                        '入口已生成；成功构建 {} 次，尚未验证重复复用。已有脚本需调用此入口。'.format(builds), info['path'],
                        command=shlex.quote(info['wrapper']) + ' --platform ios-simulator --configuration Debug -- xcodebuild <原构建参数>'))
        rows[-1]['outcome'] = '复用已验证' if reused else '待构建验证'
    for kind, path in (('磁盘', home / '.local/state/dev-storage/last-maintenance.json'),
                       ('内存', home / '.local/state/dev-memory/last-cleanup.json')):
        if path.is_file():
            try:
                body = json.loads(path.read_text())
                count = len(body.get('deleted', [])) if kind == '磁盘' else sum(a.get('result', {}).get('stopped') is True for a in body.get('actions', []))
                rows.append(row(kind + '-last', kind + '最近回执', '{} 项完成；时间戳 {}。'.format(count, body.get('time', body.get('at', '未知')))))
            except (OSError, ValueError, TypeError):
                rows.append(row(kind + '-last', kind + '最近回执', '回执不可读，请先核实；不视为成功。'))
    return dict(status='ok', message='手动清理无需开启自动维护。首次使用先扫描，看清可清理项与保留原因。', rows=rows)


def inventory(home, task_path=None):
    roots = [(home / 'Library/Caches/go-build', 'Go 编译缓存', '由 Go 自己管理；本版仅统计，避免与未接入的 go build 并发清理。'),
             (home / 'Library/Developer/CoreSimulator/Devices', 'iOS 模拟器数据', '含 App 与登录态。关机释放内存，删除才释放磁盘；本入口不删除设备。'),
             (home / '.claude/projects', 'Claude 会话记录', '含会话历史，不作为临时缓存自动删除。')]
    if task_path:
        roots.append((Path(task_path), '任务目录', '先由任务拥有者确认提交、证据归档及占用释放；这里只提供核对命令，不删除。'))
    rows = []
    for path, title, reason in roots:
        size = None
        if path.exists() and not path.is_symlink():
            try:
                result = subprocess.run(['/usr/bin/du', '-sk', str(path)], text=True, capture_output=True, timeout=120)
                if result.returncode == 0:
                    size = int(result.stdout.split()[0]) * 1024
                else:
                    reason += ' 大小读取不完整。'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                reason += ' 大小暂不可用；未按 0 处理。'
        else:
            reason += ' 默认路径不存在或为链接；自定义位置未扫描。'
        command = 'git -C {} status --short\ngit -C {} worktree list'.format(shlex.quote(str(path)), shlex.quote(str(path))) if title == '任务目录' else None
        value = row(str(path), title, reason, str(path), size, command)
        value['category'] = '大项占用 · 仅查看'
        value['outcome'] = '仅统计'
        rows.append(value)
    return rows
