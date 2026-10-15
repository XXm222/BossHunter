import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
node = Path(os.environ['BOSSHUNTER_BUILD_NODE'])
datas = [
    (str(root / 'src/bosshunter/web/frontend/dist'), 'bosshunter/web/frontend/dist'),
    (str(root / 'src/bosshunter/web/config_schema.json'), 'bosshunter/web'),
    (str(root / 'src/bosshunter/data'), 'bosshunter/data'),
    (str(root / 'LICENSE'), 'licenses'),
    (str(node / 'node.exe'), 'vendor/node'),
    (str(node / 'LICENSE'), 'licenses/node'),
]
runtime = root / 'src/bosshunter/browser/runtime'
for pattern in ('*.mjs', 'site-patterns/*.md'):
    for source in runtime.glob(pattern):
        datas.append((str(source), str(Path('bosshunter/browser/runtime') / source.relative_to(runtime).parent)))
binaries = []
hiddenimports = collect_submodules('bosshunter') + ['browser_cookie3', 'tzdata']
for package in ('patchright', 'rapidocr_onnxruntime', 'onnxruntime', 'tzdata'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
datas += copy_metadata('bosshunter', recursive=True)

a = Analysis(
    [str(root / 'packaging/windows/launcher.py')],
    pathex=[str(root / 'src')], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BossHunter',
          debug=False, bootloader_ignore_signals=False, strip=False,
          upx=False, console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='BossHunter')
