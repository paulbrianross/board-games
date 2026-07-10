# run_pipeline.ps1 -- data-refresh orchestrator.
#
# Runs the feed script(s), then commits + pushes any new data to GitHub.
# Built to be called unattended by Task Scheduler, but safe to run by hand.
#
# Design: the feed scripts are "dumb" data producers (they just save files);
# THIS wrapper owns the repo-level concern of committing + pushing. A single
# commit captures the whole run rather than each script reaching into git.
#
# Feeds/units are INDEPENDENT: if one fails, the others' output is still
# committed -- a bad feed never blocks a good one. Steps WITHIN a unit are
# sequential (e.g. the BGA game-list build only runs if its fetch succeeded).
# Currently wired in:
#   - Feed 1  : bgg-csv download            (feeds/bgg-csv/download_bgg_ranks.py)
#   - Feed 3a : BGA game-list unit           (feeds/bga/fetch_game_list.py
#                                             -> feeds/bga/build_games_csv.py)
# NOT wired in yet: the BGA ELO scrape (feeds/bga/scrape_elo.py) -- it needs a
# supervised first run before it's trusted to the unattended pipeline.

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

# Write-Host (not Write-Output) on purpose: Log is called from inside Invoke-Step,
# and Write-Output there would land in that function's return value and corrupt
# the exit code we read back. Write-Host goes to the console + (via Add-Content)
# the file, but never into the pipeline.
function Log($msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

# Every deliberate exit routes through Finish, so the log ALWAYS ends with one
# status line carrying the outcome + exit code. That makes the ABSENCE of such a
# line a definitive signal that the run was killed externally (reboot / Task
# Scheduler timeout / power loss) rather than finishing on its own -- the
# ambiguity that made the 2026-07-09 failure hard to read.
#   OK-PUSHED / OK-NOOP           : all feeds succeeded
#   PARTIAL-PUSHED / PARTIAL-NOOP : some feed failed, but the run still finished
#                                   and committed whatever succeeded (exit 1 so
#                                   the failure is visible + the task retries)
#   ERROR / FATAL                 : the run itself failed (git error / crash)
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

# Run one feed script, streaming its output into the log LIVE, and return its
# exit code. Streaming (rather than capturing to a temp file read afterwards)
# means a mid-run kill still leaves the partial output in pipeline.log -- the
# 2026-07-09 failure stranded everything in a temp file that was never read.
# Three details make it work:
#   - cmd /c "... 2>&1": merge stdout+stderr in order INSIDE cmd, so PS 5.1
#     doesn't wrap native stderr as error records (which 'Stop' would turn into
#     a terminating error).
#   - python -u: unbuffered, so each print reaches us as it happens.
#   - piping into Log writes AND flushes each line immediately.
# $LASTEXITCODE still carries python's real exit code after the pipeline; we use
# it, NOT $?, which is unreliable for native executables in Windows PowerShell.
function Invoke-Step($label, $scriptRelPath) {
    Log "Running $label..."
    $script = Join-Path $RepoRoot $scriptRelPath
    cmd /c "python -u `"$script`" 2>&1" | ForEach-Object { Log "  [$label] $_" }
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Log "$label FAILED (exit $code)."
    } else {
        Log "$label OK."
    }
    return $code
}

Log '=== pipeline run start ==='

# Track which feeds failed so we can still commit the ones that succeeded, then
# report a PARTIAL status. An empty list at the end means a clean run.
$failures = @()

# --- Feed 1: BGG bulk CSV download -----------------------------------------
if ((Invoke-Step 'bgg-csv download' 'feeds\bgg-csv\download_bgg_ranks.py') -ne 0) {
    $failures += 'bgg-csv'
}

# --- Feed 3a: BGA game-list unit (fetch -> build) --------------------------
# Sequential within the unit: only build the CSV if the fetch produced its JSON.
$fetchCode = Invoke-Step 'bga game-list fetch' 'feeds\bga\fetch_game_list.py'
if ($fetchCode -eq 0) {
    if ((Invoke-Step 'bga build-games-csv' 'feeds\bga\build_games_csv.py') -ne 0) {
        $failures += 'bga-game-list'
    }
} else {
    Log 'Skipping bga build-games-csv because the fetch failed.'
    $failures += 'bga-game-list'
}

# --- commit + push whatever the successful feeds produced ------------------
# Out-String so a multi-line result is one string for the emptiness check.
$changes = (git status --porcelain | Out-String)
if ([string]::IsNullOrWhiteSpace($changes)) {
    if ($failures.Count -gt 0) {
        Log ('No changes to commit, and these feeds failed: ' + ($failures -join ', ') + '.')
        Finish 'PARTIAL-NOOP' 1
    }
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

if ($failures.Count -gt 0) {
    Log ('Pushed to GitHub OK, but these feeds failed: ' + ($failures -join ', ') + '.')
    Finish 'PARTIAL-PUSHED' 1
}
Log 'Pushed to GitHub OK.'
Finish 'OK-PUSHED' 0
