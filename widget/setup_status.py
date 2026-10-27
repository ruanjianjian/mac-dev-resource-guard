"""Desktop capability checks and explicit setup actions; no automatic setup on launch."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


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


def status(home):
    rows = [row('common', '已有 Xcode 缓存', '已内置：扫描默认 DerivedData 中四类中间缓存，无需改构建脚本；手动确认清理。')]
    rows[0]['outcome'] = '已内置'
    for name in ('dev-storage', 'dev-memory'):
        tool = home / '.local/bin' / name
        exists = tool.exists() or tool.is_symlink()
        rows.append(row(name, name, '复用现有命令，不覆盖。' if exists else '使用 App 内置实现；安装命令工具后可配置周期维护。', str(tool)))
        rows[-1]['outcome'] = '复用' if exists else '使用内置'
    paused = (home / '.local/state/dev-memory/paused').exists()
    for kind, title in (('memory', '自动内存回收'), ('storage', '每 12 小时磁盘维护')):
        enabled = loaded(home, kind)
        reason = '调度已加载' if enabled else '未启用：尚未加载定时任务'
        if kind == 'memory' and paused:
            reason += '；自动回收暂停，支持单次回收的后端仍可手动操作'
        if kind == 'storage':
            reason += '；仅托管槽，不包含已有 Xcode 缓存、任务目录和设备'
        rows.append(row(kind, title, reason))
        rows[-1]['outcome'] = '暂停' if enabled and kind == 'memory' and paused else ('已加载' if enabled else '未启用')
    rows.append(row('devices', '模拟器自动收尾',
                    '已检测到 devlease；实际可用性与占用须在诊断时复核。' if (home / '.local/bin/devlease').is_file()
                    else '未接入设备占用协议：不会关闭或删除模拟器。磁盘页仍可查看其占用。'))
    rows.append(row('broker', 'MCP 自动收尾', '需要兼容 broker；未接入的实例保留，不强杀。'))
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
                result = subprocess.run(['/usr/bin/du', '-sk', str(path)], text=True, capture_output=True, timeout=8)
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
