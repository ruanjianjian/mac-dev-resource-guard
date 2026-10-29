#!/usr/bin/env python3
"""Build a local unsigned macOS app with a fixed Swift module cache."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,default=Path.home()/'Applications/资源看板.app')
a=p.parse_args()
app=a.output.expanduser().resolve()
if app.exists(): raise SystemExit('Output exists; retain the installed app and choose a new --output for review.')
source=Path(__file__).resolve().parent
cache=Path.home()/'Library/Caches/dev-resource-guard/widget-swift-modules'
cache.mkdir(parents=True,exist_ok=True)
(app/'Contents/MacOS').mkdir(parents=True)
subprocess.run(['xcrun','swiftc','-O','-module-cache-path',str(cache),'-target',subprocess.check_output(['uname','-m'],text=True).strip()+'-apple-macosx13.0',*[str(p) for p in sorted(source.glob('*.swift'))],'-o',str(app/'Contents/MacOS/ResourceDashboard')],check=True)
(app/'Contents/Resources').mkdir(parents=True)
shutil.copy2(source/'backend.py',app/'Contents/Resources/backend.py')
shutil.copy2(source/'setup_status.py',app/'Contents/Resources/setup_status.py')
engine=app/'Contents/Resources/engine'
for folder in ('runtime', 'scripts', 'references', 'widget', 'docs', 'tests'):
    shutil.copytree(source.parent/folder, engine/folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
shutil.copy2(source.parent/'SKILL.md', engine/'SKILL.md')
shutil.copy2(source.parent/'README.md', engine/'README.md')
(app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'io.github.ruanjianjian.mac-dev-resource-guard.widget','CFBundleName':'资源看板','CFBundleDisplayName':'资源看板','CFBundleExecutable':'ResourceDashboard','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.7.2','CFBundleVersion':'13','NSDownloadsFolderUsageDescription':'查找下载文件夹里久未用的大安装包；删除前会先问你。','NSRemovableVolumesUsageDescription':'把开发缓存和闲置任务目录放到你选的外接盘上。','NSAppleEventsUsageDescription':'你确认清空废纸篓时，请访达来清空。','LSMinimumSystemVersion':'13.0','LSUIElement':True,'NSHighResolutionCapable':True}))
subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
print(app)
