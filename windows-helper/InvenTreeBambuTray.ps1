param(
    [switch]$UploadNow
)

$ErrorActionPreference = 'Stop'
$helperRoot = Join-Path $env:LOCALAPPDATA 'InvenTreeBambuOpen'
$sessionPath = Join-Path $helperRoot 'current-session.json'
$configPath = Join-Path $helperRoot 'config.json'
$tokenPath = Join-Path $helperRoot 'token.dat'
$logPath = Join-Path $helperRoot 'helper.log'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Net.Http

function Write-HelperLog {
    param([string]$Message)
    $line = '{0:u} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Show-HelperMessage {
    param(
        [string]$Message,
        [string]$Title = 'InvenTree Bambu Helper',
        [System.Windows.Forms.MessageBoxIcon]$Icon = [System.Windows.Forms.MessageBoxIcon]::Information
    )

    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        $Title,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        $Icon
    ) | Out-Null
}

function Get-ApiToken {
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        throw 'API token is not configured. Run Install-InvenTreeBambuHelper.ps1 again.'
    }

    $encryptedToken = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    $secureToken = $encryptedToken | ConvertTo-SecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Get-CurrentSession {
    if (-not (Test-Path -LiteralPath $sessionPath)) {
        throw 'No active InvenTree model. Open a model using the 3D Print button first.'
    }

    return Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
}

function Save-CurrentSession {
    param([object]$Session)

    $temporaryPath = $sessionPath + '.tmp'
    $Session.updatedAt = (Get-Date).ToUniversalTime().ToString('o')
    $Session | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $sessionPath -Force
}

function Select-ProjectFile {
    param([object]$Session)

    if (Test-Path -LiteralPath $Session.expectedProjectPath) {
        return $Session.expectedProjectPath
    }

    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Select the Bambu Studio project to save in InvenTree'
    $dialog.Filter = 'Bambu Studio project (*.3mf)|*.3mf'
    $dialog.CheckFileExists = $true
    $dialog.InitialDirectory = [IO.Path]::GetDirectoryName($Session.sourcePath)
    $dialog.FileName = [IO.Path]::GetFileName($Session.expectedProjectPath)

    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        return $null
    }

    return $dialog.FileName
}

