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
(app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'io.github.ruanjianjian.mac-dev-resource-guard.widget','CFBundleName':'资源看板','CFBundleDisplayName':'资源看板','CFBundleExecutable':'ResourceDashboard','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.1.0','CFBundleVersion':'2','LSMinimumSystemVersion':'13.0','LSUIElement':True,'NSHighResolutionCapable':True}))
subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
print(app)
