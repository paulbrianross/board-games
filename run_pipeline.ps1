# run_pipeline.ps1 -- data-refresh orchestrator.
#
# Runs the feed script(s), then commits + pushes any new data to GitHub.
# Built to be called unattended by Task Scheduler, but safe to run by hand.
#
# Design: the feed scripts are "dumb" data producers (they just save files);
# THIS wrapper owns the repo-level concern of committing + pushing. As more
# feeds are added, they get run here too, and a single commit captures the
# whole run -- rather than each script reaching into git on its own.

$ErrorActionPreference = 'Stop'

# Resolve the repo root from this script's own location, so it works no matter
# what directory the scheduler invokes it from.
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

# --- logging ---------------------------------------------------------------
# Log to a gitignored logs/ folder so run history is available for the "verify
# it's really working" step, without the logs themselves polluting the repo.
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir 'pipeline.log'

function Log($msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

# Every deliberate exit routes through Finish, so the log ALWAYS ends with one
# status line carrying the outcome + exit code. That makes the ABSENCE of such a
# line a definitive signal that the run was killed externally (reboot / Task
# Scheduler timeout / power loss) rather than finishing on its own -- the
# ambiguity that made the 2026-07-09 failure hard to read.
function Finish($status, $code) {
    Log "=== pipeline run end (status=$status, exit=$code) ==="
    exit $code
}

# Catch any unhandled terminating error (a failing cmdlet, a null reference --
# $ErrorActionPreference='Stop' makes most errors terminating) so even an
# unexpected crash writes a final line instead of dying silently. Native
# git/python failures are handled by the explicit $LASTEXITCODE checks below,
# not here, since a non-zero exit code is not a PowerShell error.
trap {
    Log ('FATAL: unhandled error -- ' + ($_ | Out-String).Trim())
    Finish 'FATAL' 1
}

Log '=== pipeline run start ==='

# --- 1. run the feed(s) ----------------------------------------------------
Log 'Running bgg-csv download...'
$downloadScript = Join-Path $RepoRoot 'feeds\bgg-csv\download_bgg_ranks.py'
# Stream the script's output into our log LIVE, one line at a time, so a mid-run
# kill still leaves the partial output in pipeline.log. The 2026-07-09 failure
# stranded everything in a temp file that was only read AFTER the step returned
# -- which it never did -- so the log went silent and the run was undiagnosable.
# Three details make the streaming work:
#   - cmd /c "... 2>&1": merge stdout+stderr in order INSIDE cmd, so PS 5.1
#     doesn't wrap native stderr as error records (which $ErrorActionPreference
#     = 'Stop' would then turn into a terminating error).
#   - python -u: run unbuffered, so each print reaches us as it happens instead
#     of sitting in python's stdout buffer until the process exits.
#   - piping straight into Log writes AND flushes each line immediately.
# $LASTEXITCODE still carries python's real exit code after the pipeline.
cmd /c "python -u `"$downloadScript`" 2>&1" | ForEach-Object { Log "  [download] $_" }
$exit = $LASTEXITCODE
# Use the captured native exit code, NOT $?, which is unreliable for native
# executables in Windows PowerShell.
if ($exit -ne 0) {
    Log "ERROR: download script exited $exit -- aborting, nothing pushed."
    Finish 'ERROR' 1
}
Log 'Download step OK.'

# --- 2. commit + push any new data ----------------------------------------
# Out-String so a multi-line result is one string for the emptiness check.
$changes = (git status --porcelain | Out-String)
if ([string]::IsNullOrWhiteSpace($changes)) {
    Log 'No changes in working tree -- nothing to commit. Done.'
    Finish 'OK-NOOP' 0
}
Log ("Changes detected:`n" + $changes.TrimEnd())

git add -A
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git add failed.'; Finish 'ERROR' 1 }

$stamp = Get-Date -Format 'yyyy-MM-dd'
git commit -m "Data refresh $stamp"
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git commit failed.'; Finish 'ERROR' 1 }

git push origin main
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git push failed.'; Finish 'ERROR' 1 }

Log 'Pushed to GitHub OK.'
Finish 'OK-PUSHED' 0
