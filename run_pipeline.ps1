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
python (Join-Path $RepoRoot 'feeds\bgg-csv\download_bgg_ranks.py')
# Use $LASTEXITCODE (the native exit code), NOT $?, which is unreliable for
# native executables in Windows PowerShell.
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: download script exited $LASTEXITCODE -- aborting, nothing pushed."
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
