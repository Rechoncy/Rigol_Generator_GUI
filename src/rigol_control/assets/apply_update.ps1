param([Parameter(Mandatory=$true)][string]$Ticket)
$ErrorActionPreference = 'Stop'
$handoff = $null
function Write-State([string]$phase, [string]$message) {
    $body = @{phase=$phase; message=$message; utc=[DateTime]::UtcNow.ToString('o')}
    $body | ConvertTo-Json -Compress | Set-Content -LiteralPath $handoff.status_file -Encoding UTF8
}
try {
    $handoff = Get-Content -LiteralPath $Ticket -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($handoff.format -ne 'lab-control-center-install-handoff' -or $handoff.schema_version -ne 1) { throw 'Invalid update ticket' }
    $parent = Get-Process -Id $handoff.parent_pid -ErrorAction Stop
    $null = $parent.Handle # Keep the original process handle, even if its PID is reused.
    Write-State 'waiting-parent' '升级程序已就绪，等待原程序退出。'
    while (-not $parent.WaitForExit(250)) {
        if (Test-Path -LiteralPath $handoff.cancel_file) { Write-State 'cancelled' '已取消升级。'; exit 2 }
    }
    if (Test-Path -LiteralPath $handoff.cancel_file) { Write-State 'cancelled' '已取消升级。'; exit 2 }
    if (-not (Test-Path -LiteralPath $handoff.commit_file -PathType Leaf)) { Write-State 'cancelled' '原程序未确认升级，已停止。'; exit 2 }
    $commit = Get-Content -LiteralPath $handoff.commit_file -Raw -Encoding UTF8
    if ($commit.Trim() -ne $handoff.nonce) { throw 'Invalid upgrade confirmation' }
    $installer = Get-Item -LiteralPath $handoff.installer -ErrorAction Stop
    if ($installer.Length -ne $handoff.bytes) { throw 'Installer size changed; upgrade stopped' }
    # Inherited module settings can leave Get-FileHash unavailable in Windows
    # PowerShell 5. Use .NET without depending on that function's autoloading.
    $stream = [IO.File]::OpenRead($installer.FullName)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
    if ($digest -ne $handoff.sha256) { throw 'Installer checksum changed; upgrade stopped' }
    Write-State 'installing' '正在安装更新并保留用户数据。'
    # All paths are ticket data, passed as arguments, never evaluated as script.
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOAUTORUN',
                   ('/DATADIR="' + $handoff.data_dir + '"'), ('/DIR="' + $handoff.install_dir + '"'),
                   ('/LOG="' + $handoff.install_log + '"'))
    $process = Start-Process -FilePath $installer.FullName -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw ('安装程序返回错误 ' + $process.ExitCode + '。数据及升级备份仍保留。') }
    if (-not (Test-Path -LiteralPath $handoff.application -PathType Leaf)) { throw '安装后未找到应用，请从安装包重新安装。' }
    Write-State 'installed' '更新安装完成，用户数据已校验恢复。'
    Start-Process -FilePath $handoff.application -ArgumentList @('--data-dir', ('"' + $handoff.data_dir + '"')) -WindowStyle Normal
    exit 0
} catch {
    $message = $_.Exception.Message
    if ($handoff) {
        try { Write-State 'failed' $message } catch {}
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(('更新安装未完成。' + [Environment]::NewLine + $message + [Environment]::NewLine + '用户数据和备份仍保留，请查看下载目录内的安装日志。'), '实验室设备中心更新', 'OK', 'Error') | Out-Null
    } catch {}
    exit 1
}
