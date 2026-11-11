# Native Windows CPU-only runner. No bash, WSL, Docker, GPU, or testnet.
# Full logs and exit codes are retained even when a suite fails.
#   powershell -ExecutionPolicy Bypass -File infra\run-tests-host.ps1 [-Suite fast|settlement|secure-settlement|chain|quality-smoke|all]
param(
    [ValidateSet('all', 'fast', 'settlement', 'secure-settlement', 'chain', 'quality-smoke')][string]$Suite = 'all'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot '.cache\cpu-venv\Scripts\python.exe'
$forge = Join-Path $repoRoot '.cache\foundry\forge.exe'
$anvil = Join-Path $repoRoot '.cache\foundry\anvil.exe'
$runId = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$logDir = Join-Path $repoRoot "experiments\results\host_tests_$runId"
$results = [System.Collections.Generic.List[object]]::new()
$chainProcess = $null
$watchdogProcess = $null
$runError = $null

function Assert-Memory {
    $available = [int](Get-Counter '\Memory\Available MBytes' -ErrorAction Stop).CounterSamples[0].CookedValue
    if ($available -lt 3584) { throw "CPU tests need 3584 MB available; currently $available MB. Close unused apps before retrying." }
    if ($watchdogProcess) {
        $watchdogProcess.Refresh()
        if ($watchdogProcess.HasExited) { throw 'Memory watchdog stopped; no more suites will start. Check its logs.' }
    }
    return $available
}

function Invoke-Suite([string]$Name, [string]$Directory, [string]$Executable, [string[]]$Arguments) {
    $available = Assert-Memory
    $logPath = Join-Path $logDir "$Name.log"
    Write-Host "=== $Name (available $available MB)"
    $started = Get-Date
    Push-Location $Directory
    try {
        # Native stderr is evidence, including Python warnings and full tracebacks.
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments *> $logPath
        $code = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
    } finally { Pop-Location }
    $results.Add([pscustomobject]@{suite=$Name; exit_code=$code; seconds=[math]::Round(((Get-Date)-$started).TotalSeconds, 3); log="$Name.log"})
    if ($code -eq 0) { Get-Content -LiteralPath $logPath -Tail 6 } else { Get-Content -LiteralPath $logPath }
    if ($code -ne 0) { Write-Host "FAILED: $Name; full log: $logPath" }
}

function Start-LocalChain {
    if (Get-NetTCPConnection -LocalPort 18545 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 18545 is occupied; refusing to reuse or stop an unknown chain.' }
    $script:chainProcess = Start-Process -FilePath $anvil -ArgumentList @('--host','127.0.0.1','--port','18545','--silent') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'anvil.stdout.log') -RedirectStandardError (Join-Path $logDir 'anvil.stderr.log')
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $chainProcess.Refresh()
        if ($chainProcess.HasExited) { throw 'Local Anvil exited before it became ready.' }
        try {
            $reply = Invoke-RestMethod -Uri 'http://127.0.0.1:18545' -Method Post -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' -TimeoutSec 2
            if ($reply.result -eq '0x7a69') { return }
        } catch { }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    throw 'Local Anvil did not become ready in 15 seconds.'
}

$savedEnv = @{}
foreach ($name in @('PYTHONPATH','PYTHONDONTWRITEBYTECODE','PYTHONUTF8','ANVIL_RPC')) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .cache/cpu-venv; host dependencies must be prepared first.' }
    if (-not (Test-Path -LiteralPath $forge -PathType Leaf)) { throw 'Missing .cache/foundry/forge.exe; refusing to start host tests.' }
    if ($Suite -notin @('fast', 'quality-smoke') -and -not (Test-Path -LiteralPath $anvil -PathType Leaf)) { throw 'Missing .cache/foundry/anvil.exe; refusing to start chain tests.' }
    $startingMb = Assert-Memory
    New-Item -ItemType Directory -Path $logDir | Out-Null
    Write-Host "CPU-only tests. Logs: $logDir"
    $watchdogPath = Join-Path $PSScriptRoot 'memory-watchdog.ps1'
    $watchdogProcess = Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', ('"' + $watchdogPath + '"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'watchdog.stdout.log') -RedirectStandardError (Join-Path $logDir 'watchdog.stderr.log')
    $env:PYTHONPATH = @($repoRoot, "$repoRoot\apps\coordinator", "$repoRoot\packages\gpuchain_core", "$repoRoot\workers\agent", "$repoRoot\workers\runtime", "$repoRoot\workers\verifier") -join ';'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    if ($Suite -notin @('settlement', 'secure-settlement', 'chain', 'quality-smoke')) {
        foreach ($area in @(@('core','packages\gpuchain_core'), @('runtime','workers\runtime'), @('agent','workers\agent'), @('coordinator','apps\coordinator'), @('verifier','workers\verifier'))) {
            Invoke-Suite $area[0] (Join-Path $repoRoot $area[1]) $python @('-m','unittest','discover','-s','tests','-v')
        }
    }
    Invoke-Suite 'contracts' (Join-Path $repoRoot 'contracts') $forge @('test','--offline')
    if ($Suite -eq 'quality-smoke') {
        Invoke-Suite 'quality-smoke' $repoRoot $python @('experiments/quality_comparison.py','--synthetic','--seeds','1','--steps','50','--resume-step','25','--eval-examples','128','--device','cpu')
    } elseif ($Suite -eq 'chain') {
        if (($results | Where-Object { $_.suite -eq 'contracts' }).exit_code -ne 0) { throw 'Contract build/tests failed; chain tests require fresh compiled artifacts.' }
        Invoke-Suite 'e2e-chain' $repoRoot $python @('-m','unittest','tests/test_e2e_chain.py','-v')
    } elseif ($Suite -ne 'fast') {
        if ($Suite -eq 'all') {
            Invoke-Suite 'e2e-recovery' $repoRoot $python @('-m','unittest','tests/test_e2e_recovery.py','-v')
            Invoke-Suite 'e2e-faults' $repoRoot $python @('-m','unittest','tests/test_e2e_faults.py','-v')
        }
        if (($results | Where-Object { $_.suite -eq 'contracts' }).exit_code -ne 0) { throw 'Contract build/tests failed; settlement requires fresh compiled artifacts.' }
        $null = Assert-Memory
        Start-LocalChain
        $env:ANVIL_RPC = 'http://127.0.0.1:18545'
        if ($Suite -eq 'secure-settlement') {
            Invoke-Suite 'e2e-secure-settlement' $repoRoot $python @('-m','unittest','tests.test_e2e_settlement.Settlement.test_secure_real_workers_settle_after_provider_handover','-v')
        } else {
            Invoke-Suite 'e2e-settlement' $repoRoot $python @('-m','unittest','tests/test_e2e_settlement.py','-v')
        }
        if (-not $chainProcess.HasExited) { Stop-Process -Id $chainProcess.Id -Force -ErrorAction SilentlyContinue }
        if ($Suite -eq 'all') { Invoke-Suite 'e2e-chain' $repoRoot $python @('-m','unittest','tests/test_e2e_chain.py','-v') }
    }
} catch {
    $runError = $_.Exception.Message
    Write-Host "STOPPED: $runError"
} finally {
    if ($chainProcess -and -not $chainProcess.HasExited) { Stop-Process -Id $chainProcess.Id -Force -ErrorAction SilentlyContinue }
    if ($watchdogProcess -and -not $watchdogProcess.HasExited) { Stop-Process -Id $watchdogProcess.Id -Force -ErrorAction SilentlyContinue }
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
    if (Test-Path -LiteralPath $logDir) {
        [pscustomobject]@{kind='CPU_SYNTHETIC_LOCAL_CHAIN_TESTS'; suite=$Suite; starting_available_mb=$startingMb; error=$runError; results=@($results.ToArray())} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logDir 'summary.json') -Encoding UTF8
    }
}
if ($runError -or ($results | Where-Object { $_.exit_code -ne 0 })) { exit 1 }
Write-Host 'ALL REQUESTED SUITES PASSED'
exit 0
