#!/usr/bin/env python3
"""Build/install this Mac's Zhijian app; project data and secrets stay in place."""
import argparse
import os
import pathlib
import plistlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
BUNDLE_ID = 'local.zhijian.app'


def build(destination):
    destination = pathlib.Path(destination).expanduser().resolve()
    if destination.exists():
        with open(destination/'Contents/Info.plist','rb') as f:
            existing=plistlib.load(f)
        if existing.get('CFBundleIdentifier') != BUNDLE_ID:
            raise RuntimeError('Refusing to replace an unrelated application')
    subprocess.run(['bash',str(ROOT/'build.sh')],check=True)
    with tempfile.TemporaryDirectory(prefix='zhijian-build-',dir=ROOT/'.tmp') as temp:
        temp=pathlib.Path(temp)
        app=temp/'知间.app';contents=app/'Contents';resources=contents/'Resources'
        resources.mkdir(parents=True);(contents/'MacOS').mkdir()
        shutil.copy2(ROOT/'wvbar',contents/'MacOS/Zhijian')
        iconset=temp/'AppIcon.iconset';iconset.mkdir()
        for size in [16,32,128,256,512]:
            for scale in [1,2]:
                name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
                subprocess.run(['sips','-z',str(size*scale),str(size*scale),
                                str(ROOT/'assets/zhijian-logo.png'),'--out',str(iconset/name)],
                               check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(resources/'AppIcon.icns')],check=True)
        info={
            'CFBundleIdentifier':BUNDLE_ID,'CFBundleName':'知间','CFBundleDisplayName':'知间',
            'CFBundleExecutable':'Zhijian','CFBundlePackageType':'APPL',
            'CFBundleShortVersionString':'1.0.0','CFBundleVersion':'1',
            'CFBundleIconFile':'AppIcon','CFBundleDevelopmentRegion':'zh_CN',
            'CFBundleLocalizations':['zh_CN'],'NSPrincipalClass':'NSApplication',
            'LSApplicationCategoryType':'public.app-category.social-networking',
            'LSUIElement':False,'NSHighResolutionCapable':True,
            'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
            'WVProjectRoot':str(ROOT),
            'NSHumanReadableCopyright':'知间 · 从聊天中了解彼此的关系',
        }
        with open(contents/'Info.plist','wb') as f:plistlib.dump(info,f)
        (contents/'PkgInfo').write_bytes(b'APPL????')
        subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
        subprocess.run(['codesign','--verify','--strict',str(app)],check=True)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            backup=ROOT/'.tmp/previous-zhijian.app'
            if backup.exists():shutil.rmtree(backup)
            shutil.move(str(destination),str(backup))
        shutil.copytree(app,destination)
    subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister','-f',str(destination)],check=True)
    subprocess.run(['mdimport',str(destination)],check=True)
    print('Installed:',destination)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',default='/Applications/知间.app')
    build(parser.parse_args().destination)
