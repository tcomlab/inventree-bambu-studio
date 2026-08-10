param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ProtocolUri
)

$ErrorActionPreference = 'Stop'
$helperRoot = Join-Path $env:LOCALAPPDATA 'InvenTreeBambuOpen'
$logPath = Join-Path $helperRoot 'helper.log'
$configPath = Join-Path $helperRoot 'config.json'
$sessionPath = Join-Path $helperRoot 'current-session.json'
$trayScript = Join-Path $helperRoot 'InvenTreeBambuTray.ps1'

function Write-HelperLog {
    param([string]$Message)
    $line = '{0:u} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Start-TrayIfNeeded {
    if (-not (Test-Path -LiteralPath $trayScript)) {
        return
    }

    $running = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($trayScript) }

    if (-not $running) {
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-WindowStyle', 'Hidden',
            '-File', ('"{0}"' -f $trayScript)
        )
    }
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
    $instanceUrl = [Uri]$query['instanceUrl']
    $filename = [IO.Path]::GetFileName($query['filename'])
    $partId = [int]$query['partId']
    $attachmentId = [int]$query['attachmentId']

    if (-not (Test-Path -LiteralPath $configPath)) {
        throw 'Helper configuration is missing. Run Install-InvenTreeBambuHelper.ps1 again.'
    }

    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $allowedHosts = @($config.allowedHosts) | ForEach-Object { $_.ToString().ToLowerInvariant() }

    if ($attachmentUrl.Scheme -ne 'https' -or $instanceUrl.Scheme -ne 'https') {
        throw 'Only HTTPS InvenTree instances and attachments are allowed.'
    }

    if ($attachmentUrl.Host -ne $instanceUrl.Host) {
        throw 'The attachment and InvenTree instance hosts do not match.'
    }

    if ($allowedHosts -notcontains $attachmentUrl.Host.ToLowerInvariant()) {
        throw "The InvenTree host '$($attachmentUrl.Host)' is not allowed by the helper configuration."
    }

    $extension = [IO.Path]::GetExtension($filename).ToLowerInvariant()
    if ($extension -notin @('.3mf', '.step', '.stp')) {
        throw 'Only 3MF, STEP and STP files are allowed.'
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $sessionKey = '{0}|part|{1}' -f $instanceUrl.GetLeftPart([UriPartial]::Authority), $partId
        $hashBytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sessionKey))
    }
    finally {
        $sha.Dispose()
    }

    $hash = -join ($hashBytes | ForEach-Object { $_.ToString('x2') })
    $partFolder = Join-Path (Join-Path $helperRoot 'Models') $hash.Substring(0, 16)
    New-Item -ItemType Directory -Path $partFolder -Force | Out-Null

    $targetPath = Join-Path $partFolder $filename
    $downloadPath = Join-Path $partFolder ($filename + '.download')
    $expectedProjectPath = [IO.Path]::ChangeExtension($targetPath, '.3mf')

    Invoke-WebRequest -Uri $attachmentUrl.AbsoluteUri -OutFile $downloadPath -UseBasicParsing -MaximumRedirection 0
    Move-Item -LiteralPath $downloadPath -Destination $targetPath -Force

    $session = [ordered]@{
        instanceUrl = $instanceUrl.GetLeftPart([UriPartial]::Authority)
        partId = $partId
        sourceAttachmentId = $attachmentId
        threeMfAttachmentId = if ($extension -eq '.3mf') { $attachmentId } else { $null }
        sourceFilename = $filename
        sourcePath = $targetPath
        expectedProjectPath = $expectedProjectPath
        updatedAt = (Get-Date).ToUniversalTime().ToString('o')
    }

    $temporarySessionPath = $sessionPath + '.tmp'
    $session | ConvertTo-Json | Set-Content -LiteralPath $temporarySessionPath -Encoding UTF8
    Move-Item -LiteralPath $temporarySessionPath -Destination $sessionPath -Force

    Start-TrayIfNeeded
    Write-HelperLog "Opening $targetPath for InvenTree Part $partId"
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
        "Could not open the model in Bambu Studio.`n`n$($_.Exception.Message)`n`nLog: $logPath",
        'InvenTree Bambu Helper',
        'OK',
        'Error'
    ) | Out-Null
    exit 1
}