function Upload-ProjectToInvenTree {
    $session = Get-CurrentSession
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $instanceUrl = [Uri]$session.instanceUrl
    $allowedHosts = @($config.allowedHosts) | ForEach-Object { $_.ToString().ToLowerInvariant() }

    if ($instanceUrl.Scheme -ne 'https' -or $allowedHosts -notcontains $instanceUrl.Host.ToLowerInvariant()) {
        throw 'The active InvenTree instance is not allowed by the helper configuration.'
    }

    $projectPath = Select-ProjectFile -Session $session
    if (-not $projectPath) {
        return
    }

    if ([IO.Path]::GetExtension($projectPath).ToLowerInvariant() -ne '.3mf') {
        throw 'Only a Bambu Studio .3mf project can be saved to InvenTree.'
    }

    $apiToken = Get-ApiToken
    $filename = [IO.Path]::GetFileName($projectPath)
    $attachmentId = $session.threeMfAttachmentId
    $endpoint = if ($attachmentId) {
        '{0}/api/attachment/{1}/' -f $session.instanceUrl.TrimEnd('/'), $attachmentId
    }
    else {
        '{0}/api/attachment/' -f $session.instanceUrl.TrimEnd('/')
    }

    $client = New-Object System.Net.Http.HttpClient
    $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Token', $apiToken)
    $multipart = New-Object System.Net.Http.MultipartFormDataContent
    $fileStream = [IO.File]::Open($projectPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $fileContent = New-Object System.Net.Http.StreamContent($fileStream)
    $fileContent.Headers.ContentType = New-Object System.Net.Http.Headers.MediaTypeHeaderValue('application/octet-stream')
    $multipart.Add($fileContent, 'attachment', $filename)

    if ($attachmentId) {
        $multipart.Add((New-Object System.Net.Http.StringContent($filename)), 'filename')
    }
    else {
        $multipart.Add((New-Object System.Net.Http.StringContent('part')), 'model_type')
        $multipart.Add((New-Object System.Net.Http.StringContent([string]$session.partId)), 'model_id')
        $multipart.Add((New-Object System.Net.Http.StringContent('Saved from Bambu Studio')), 'comment')
    }

    $method = if ($attachmentId) { New-Object System.Net.Http.HttpMethod('PATCH') } else { [System.Net.Http.HttpMethod]::Post }
    $request = New-Object System.Net.Http.HttpRequestMessage($method, $endpoint)
    $request.Content = $multipart

    try {
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        $responseBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

        if (-not $response.IsSuccessStatusCode) {
            throw "InvenTree returned HTTP $([int]$response.StatusCode): $responseBody"
        }

        $result = $responseBody | ConvertFrom-Json
        if (-not $attachmentId -and $result.pk) {
            $session.threeMfAttachmentId = [int]$result.pk
        }

        $session.expectedProjectPath = $projectPath
        Save-CurrentSession -Session $session
        Write-HelperLog "Uploaded $filename to InvenTree Part $($session.partId)"
        return $filename
    }
    finally {
        $request.Dispose()
        $multipart.Dispose()
        $fileContent.Dispose()
        $fileStream.Dispose()
        $client.Dispose()
        $apiToken = $null
    }
}

if ($UploadNow) {
    try {
        $uploadedFilename = Upload-ProjectToInvenTree
        if ($uploadedFilename) {
            Write-Output $uploadedFilename
        }
        exit 0
    }
    catch {
        $details = '{0} | {1}' -f $_.Exception.Message, $_.ScriptStackTrace
        Write-HelperLog ("ERROR: " + $details)
        Write-Error $details
        exit 1
    }
}

$createdNew = $false
$mutex = New-Object Threading.Mutex($true, 'Local\InvenTreeBambuTray', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$bambuPath = 'C:\Program Files\Bambu Studio\bambu-studio.exe'
if (Test-Path -LiteralPath $bambuPath) {
    $notifyIcon.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($bambuPath)
}
else {
    $notifyIcon.Icon = [System.Drawing.SystemIcons]::Application
}
$notifyIcon.Text = 'InvenTree Bambu Helper'
$notifyIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$saveItem = $menu.Items.Add('Save 3MF to InvenTree')
$folderItem = $menu.Items.Add('Open current model folder')
$logItem = $menu.Items.Add('Open log')
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$exitItem = $menu.Items.Add('Exit')
$notifyIcon.ContextMenuStrip = $menu

$saveItem.Add_Click({
    try {
        $uploadedFilename = Upload-ProjectToInvenTree
        if ($uploadedFilename) {
            $notifyIcon.ShowBalloonTip(
                5000,
                'InvenTree',
                "$uploadedFilename saved to InvenTree",
                [System.Windows.Forms.ToolTipIcon]::Info
            )
        }
    }
    catch {
        Write-HelperLog ("ERROR: " + $_.Exception.Message)
        Show-HelperMessage -Message $_.Exception.Message -Icon ([System.Windows.Forms.MessageBoxIcon]::Error)
    }
})

$folderItem.Add_Click({
    try {
        $session = Get-CurrentSession
        Start-Process -FilePath 'explorer.exe' -ArgumentList @('/select,', ('"{0}"' -f $session.sourcePath))
    }
    catch {
        Show-HelperMessage -Message $_.Exception.Message -Icon ([System.Windows.Forms.MessageBoxIcon]::Error)
    }
})

$logItem.Add_Click({
    if (-not (Test-Path -LiteralPath $logPath)) {
        New-Item -ItemType File -Path $logPath -Force | Out-Null
    }
    Start-Process -FilePath $logPath
})

$exitItem.Add_Click({ [System.Windows.Forms.Application]::Exit() })
$notifyIcon.Add_DoubleClick({ $saveItem.PerformClick() })

try {
    Write-HelperLog 'Tray helper started'
    [System.Windows.Forms.Application]::Run()
}
finally {
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    $menu.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
