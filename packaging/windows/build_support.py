"""Build ABI checks, including x64 Python running on Windows 11 ARM64."""
from pathlib import Path
import os
import runpy
import struct
import sys
import sysconfig


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


def probe_python():
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise ValueError('Use Windows Python 3.12 x64, not ARM64 or 32-bit Python.')
    require_x64(sys.executable)
    if sysconfig.get_platform() != 'win-amd64':
        raise ValueError('Python package ABI must be win-amd64.')
    import tkinter  # noqa: F401
    print(sys.executable)


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
        raise ValueError('Expected --probe, --verify-bundle DIR or --pyinstaller ARGS.')


if __name__ == '__main__':
    main(sys.argv[1:])
