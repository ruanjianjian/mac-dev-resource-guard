#!/usr/bin/env python3
"""Install/update the menu app and enable guarded automatic maintenance."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

from service import activate, ask_consent, current_target, enable_recommended, install_skill, set_target, stop_menu_app, tiers_config, waiting_lock
import discover
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'widget'))
import local_signing

root = Path(__file__).resolve().parents[1]
home = Path.home()
app = home / 'Applications/资源看板.app'
# Validate before replacing a working app; invalid settings remain untouched.
tiers_config.load(home)
staging = home / 'Library/Caches/dev-resource-guard/installation' / str(time.time_ns()) / '资源看板.app'
# macOS keys permissions on the signature's requirement; compare it to know whether it will ask again.
before = local_signing.requirement(app) if app.exists() else ''
if app.exists():
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'io.github.ruanjianjian.mac-dev-resource-guard.widget':
        raise SystemExit('目标应用不是资源看板，保留原文件')
if staging.exists():
    raise SystemExit('上次安装副本仍在，请核对后重试：' + str(staging))
subprocess.run([sys.executable, str(root/'widget/build.py'), '--output', str(staging)], check=True)
subprocess.run([str(staging/'Contents/MacOS/ResourceDashboard'), '--self-test'], check=True)
# Never replace the app or activate a new engine over an in-flight maintenance round.
# A busy lock returns an error for retry; it does not stop the old worker.
with waiting_lock(home, 'periodic-storage'):
    # Only stop our menu process; detached builds and maintenance are left running.
    if app.exists():
        executable = str(app/'Contents/MacOS/ResourceDashboard')
        left = stop_menu_app(executable)
        if left:
            raise SystemExit('旧的资源看板（PID {}）没有退出，已保留原 App；请退出它后重试'.format(left))
        backup = home / '.local/state/dev-resource-guard/app-backups' / str(time.time_ns())
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(app, backup)
    app.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(staging), str(app))
    except BaseException:
        if 'backup' in locals(): os.rename(backup, app)
        raise
    # The storage job runs through the installed app, so activate only once it is in place.
    result = activate(home)
# The companion skill travels with the app so the user's agent always matches the installed version.
for note in install_skill(home, root):
    print(note)
# One question at the first install for what needs consent: shared caches and the drive (RAW-36).
# Safe maintenance (expired Xcode intermediates only) is on by default and kept as chosen on upgrades.
drive = discover.recommended_drive(discover.external_drives(look_inside=False))
answer = ask_consent(home, drive['name'] if drive else None)
# The agent passes the target the user agreed to in the same question (RESOURCE_GUARD_TARGET=90).
if os.environ.get('RESOURCE_GUARD_TARGET', '').strip():
    set_target(home, os.environ['RESOURCE_GUARD_TARGET'].strip())
if answer == 'yes':
    print('正在体检并打开，任务多时要一两分钟…')
    for note in enable_recommended(home):
        print(note)
subprocess.run(['open', '-g', '-j', str(app)], check=True)
enabled = tiers_config.load(home)['auto_enabled'] and not (home/'.local/state/dev-resource-guard/storage-paused').exists()
safe = '安全自动维护' + ('已启用' if enabled else '保持关闭')
print({
    'yes': '安装完成：都已开好，{}，面板不会自己弹出。点菜单栏的资源看板图标随时查看。'.format(safe),
    'no': '安装完成：{}；共享构建缓存和外接盘没有打开，以后可以在「帮你管着」里打开。'.format(safe),
    'decided': '安装完成：{}，面板不会自己弹出。共享构建缓存和外接盘保持原有选择。'.format(safe),
    'unasked': '安装完成：{}。还没问你要不要打开共享构建缓存和外接盘；同意的话运行：~/.local/bin/resource-guard-service enable'.format(safe),
}[answer])
if answer != 'decided':
    print('空间目标：内置盘至少留 {:.0f} GB（约磁盘的 20%），想改就在菜单栏「资源看板」→「帮你管着」里调，或告诉你的 agent。'.format(current_target(home)['target_gib']))
if tiers_config.load(home)['external_root']:
    # Without "Allow" moves to the drive pause. Same requirement as before: macOS keeps the earlier answer.
    after = local_signing.requirement(app)
    if 'cdhash' in after:
        print('提示：macOS 可能会再问一次「资源看板」能否访问外接盘（这次用的是临时签名），请点「允许」，否则往外接盘挪东西会暂停。')
    elif not before:
        print('提示：macOS 会问一次「资源看板」能否访问外接盘，请点「允许」；点过以后升级不会再问。')
    elif after != before:
        print('提示：macOS 会再问一次「资源看板」能否访问外接盘，请点「允许」；资源看板已换成这台 Mac 的固定签名，以后升级不会再问。')
