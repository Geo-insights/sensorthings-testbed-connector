<#
.SYNOPSIS
    Run an unattended Claude Code session while keeping Windows awake.

.DESCRIPTION
    Long autonomous runs (`claude -p --dangerously-skip-permissions`) die when the
    machine sleeps mid-task. This wrapper asks Windows to keep the system awake for
    as long as the child process runs, then releases the request on exit.

    Uses SetThreadExecutionState (kernel32), which needs no admin rights and leaves
    no state behind if the script is killed -- the request dies with the process.

    Limits (be aware before trusting it with a multi-hour run):
      * The display is still allowed to sleep; only the system is kept awake.
      * On Modern Standby (S0) laptops, closing the lid or pressing the power
        button still sleeps the machine. This only blocks the *idle* timeout.
      * Hibernate after a fixed period, and battery-critical actions, still apply.
        For runs on battery, also check `powercfg /q` or plug the laptop in.

.PARAMETER Prompt
    The task prompt. Must include the done-gate command and an explicit exit
    condition, per the rules in CLAUDE.md.

.PARAMETER MaxTurns
    Turn cap for the session (default 30; raise to 50 for larger tasks).

.PARAMETER KeepDisplayOn
    Also keep the display awake. Off by default -- the screen does not need to be
    on for the run to finish.

.EXAMPLE
    .\scripts\run_autonomous.ps1 -Prompt "Fix the KNMI station id drift. Exit when done-gate passes (pytest + ruff + mypy clean)."

.EXAMPLE
    .\scripts\run_autonomous.ps1 -MaxTurns 50 -Prompt "..." -ExtraArgs @('--model','opus')
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Prompt,

    [int]$MaxTurns = 30,

    [switch]$KeepDisplayOn,

    [string[]]$ExtraArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "claude CLI not found on PATH."
}

Add-Type -Namespace GiPower -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@

$ES_CONTINUOUS       = [uint32]'0x80000000'
$ES_SYSTEM_REQUIRED  = [uint32]'0x00000001'
$ES_DISPLAY_REQUIRED = [uint32]'0x00000002'

$flags = $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED
if ($KeepDisplayOn) { $flags = $flags -bor $ES_DISPLAY_REQUIRED }

# Returns the previous state, or 0 on failure.
$previous = [GiPower.Native]::SetThreadExecutionState($flags)
if ($previous -eq 0) {
    Write-Warning "Could not request a sleep block (SetThreadExecutionState returned 0, Win32 error $([System.Runtime.InteropServices.Marshal]::GetLastWin32Error())). The run continues, but the machine may sleep."
} else {
    $what = if ($KeepDisplayOn) { 'system + display' } else { 'system' }
    Write-Host "Sleep block active ($what). Released when this script exits."
}

$exitCode = 1
try {
    $claudeArgs = @(
        '--dangerously-skip-permissions'
        '-p', $Prompt
        '--max-turns', $MaxTurns
    ) + $ExtraArgs

    & claude @claudeArgs
    $exitCode = $LASTEXITCODE
}
finally {
    # ES_CONTINUOUS on its own clears the request and restores normal idle timers.
    [void][GiPower.Native]::SetThreadExecutionState($ES_CONTINUOUS)
    Write-Host "Sleep block released."
}

exit $exitCode
