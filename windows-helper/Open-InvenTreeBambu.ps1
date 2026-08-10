param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ProtocolUri
)

$ErrorActionPreference = 'Stop'
$helperRoot = Join-Path $env:LOCALAPPDATA 'InvenTreeBambuOpen'
$logPath = Join-Path $helperRoot 'helper.log'
$configPath = Join-Path $helperRoot 'config.json'

function Write-HelperLog {
    param([string]$Message)
    $line = '{0:u} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null
    Add-Type -AssemblyName System.Web

    $parsedProtocol = [Uri]$ProtocolUri
    if ($parsedProtocol.Scheme -ne 'inventree-bambu-open') {
        throw 'Invalid helper protocol.'
    }

    $query = [System.Web.HttpUtility]::ParseQueryString($parsedProtocol.Query)
    $attachmentUrl = [Uri]$query['url']
    $filename = [IO.Path]::GetFileName($query['filename'])

    if (-not (Test-Path -LiteralPath $configPath)) {
        throw 'Helper configuration is missing. Run Install-InvenTreeBambuHelper.ps1 again.'
    }

    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $allowedHosts = @($config.allowedHosts) | ForEach-Object { $_.ToString().ToLowerInvariant() }

    if ($attachmentUrl.Scheme -ne 'https') {
        throw 'Only HTTPS attachments are allowed.'
    }

    if ($allowedHosts -notcontains $attachmentUrl.Host.ToLowerInvariant()) {
        throw "The InvenTree host '$($attachmentUrl.Host)' is not allowed by the helper configuration."
    }

    $extension = [IO.Path]::GetExtension($filename).ToLowerInvariant()
    if ($extension -notin @('.step', '.stp')) {
        throw 'Only STEP and STP files are allowed.'
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($attachmentUrl.AbsoluteUri))
    }
    finally {
        $sha.Dispose()
    }

    $hash = -join ($hashBytes | ForEach-Object { $_.ToString('x2') })
    $partFolder = Join-Path (Join-Path $helperRoot 'Models') $hash.Substring(0, 16)
    New-Item -ItemType Directory -Path $partFolder -Force | Out-Null

    $targetPath = Join-Path $partFolder $filename
    $downloadPath = Join-Path $partFolder ($filename + '.download')

    Invoke-WebRequest -Uri $attachmentUrl.AbsoluteUri -OutFile $downloadPath -UseBasicParsing -MaximumRedirection 0
    Move-Item -LiteralPath $downloadPath -Destination $targetPath -Force
    Write-HelperLog "Opening $targetPath"

    Start-Process -FilePath $targetPath
}
catch {
    try {
        New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null
        Write-HelperLog ("ERROR: " + $_.Exception.Message)
    }
    catch {}

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Could not open the STEP file in Bambu Studio.`n`n$($_.Exception.Message)`n`nLog: $logPath",
        'InvenTree Bambu Helper',
        'OK',
        'Error'
    ) | Out-Null
    exit 1
}
