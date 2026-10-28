#!/usr/bin/env python3
"""Install/update the menu app and enable guarded automatic maintenance."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

from service import activate

root = Path(__file__).resolve().parents[1]
home = Path.home()
app = home / 'Applications/资源看板.app'
staging = home / 'Library/Caches/dev-resource-guard/installation' / str(time.time_ns()) / '资源看板.app'
if app.exists():
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'io.github.ruanjianjian.mac-dev-resource-guard.widget':
        raise SystemExit('目标应用不是资源看板，保留原文件')
if staging.exists():
    raise SystemExit('上次安装副本仍在，请核对后重试：' + str(staging))
subprocess.run([sys.executable, str(root/'widget/build.py'), '--output', str(staging)], check=True)
subprocess.run([str(staging/'Contents/MacOS/ResourceDashboard'), '--self-test'], check=True)
# Only stop our menu process; detached builds and maintenance are left running.
if app.exists():
    executable = str(app/'Contents/MacOS/ResourceDashboard')
    subprocess.run(['pkill', '-TERM', '-f', '^' + __import__('re').escape(executable) + '$'])
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
subprocess.run(['open', str(app)], check=True)
print('安装完成：安全自动维护已启用，面板默认隐藏。点击菜单栏图标查看结果。')
