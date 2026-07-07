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

Log '=== pipeline run start ==='

# --- 1. run the feed(s) ----------------------------------------------------
Log 'Running bgg-csv download...'
$downloadScript = Join-Path $RepoRoot 'feeds\bgg-csv\download_bgg_ranks.py'
$outTmp = Join-Path $LogDir 'download.tmp'
# Run via cmd so the script's stdout+stderr merge (in order) into a file.
# Doing the 2>&1 redirection inside cmd -- not PowerShell -- sidesteps PS 5.1
# wrapping native stderr as error records. $LASTEXITCODE then carries python's
# real exit code.
cmd /c "python `"$downloadScript`" > `"$outTmp`" 2>&1"
$exit = $LASTEXITCODE
# Fold the script's own output into our log so unattended failures are
# self-diagnosing -- a crash's traceback lands here, not just a bare exit code.
foreach ($line in (Get-Content $outTmp -ErrorAction SilentlyContinue)) {
    Log "  [download] $line"
}
Remove-Item $outTmp -ErrorAction SilentlyContinue
# Use the captured native exit code, NOT $?, which is unreliable for native
# executables in Windows PowerShell.
if ($exit -ne 0) {
    Log "ERROR: download script exited $exit -- aborting, nothing pushed."
    exit 1
}
Log 'Download step OK.'

# --- 2. commit + push any new data ----------------------------------------
# Out-String so a multi-line result is one string for the emptiness check.
$changes = (git status --porcelain | Out-String)
if ([string]::IsNullOrWhiteSpace($changes)) {
    Log 'No changes in working tree -- nothing to commit. Done.'
    exit 0
}
Log ("Changes detected:`n" + $changes.TrimEnd())

git add -A
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git add failed.'; exit 1 }

$stamp = Get-Date -Format 'yyyy-MM-dd'
git commit -m "Data refresh $stamp"
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git commit failed.'; exit 1 }

git push origin main
if ($LASTEXITCODE -ne 0) { Log 'ERROR: git push failed.'; exit 1 }

Log 'Pushed to GitHub OK.'
Log '=== pipeline run end ==='
exit 0
