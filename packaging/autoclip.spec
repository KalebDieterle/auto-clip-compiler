# PyInstaller spec for the Auto Clip Compiler desktop app (onedir, windowed). Built by packaging\build.ps1.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

root = Path(SPECPATH).parent
version = re.search(r'__version__ = "([^"]+)"', (root/'clipcompiler'/'__init__.py').read_text()).group(1)
numbers = tuple(int(n) for n in re.findall(r'\d+', version)[:3]) + (0,)
info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('ProductName', 'Auto Clip Compiler'), StringStruct('FileDescription', 'Auto Clip Compiler'),
        StringStruct('FileVersion', version), StringStruct('ProductVersion', version),
        StringStruct('OriginalFilename', 'AutoClipCompiler.exe'), StringStruct('LegalCopyright', 'Kaleb Dieterle')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])

a = Analysis(
    [str(root/'autoclip.py')],
    pathex=[str(root)],
    datas=[(str(root/'clipcompiler'/'ui'/'web'), 'clipcompiler/ui/web'),
           (str(root/'packaging'/'ffmpeg'), 'ffmpeg')] + copy_metadata('keyring'),
    # keyring finds its backends through entry points; pywebview picks its platform at runtime.
    hiddenimports=collect_submodules('keyring.backends') + ['webview.platforms.edgechromium', 'webview.platforms.winforms'],
    excludes=['tkinter', 'numpy', 'matplotlib', 'PIL', 'pytest', 'IPython'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AutoClipCompiler', icon=str(root/'packaging'/'app.ico'),
          version=info, console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='AutoClipCompiler', upx=False)
