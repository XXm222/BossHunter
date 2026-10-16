"""Build ABI checks, including x64 Python running on Windows 11 ARM64."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import os
import runpy
import struct
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
import zipfile


VC_REDIST_URL = 'https://aka.ms/vc14/vc_redist.x64.exe'
NATIVE_CHECKS = (
    ('PyMuPDF PDF rendering', '''
import pymupdf
with pymupdf.open() as document:
    page = document.new_page()
    page.insert_text((72, 72), 'BossHunter native dependency check')
    content = document.tobytes()
with pymupdf.open(stream=content, filetype='pdf') as document:
    if 'BossHunter' not in document[0].get_text():
        raise RuntimeError('PDF text round-trip failed.')
    if not document[0].get_pixmap().samples:
        raise RuntimeError('PDF rendering failed.')
print('PyMuPDF', pymupdf.VersionBind, 'OK')
'''),
    ('RapidOCR model loading and inference', '''
import numpy as np
from rapidocr_onnxruntime import RapidOCR
engine = RapidOCR()
engine(np.full((64, 128, 3), 255, dtype=np.uint8))
print('RapidOCR model loading and inference OK')
'''),
)


def check_native_dependencies():
    """Detect broken native wheels before spending time freezing the app.

    A separate process also turns native crashes into a readable build error.
    These checks use only local libraries/models and make no platform requests.
    """
    for name, script in NATIVE_CHECKS:
        print(f'Checking {name} in the build environment...', flush=True)
        try:
            result = subprocess.run(
                [sys.executable, '-I', '-X', 'utf8', '-c', script],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'{name} check timed out after 120 seconds; PyInstaller was not started.') from error
        output = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if output:
            print(output, flush=True)
        if result.returncode:
            message = (f'{name} failed in the UNFROZEN build environment '
                       f'(exit 0x{result.returncode & 0xffffffff:08x}); PyInstaller was not started.')
            if sys.platform == 'win32' and 'dll load failed' in output.lower():
                message += (f' A missing/outdated Visual C++ runtime is one possible cause. '
                            f'Install or repair the official x64 runtime: {VC_REDIST_URL} '
                            '(also for x64 Python on Windows 11 ARM64), then rerun this check. '
                            'If it still fails, preserve the error above for diagnosis.')
            raise RuntimeError(message)


def pe_machine(path):
    with Path(path).open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError(f'Not a Windows executable: {path}')
        stream.seek(60)
        offset = struct.unpack('<I', stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b'PE\0\0':
            raise ValueError(f'Invalid PE header: {path}')
        return struct.unpack('<H', stream.read(2))[0]


def require_x64(path):
    machine = pe_machine(path)
    if machine != 0x8664:
        raise ValueError(f'x64 required, PE machine is 0x{machine:04x}: {path}')


def vc_runtime_manifest():
    return json.loads(Path(__file__).with_name('vc-runtime.json').read_text(encoding='utf-8'))


def stage_vc_runtime(archive, folder):
    """Extract only the release x64 CRT from a hash-pinned Microsoft VSIX.

    ARM64 Windows can expose ARM64 CRT DLLs to an emulated x64 build process.
    Never use those host files as the runtime for an Intel/AMD distribution.
    """
    manifest = vc_runtime_manifest()
    archive, folder = Path(archive), Path(folder)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != manifest['sha256']:
        raise ValueError('Microsoft x64 VC runtime archive SHA-256 mismatch.')
    folder.parent.mkdir(parents=True, exist_ok=True)
    # Validate every DLL before changing a previously staged runtime.
    with TemporaryDirectory(prefix='vc-runtime-', dir=folder.parent) as temporary:
        temporary = Path(temporary)
        with zipfile.ZipFile(archive) as package:
            for name in manifest['dll_names']:
                path = temporary / name
                path.write_bytes(package.read(manifest['member_prefix'] + name))
                require_x64(path)
        folder.mkdir(parents=True, exist_ok=True)
        for name in manifest['dll_names']:
            (temporary / name).replace(folder / name)
    print(f"Staged Microsoft VC runtime {manifest['version']}: {folder}")


def vc_runtime_files(folder):
    files = [Path(folder) / name for name in vc_runtime_manifest()['dll_names']]
    for path in files:
        require_x64(path)
    return files


def pin_vc_runtime(binaries, datas, runtime_files):
    """Replace CRT entries selected by hooks/dependency scans before COLLECT.

    Keep original destinations (including package-local copies) so an ARM64
    DLL cannot overwrite our x64 runtime even if collected as a data file.
    """
    runtime = {path.name.lower(): str(path) for path in runtime_files}
    pinned, remaining_data = [], []
    for entries, target in ((binaries, pinned), (datas, remaining_data)):
        for destination, source, kind in entries:
            name = PureWindowsPath(destination).name.lower()
            if name in runtime:
                pinned.append((destination, runtime[name], 'BINARY'))
            else:
                target.append((destination, source, kind))
    root_destinations = {destination.lower() for destination, _, _ in pinned}
    for name, source in runtime.items():
        if name not in root_destinations:
            pinned.append((name, source, 'BINARY'))
    return pinned, remaining_data


def probe_python():
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise ValueError('Use Windows Python 3.12 x64, not ARM64 or 32-bit Python.')
    require_x64(sys.executable)
    if sysconfig.get_platform() != 'win-amd64':
        raise ValueError('Python package ABI must be win-amd64.')
    import tkinter  # noqa: F401
    print(sys.executable)


def check_node_archive(archive, destination):
    """Check actual ZIP members against PowerShell 5.1's legacy path limits.

    Check every member, including deeply nested npm files, instead of relying
    on the one filename reported by an extraction error. Inspect before
    extraction so this check cannot leave a half-unpacked cache.
    """
    destination = PureWindowsPath(str(destination))
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
    if not members:
        raise ValueError('Node archive is empty.')
    longest = 0
    exceeds_limit = False
    for member in members:
        path = destination.joinpath(*PurePosixPath(member.filename).parts)
        units = len(str(path).encode('utf-16-le')) // 2
        parent_units = len(str(path.parent).encode('utf-16-le')) // 2
        longest = max(longest, units)
        if units >= (248 if member.is_dir() else 260) or parent_units >= 248:
            exceeds_limit = True
    if exceeds_limit:
        raise ValueError(
            f'Node ZIP paths are too long for Windows PowerShell 5.1 '
            f'(longest: {longest} UTF-16 characters). '
            r'Re-extract the complete project to C:\BossHunter without nested '
            r'archive-name folders, then run packaging\windows\build.cmd. '
            r'Do not move or reuse the old .build\windows\venv; rebuild it at the new location.'
        )
    print(f'Node ZIP path check passed (longest: {longest} UTF-16 characters).')


def verify_bundle(folder):
    files = [path for path in Path(folder).rglob('*')
             if path.is_file() and path.suffix.lower() in {'.exe', '.dll', '.pyd'}]
    if not files or not (Path(folder) / 'BossHunter.exe').is_file():
        raise ValueError('Frozen application was not generated.')
    for path in files:
        require_x64(path)
    print(f'Verified {len(files)} executable/native files: all x64.')


def main(arguments):
    if arguments == ['--probe']:
        probe_python()
    elif arguments == ['--check-native']:
        check_native_dependencies()
    elif len(arguments) == 3 and arguments[0] == '--stage-vc-runtime':
        stage_vc_runtime(arguments[1], arguments[2])
    elif len(arguments) == 3 and arguments[0] == '--check-node-archive':
        check_node_archive(arguments[1], arguments[2])
    elif len(arguments) == 2 and arguments[0] == '--verify-bundle':
        verify_bundle(arguments[1])
    elif arguments and arguments[0] == '--pyinstaller':
        probe_python()
        # PyInstaller chooses its Windows bootloader using this environment
        # value. An ARM64 PowerShell parent can pass ARM64 to x64 Python.
        # Set it ONLY inside this verified x64 build process, not system-wide.
        os.environ['PROCESSOR_ARCHITECTURE'] = 'AMD64'
        sys.argv = ['pyinstaller'] + arguments[1:]
        runpy.run_module('PyInstaller', run_name='__main__')
    else:
        raise ValueError('Expected --probe, --check-native, --stage-vc-runtime ARCHIVE DIR, --check-node-archive ARCHIVE DIR, --verify-bundle DIR or --pyinstaller ARGS.')


if __name__ == '__main__':
    main(sys.argv[1:])
