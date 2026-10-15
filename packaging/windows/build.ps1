# Windows PowerShell 5.1 compatible. Run through build.cmd.
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Work = Join-Path $Root '.build\windows'
$Output = Join-Path $Root 'dist\windows'
New-Item -ItemType Directory -Force $Work | Out-Null
Start-Transcript -Path (Join-Path $Work 'build.log') -Force | Out-Null
try {
    # Invalidate the installer input until this build passes its smoke test.
    $VersionInclude = Join-Path $Output 'version.iss'
    if (Test-Path $VersionInclude) { Remove-Item -LiteralPath $VersionInclude }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'A 64-bit Windows build host is required.'
    }
    $HostArchitecture = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').PROCESSOR_ARCHITECTURE
    $WindowsBuild = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
    if ($HostArchitecture -eq 'ARM64' -and $WindowsBuild -lt 22000) {
        throw 'Windows 10 ARM64 cannot run the required x64 build tools. Use Windows 11 ARM64 or an Intel/AMD Windows PC.'
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    function Run([string]$Program, [string[]]$Arguments) {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
    }
    # Builder prerequisites only; recipients need neither Python nor Node.js.
    $Python = $null
    $Support = Join-Path $PSScriptRoot 'build_support.py'
    foreach ($Candidate in @('py', 'python')) {
        if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
            $ProbeArguments = @($Support, '--probe')
            if ($Candidate -eq 'py') { $ProbeArguments = @('-3.12') + $ProbeArguments }
            try {
                $Found = & $Candidate @ProbeArguments 2>$null
                if ($LASTEXITCODE -eq 0) { $Python = [string]($Found | Select-Object -Last 1); break }
            } catch { continue }
        }
    }
    if (-not $Python) {
        throw 'Install Python 3.12 x64 from python.org with pip and Tcl/Tk, then double-click build.cmd again. https://www.python.org/downloads/windows/'
    }
    Write-Host "Build Python: $Python"
    $Venv = Join-Path $Work 'venv'
    if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
        Run $Python @('-m', 'venv', $Venv)
    }
    $BuildPython = Join-Path $Venv 'Scripts\python.exe'
    # Reject an old ARM64/32-bit venv rather than silently shipping its ABI.
    Run $BuildPython @($Support, '--probe')
    Run $BuildPython @('-m', 'pip', 'install', '--upgrade', 'pip')
    Run $BuildPython @('-m', 'pip', 'install', 'pyinstaller>=6.16,<7', 'tzdata')

    # Use the official Node 22 x64 distribution and verify its SHA-256.
    $Sums = Join-Path $Work 'SHASUMS256.txt'
    Invoke-WebRequest -UseBasicParsing 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt' -OutFile $Sums
    $Match = [regex]::Match((Get-Content $Sums -Raw), '(?m)^([a-fA-F0-9]{64})\s+(node-v22\.[0-9]+\.[0-9]+-win-x64\.zip)\s*$')
    if (-not $Match.Success) { throw 'Cannot identify the official Node 22 Windows x64 archive.' }
    $Archive = Join-Path $Work $Match.Groups[2].Value
    if (-not (Test-Path $Archive) -or (Get-FileHash $Archive -Algorithm SHA256).Hash -ne $Match.Groups[1].Value) {
        Invoke-WebRequest -UseBasicParsing ("https://nodejs.org/dist/latest-v22.x/" + $Match.Groups[2].Value) -OutFile $Archive
    }
    if ((Get-FileHash $Archive -Algorithm SHA256).Hash -ne $Match.Groups[1].Value) { throw 'Node archive SHA-256 mismatch.' }
    $NodeFolder = Join-Path $Work ([IO.Path]::GetFileNameWithoutExtension($Archive))
    Expand-Archive -LiteralPath $Archive -DestinationPath $Work -Force
    $env:PATH = "$NodeFolder;$env:PATH"
    $env:BOSSHUNTER_BUILD_NODE = $NodeFolder
    Run (Join-Path $NodeFolder 'node.exe') @('--version')
    $Frontend = Join-Path $Root 'src\bosshunter\web\frontend'
    Push-Location $Frontend
    try {
        Run (Join-Path $NodeFolder 'npm.cmd') @('ci')
        Run (Join-Path $NodeFolder 'npm.cmd') @('run', 'build')
    } finally { Pop-Location }

    # Hatchling force-includes frontend/dist even during metadata preparation.
    # A fresh source ZIP has no dist, so install the project only AFTER npm build.
    if (-not (Test-Path (Join-Path $Frontend 'dist\index.html'))) {
        throw 'Frontend build did not produce dist\index.html; cannot install the Python project.'
    }
    Run $BuildPython @('-m', 'pip', 'install', "${Root}[recruiting,ocr,pdf]")

    Push-Location $Root
    try {
        Run $BuildPython @($Support, '--pyinstaller', '--noconfirm', '--clean', '--distpath', $Output, '--workpath', (Join-Path $Work 'pyinstaller'), (Join-Path $PSScriptRoot 'BossHunter.spec'))
    } finally { Pop-Location }
    $Exe = Join-Path $Output 'BossHunter\BossHunter.exe'
    Run $BuildPython @($Support, '--verify-bundle', (Join-Path $Output 'BossHunter'))
    $SmokeResult = Join-Path $Work 'smoke-result.json'
    if (Test-Path $SmokeResult) { Remove-Item -LiteralPath $SmokeResult }
    $Process = Start-Process -FilePath $Exe -ArgumentList @('--smoke-test', ('"' + $SmokeResult + '"')) -PassThru
    $null = $Process.Handle  # Retain the handle for ExitCode on Windows PowerShell 5.1.
    if (-not $Process.WaitForExit(300000)) {
        # Stop only this build's self-test and its driver children.
        & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Host
        throw 'Frozen application smoke test timed out after 5 minutes. Check .build\windows\smoke-result.log.'
    }
    if (Test-Path $SmokeResult) {
        $Smoke = Get-Content $SmokeResult -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $Smoke.ok) {
            Write-Host $Smoke.error -ForegroundColor Red
            throw 'Frozen application resources/dependencies are incomplete. See the error above.'
        }
    }
    if ($Process.ExitCode -ne 0 -or -not (Test-Path $SmokeResult)) { throw 'Frozen application smoke test failed. Check .build\windows\smoke-result.log and smoke-result.json.' }
    $Version = & $BuildPython -c 'import bosshunter; print(bosshunter.__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read application version.' }
    Run $BuildPython @('-m', 'pip', 'freeze', '--all')
    [IO.File]::WriteAllText($VersionInclude, ('#define MyAppVersion "' + $Version.Trim() + '"'), [Text.Encoding]::ASCII)
    Write-Host "SUCCESS: $Exe"
    Write-Host 'Next: double-click packaging\windows\installer.cmd (Inno Setup 6 required).'
} catch {
    Write-Host ("BUILD FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally { Stop-Transcript | Out-Null }
