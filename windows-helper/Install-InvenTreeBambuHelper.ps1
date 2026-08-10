param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$AllowedHosts
)

$ErrorActionPreference = 'Stop'
$helperRoot = Join-Path $env:LOCALAPPDATA 'InvenTreeBambuOpen'
$helperScript = Join-Path $helperRoot 'Open-InvenTreeBambu.ps1'
$sourceScript = Join-Path $PSScriptRoot 'Open-InvenTreeBambu.ps1'
$protocolRoot = 'HKCU:\Software\Classes\inventree-bambu-open'

if (-not (Test-Path -LiteralPath $sourceScript)) {
    throw "Helper source script not found: $sourceScript"
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
Copy-Item -LiteralPath $sourceScript -Destination $helperScript -Force

@{ allowedHosts = $normalizedHosts } |
    ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $helperRoot 'config.json') -Encoding UTF8

New-Item -Path $protocolRoot -Force | Out-Null
Set-Item -Path $protocolRoot -Value 'URL:InvenTree Bambu Studio Protocol'
New-ItemProperty -Path $protocolRoot -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null

$iconKey = New-Item -Path "$protocolRoot\DefaultIcon" -Force
Set-Item -Path $iconKey.PSPath -Value 'C:\Program Files\Bambu Studio\bambu-studio.exe,0'

$commandKey = New-Item -Path "$protocolRoot\shell\open\command" -Force
$command = '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $helperScript + '" "%1"'
Set-Item -Path $commandKey.PSPath -Value $command

Write-Host 'InvenTree Bambu helper installed.' -ForegroundColor Green
Write-Host "Allowed InvenTree hosts: $($normalizedHosts -join ', ')"
Write-Host "Helper directory: $helperRoot"
