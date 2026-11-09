param([Parameter(Mandatory=$true)][string]$Ticket)
$ErrorActionPreference = 'Stop'
$handoff = $null
function Write-State([string]$phase, [string]$message) {
    $body = @{phase=$phase; message=$message; utc=[DateTime]::UtcNow.ToString('o')}
    $temporary = $handoff.status_file + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        # Readers must see a complete JSON document. The temporary file lives
        # beside the destination so Move/Replace publishes it atomically.
        $json = ($body | ConvertTo-Json -Compress) + [Environment]::NewLine
        [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($true))
        while ($true) {
            try {
                if ([IO.File]::Exists($handoff.status_file)) {
                    # Windows PowerShell 5 otherwise converts $null to an
                    # empty backup path for this .NET string parameter.
                    [IO.File]::Replace($temporary, $handoff.status_file, [NullString]::Value)
                } else {
                    [IO.File]::Move($temporary, $handoff.status_file)
                }
                return
            } catch {
                # A status reader may briefly omit FILE_SHARE_DELETE/WRITE.
                # Retry only this idempotent publication, never an installer
                # or application action. Other I/O errors fail immediately.
                $cause = $_.Exception
                while ($cause.InnerException) { $cause = $cause.InnerException }
                $nativeCode = $cause.HResult -band 0xFFFF
                if (-not ($cause -is [IO.IOException]) -or $nativeCode -notin @(32, 33) -or $clock.ElapsedMilliseconds -ge 3000) { throw }
                Start-Sleep -Milliseconds 25
            }
        }
    } finally {
        $clock.Stop()
        if ([IO.File]::Exists($temporary)) {
            try { [IO.File]::Delete($temporary) }
            catch { [Console]::Error.WriteLine('无法清理临时升级状态文件：' + $_.Exception.Message) }
        }
    }
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
