param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Text,

    [string]$ApiBase = 'http://127.0.0.1:8880',
    [string]$Voice = 'am_echo',
    [double]$Speed = 1.0,
    [double]$VolumeMultiplier = 1.0,
    [string]$LangCode = 'a',
    [string]$Model = 'kokoro',
    [int]$TimeoutSec = 90,
    [string]$OutputPath,
    [bool]$PlayInBackground = $true,
    [int]$SampleRate = 24000
)

$ErrorActionPreference = 'Stop'

function Write-WavFromPcm {
    param(
        [Parameter(Mandatory = $true)][string]$PcmPath,
        [Parameter(Mandatory = $true)][string]$WavPath,
        [int]$SampleRate = 24000,
        [int]$Channels = 1,
        [int]$BitsPerSample = 16
    )

    [byte[]]$pcm = [System.IO.File]::ReadAllBytes($PcmPath)
    $dataSize = $pcm.Length
    $byteRate = $SampleRate * $Channels * ($BitsPerSample / 8)
    $blockAlign = $Channels * ($BitsPerSample / 8)
    $riffSize = 36 + $dataSize

    $fs = [System.IO.File]::Open($WavPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try {
        $bw = New-Object System.IO.BinaryWriter($fs)
        $bw.Write([System.Text.Encoding]::ASCII.GetBytes('RIFF'))
        $bw.Write([int]$riffSize)
        $bw.Write([System.Text.Encoding]::ASCII.GetBytes('WAVE'))
        $bw.Write([System.Text.Encoding]::ASCII.GetBytes('fmt '))
        $bw.Write([int]16)
        $bw.Write([int16]1)
        $bw.Write([int16]$Channels)
        $bw.Write([int]$SampleRate)
        $bw.Write([int]$byteRate)
        $bw.Write([int16]$blockAlign)
        $bw.Write([int16]$BitsPerSample)
        $bw.Write([System.Text.Encoding]::ASCII.GetBytes('data'))
        $bw.Write([int]$dataSize)
        $bw.Write($pcm)
        $bw.Flush()
    }
    finally {
        $fs.Dispose()
    }
}

$endpoint = ($ApiBase.TrimEnd('/')) + '/v1/audio/speech'
$usedTempPath = $false
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $env:TEMP ("kokoro_report_" + [guid]::NewGuid().ToString('N') + '.wav')
    $usedTempPath = $true
} elseif (-not $OutputPath.EndsWith('.wav', [System.StringComparison]::OrdinalIgnoreCase)) {
    $OutputPath = "$OutputPath.wav"
}

$pcmPath = Join-Path $env:TEMP ("kokoro_report_pcm_" + [guid]::NewGuid().ToString('N') + '.pcm')

$payload = [ordered]@{
    model = $Model
    input = $Text
    voice = $Voice
    response_format = 'pcm'
    download_format = 'pcm'
    speed = $Speed
    stream = $false
    return_download_link = $false
    lang_code = $LangCode
    volume_multiplier = $VolumeMultiplier
    normalization_options = [ordered]@{
        normalize = $true
        unit_normalization = $false
        url_normalization = $true
        email_normalization = $true
        optional_pluralization_normalization = $true
        phone_normalization = $true
        replace_remaining_symbols = $true
    }
}

