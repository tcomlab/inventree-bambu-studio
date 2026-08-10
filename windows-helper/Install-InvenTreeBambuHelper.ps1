param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$AllowedHosts,

    [Security.SecureString]$ApiToken,

    [switch]$SkipTokenPrompt
)

$ErrorActionPreference = 'Stop'
$helperRoot = Join-Path $env:LOCALAPPDATA 'InvenTreeBambuOpen'
$helperScript = Join-Path $helperRoot 'Open-InvenTreeBambu.ps1'
$trayScript = Join-Path $helperRoot 'InvenTreeBambuTray.ps1'
$sourceHelperScript = Join-Path $PSScriptRoot 'Open-InvenTreeBambu.ps1'
$sourceTrayScript = Join-Path $PSScriptRoot 'InvenTreeBambuTray.ps1'
$configPath = Join-Path $helperRoot 'config.json'
$tokenPath = Join-Path $helperRoot 'token.dat'
$protocolRoot = 'HKCU:\Software\Classes\inventree-bambu-open'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

foreach ($sourcePath in @($sourceHelperScript, $sourceTrayScript)) {
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Helper source script not found: $sourcePath"
    }
}

$normalizedHosts = @(
    $AllowedHosts | ForEach-Object {
        $value = $_.Trim().ToLowerInvariant()
        if ($value -match '^https?://') {
            ([Uri]$value).Host.ToLowerInvariant()
        }
        else {
            $value.Split(':')[0]
        }
    } | Sort-Object -Unique
)

if ($normalizedHosts.Count -eq 0) {
    throw 'At least one InvenTree host is required.'
}

New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null

$runningTrays = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($trayScript) }
foreach ($process in $runningTrays) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

Copy-Item -LiteralPath $sourceHelperScript -Destination $helperScript -Force
Copy-Item -LiteralPath $sourceTrayScript -Destination $trayScript -Force

@{ allowedHosts = $normalizedHosts } |
    ConvertTo-Json |
    Set-Content -LiteralPath $configPath -Encoding UTF8

if (-not $ApiToken -and -not $SkipTokenPrompt -and -not (Test-Path -LiteralPath $tokenPath)) {
    $ApiToken = Read-Host 'Enter an InvenTree API token for saving 3MF attachments' -AsSecureString
}

if ($ApiToken -and $ApiToken.Length -gt 0) {
    $ApiToken |
        ConvertFrom-SecureString |
        Set-Content -LiteralPath $tokenPath -Encoding ASCII
}

New-Item -Path $protocolRoot -Force | Out-Null
Set-Item -Path $protocolRoot -Value 'URL:InvenTree Bambu Studio Protocol'
New-ItemProperty -Path $protocolRoot -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null

$iconKey = New-Item -Path "$protocolRoot\DefaultIcon" -Force
Set-Item -Path $iconKey.PSPath -Value 'C:\Program Files\Bambu Studio\bambu-studio.exe,0'

$commandKey = New-Item -Path "$protocolRoot\shell\open\command" -Force
$protocolCommand = '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $helperScript + '" "%1"'
Set-Item -Path $commandKey.PSPath -Value $protocolCommand

$trayCommand = '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $trayScript + '"'
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name 'InvenTreeBambuTray' -Value $trayCommand -PropertyType String -Force | Out-Null

Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', ('"{0}"' -f $trayScript)
)

Write-Host 'InvenTree Bambu helper installed.' -ForegroundColor Green
Write-Host "Allowed InvenTree hosts: $($normalizedHosts -join ', ')"
Write-Host "Helper directory: $helperRoot"
if (Test-Path -LiteralPath $tokenPath) {
    Write-Host 'API token: configured with Windows DPAPI' -ForegroundColor Green
}
else {
    Write-Warning 'API token is not configured. Opening models works, but saving to InvenTree is disabled.'
}
