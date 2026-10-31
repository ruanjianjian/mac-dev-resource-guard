#!/usr/bin/env python3
"""Build a local unsigned macOS app with a fixed Swift module cache."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,default=Path.home()/'Applications/资源看板.app')
a=p.parse_args()
app=a.output.expanduser().resolve()
if app.exists(): raise SystemExit('Output exists; retain the installed app and choose a new --output for review.')
source=Path(__file__).resolve().parent
cache=Path.home()/'Library/Caches/dev-resource-guard/widget-swift-modules'
cache.mkdir(parents=True,exist_ok=True)
(app/'Contents/MacOS').mkdir(parents=True)
# The SDK must come from the same developer folder as swiftc. /usr/bin/python3 (how the installer runs) hands its
# children SDKROOT=the Command Line Tools SDK; with Xcode's swiftc that SDK's SwiftUI macros have no plugin and the
# build fails (10-31, Xcode 27.2 beside newer Command Line Tools). `--sdk macosx` picks the matching one.
env={k:v for k,v in os.environ.items() if k not in ('SDKROOT','CPATH','LIBRARY_PATH')}
subprocess.run(['xcrun','--sdk','macosx','swiftc','-O','-module-cache-path',str(cache),'-target',subprocess.check_output(['uname','-m'],text=True).strip()+'-apple-macosx13.0',*[str(p) for p in sorted(source.glob('*.swift'))],'-o',str(app/'Contents/MacOS/ResourceDashboard')],check=True,env=env)
(app/'Contents/Resources').mkdir(parents=True)
# 小留, drawn by the app's own code (widget/Logo.swift); a missing icon never blocks the install.
with tempfile.TemporaryDirectory() as scratch:
    iconset=Path(scratch)/'AppIcon.iconset'
    try:
        subprocess.run([str(app/'Contents/MacOS/ResourceDashboard'),'--render-icon',str(iconset)],check=True,timeout=60)
        subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(app/'Contents/Resources/AppIcon.icns')],check=True,timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        print('App 图标没有生成，先用系统默认图标：{}'.format(error))
shutil.copy2(source/'backend.py',app/'Contents/Resources/backend.py')
shutil.copy2(source/'setup_status.py',app/'Contents/Resources/setup_status.py')
engine=app/'Contents/Resources/engine'
for folder in ('runtime', 'scripts', 'references', 'widget', 'docs', 'tests'):
    shutil.copytree(source.parent/folder, engine/folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
shutil.copy2(source.parent/'SKILL.md', engine/'SKILL.md')
shutil.copy2(source.parent/'README.md', engine/'README.md')
(app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'io.github.ruanjianjian.mac-dev-resource-guard.widget','CFBundleName':'资源看板','CFBundleDisplayName':'资源看板','CFBundleExecutable':'ResourceDashboard','CFBundlePackageType':'APPL','CFBundleIconFile':'AppIcon','CFBundleShortVersionString':'1.8.1','CFBundleVersion':'20','NSDownloadsFolderUsageDescription':'查找下载文件夹里久未用的大安装包；删除前会先问你。','NSRemovableVolumesUsageDescription':'把开发缓存和闲置任务目录放到你选的外接盘上。','NSAppleEventsUsageDescription':'你确认清空废纸篓时，请访达来清空。','LSMinimumSystemVersion':'13.0','LSUIElement':True,'NSHighResolutionCapable':True}))
# The same identity on every build, so macOS keeps the user's permissions across upgrades.
import local_signing
local_signing.sign(app,Path.home())
print(app)