try {
    $json = $payload | ConvertTo-Json -Depth 8

    Invoke-WebRequest `
        -Uri $endpoint `
        -Method Post `
        -ContentType 'application/json' `
        -Body $json `
        -OutFile $pcmPath `
        -TimeoutSec $TimeoutSec | Out-Null

    if (-not (Test-Path $pcmPath)) {
        throw 'No PCM audio file was written.'
    }

    $pcmSize = (Get-Item $pcmPath).Length
    if ($pcmSize -le 0) {
        throw "PCM response appears empty ($pcmSize bytes)."
    }

    Write-WavFromPcm -PcmPath $pcmPath -WavPath $OutputPath -SampleRate $SampleRate

    if (-not (Test-Path $OutputPath)) {
        throw 'No WAV audio file was written.'
    }

    $wavSize = (Get-Item $OutputPath).Length
    if ($wavSize -le 44) {
        throw "WAV response appears empty ($wavSize bytes)."
    }

    if ($PlayInBackground) {
        $queueDir = Join-Path $env:TEMP "kokoro_report_queue"
        $workerPath = Join-Path $queueDir "queue_worker.ps1"
        $launcherVbsPath = Join-Path $queueDir "run_hidden.vbs"
        $lockPath = Join-Path $queueDir "worker.lock"
        $taskName = "KokoroReportQueueWorker"

        New-Item -ItemType Directory -Path $queueDir -Force | Out-Null

        $job = [ordered]@{
            path = $OutputPath
            cleanup = $usedTempPath
            created_utc = [DateTime]::UtcNow.ToString("o")
        }
        $jobName = ("{0:D20}_{1}.json" -f [DateTime]::UtcNow.Ticks, [guid]::NewGuid().ToString('N'))
        $jobPath = Join-Path $queueDir $jobName
        $job | ConvertTo-Json -Compress | Set-Content -Path $jobPath -Encoding ASCII

        $escapedQueueDir = $queueDir.Replace("'", "''")
        $escapedLockPath = $lockPath.Replace("'", "''")
        $workerScript = @"
`$queueDir = '$escapedQueueDir'
`$lockPath = '$escapedLockPath'
`$lockStream = `$null
try {
    `$lockStream = [System.IO.File]::Open(`$lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    exit 0
}

try {
    while (`$true) {
        `$jobFile = Get-ChildItem -Path `$queueDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 1
        if (-not `$jobFile) { break }

        `$path = `$null
        `$cleanup = `$false
        try {
            `$job = Get-Content -Path `$jobFile.FullName -Raw | ConvertFrom-Json
            `$path = [string]`$job.path
            `$cleanup = [bool]`$job.cleanup
        } catch {
            Remove-Item -LiteralPath `$jobFile.FullName -Force -ErrorAction SilentlyContinue
            continue
        }

        Remove-Item -LiteralPath `$jobFile.FullName -Force -ErrorAction SilentlyContinue

        if ([string]::IsNullOrWhiteSpace(`$path)) { continue }
        if (-not (Test-Path -LiteralPath `$path)) { continue }

        try {
            `$player = New-Object System.Media.SoundPlayer `$path
            `$player.PlaySync()
        } catch {}

        if (`$cleanup -and (Test-Path -LiteralPath `$path)) {
            Remove-Item -LiteralPath `$path -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    if (`$lockStream -ne `$null) {
        `$lockStream.Dispose()
    }
}
"@
        Set-Content -Path $workerPath -Value $workerScript -Encoding ASCII
        $launcherScript = @'
Dim shell, psScript, cmd
If WScript.Arguments.Count = 0 Then
  WScript.Quit 1
End If
psScript = WScript.Arguments(0)
Set shell = CreateObject("WScript.Shell")
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & psScript & """"
shell.Run cmd, 0, False
'@
        Set-Content -Path $launcherVbsPath -Value $launcherScript -Encoding ASCII

        try {
            $startTime = (Get-Date).AddMinutes(1).ToString("HH:mm")
            $taskCmd = "wscript.exe //B //NoLogo `"$launcherVbsPath`" `"$workerPath`""
            schtasks /Create /TN $taskName /SC ONCE /ST $startTime /TR $taskCmd /F | Out-Null
            schtasks /Run /TN $taskName | Out-Null
        }
        catch {
            Start-Process -FilePath 'wscript.exe' `
                -WindowStyle Hidden `
                -ArgumentList @('//B', '//NoLogo', $launcherVbsPath, $workerPath) | Out-Null
        }

        Write-Output "Queued detached playback: $OutputPath ($wavSize bytes)"
    } else {
        $player = New-Object System.Media.SoundPlayer $OutputPath
        $player.PlaySync()
        Write-Output "Played: $OutputPath ($wavSize bytes)"
        if ($usedTempPath -and (Test-Path $OutputPath)) {
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    if (Test-Path $pcmPath) {
        Remove-Item $pcmPath -Force -ErrorAction SilentlyContinue
    }
}
