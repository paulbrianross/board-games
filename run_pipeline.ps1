# run_pipeline.ps1 -- data-refresh orchestrator.
#
# Runs the feed script(s), then commits + pushes any new data to GitHub.
# Built to be called unattended by Task Scheduler, but safe to run by hand.
#
# Design: the feed scripts are "dumb" data producers (they just save files);
# THIS wrapper owns the repo-level concern of committing + pushing.
#
# Feeds/units are INDEPENDENT: if one fails, the others' output is still
# committed -- a bad feed never blocks a good one. Steps WITHIN a unit are
# sequential (e.g. the BGA game-list build only runs if its fetch succeeded).
#
# TWO commit phases, on purpose (see elo-wiring-plan.md):
#   Phase A -- Feed 1 (bgg-csv) + Feed 3a (BGA game list): run, then commit+push.
#   Phase B -- Feed 3b (BGA ELO): runs LAST, then a SEPARATE commit+push.
# The cheap feeds are banked BEFORE the 10-15 min ELO scrape is attempted, so a
# hang/crash/kill during ELO can never strand feeds 1 & 2 uncommitted. ELO is
# best-effort: a partial or empty ELO day is acceptable and never blocks or
# reverts the Phase A commit.
#   Feed 1  : bgg-csv download    (feeds/bgg-csv/download_bgg_ranks.py)
#   Feed 3a : BGA game-list unit  (feeds/bga/fetch_game_list.py -> build_games_csv.py)
#   Feed 3b : BGA ELO scrape      (feeds/bga/scrape_elo.py) -- resume-driven loop

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
# Scheduler timeout / power loss) rather than finishing on its own.
# Final status reads "<feeds>/<elo>", e.g. OK/DONE, PARTIAL(bgg-csv)/PARTIAL:
#   feeds : OK | PARTIAL(<failed feeds>)
#   elo   : DONE (all games) | PARTIAL (some) | NONE (zero) | SKIPPED (due but no
#           game list today) | NOTDUE (weekly rest day -- already captured this
#           week, so ELO deliberately not run)
# exit 1 (so Task Scheduler retries, up to its fixed 2x @15min) whenever a cheap
# feed failed OR ELO did not fully complete. Because a retry RESUMES (scrapes
# only the games not yet in the done-list) it is always cheap -- never a second
# full scrape -- so any not-complete ELO just gets another spaced go until the
# task's retries run out, then we accept whatever was captured. Each run already
# exhausts its own progress internally via the resume loop below; there is no
# cross-run orchestration -- the task's fixed retry count is the outer bound.
# ERROR / FATAL are git errors / unhandled crashes.
function Finish($status, $code) {
    Log "=== pipeline run end (status=$status, exit=$code) ==="
    exit $code
}

# Catch any unhandled terminating error so even an unexpected crash writes a
# final line instead of dying silently. Native git/python failures are handled
# by the explicit $LASTEXITCODE checks below, not here.
trap {
    Log ('FATAL: unhandled error -- ' + ($_ | Out-String).Trim())
    Finish 'FATAL' 1
}

# Run one feed script, streaming its output into the log LIVE, and return its
# exit code. cmd /c "... 2>&1" merges stdout+stderr in order INSIDE cmd so PS 5.1
# doesn't wrap native stderr as error records; python -u is unbuffered so lines
# arrive as they happen; $LASTEXITCODE carries python's real exit code (not $?,
# unreliable for native exes in Windows PowerShell).
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

# Commit + push whatever is currently staged-able in the working tree, under the
# given message. Returns 'PUSHED' if it committed, 'NOOP' if nothing changed. A
# git failure is fatal (Finish 'ERROR'): if we can't commit/push we must not
# pretend success. Called once per commit phase.
function Commit-Push($message) {
    # Scope the emptiness check to data/ to match `git add data/` below: if only
    # code changed (no new data), there's nothing for the pipeline to commit, so
    # this must read NOOP -- not proceed and then fail on an empty commit.
    $changes = (git status --porcelain -- data | Out-String)
    if ([string]::IsNullOrWhiteSpace($changes)) {
        Log "Nothing to commit for '$message'."
        return 'NOOP'
    }
    Log ("Changes detected for '$message':`n" + $changes.TrimEnd())
    # Stage ONLY data/, never the whole tree: the pipeline's job is to commit the
    # feeds' output, and every feed writes under data/. `git add -A` here would
    # sweep up any uncommitted code you happen to be mid-edit on when the daily
    # task fires, committing + pushing it under a "Data refresh" message. Code is
    # committed deliberately by hand, never by the pipeline.
    git add data/
    if ($LASTEXITCODE -ne 0) { Log 'ERROR: git add failed.'; Finish 'ERROR' 1 }
    git commit -m $message
    if ($LASTEXITCODE -ne 0) { Log 'ERROR: git commit failed.'; Finish 'ERROR' 1 }
    git push origin main
    if ($LASTEXITCODE -ne 0) { Log 'ERROR: git push failed.'; Finish 'ERROR' 1 }
    Log "Pushed to GitHub OK: '$message'."
    return 'PUSHED'
}

# Data rows in a CSV = lines minus the header (0 if absent/empty).
function Count-DataLines($path) {
    if (-not (Test-Path $path)) { return 0 }
    $n = @(Get-Content -Path $path).Count
    if ($n -le 1) { return 0 }
    return $n - 1
}

# Was day <dateStrD> (yyyyMMdd) a COMPLETE ELO capture? True iff that day's
# done-list has a line for every game in that day's game list. Both files are
# committed + dated, so this reads pure history -- the basis for the weekly gate.
# A day with no game list (count 0) can't be judged complete -> false.
function Test-EloCompleteForDay($dateStrD) {
    $games = Join-Path $RepoRoot "data\bga\bga_games_$dateStrD.csv"
    $done  = Join-Path $RepoRoot "data\bga\elo\bga_elo_done_$dateStrD.csv"
    $totalD = Count-DataLines $games
    if ($totalD -le 0) { return $false }
    return ((Count-DataLines $done) -ge $totalD)
}

Log '=== pipeline run start ==='

$stamp = Get-Date -Format 'yyyy-MM-dd'
$dateStr = Get-Date -Format 'yyyyMMdd'

# Track which cheap feeds failed so we can still commit the ones that succeeded,
# then report a PARTIAL status (exit 1 -> the task retries the cheap feeds).
$feedFailures = @()

# =========================================================================
# PHASE A -- cheap feeds (bank these BEFORE the long ELO scrape)
# =========================================================================

# --- Feed 1: BGG bulk CSV download -----------------------------------------
if ((Invoke-Step 'bgg-csv download' 'feeds\bgg-csv\download_bgg_ranks.py') -ne 0) {
    $feedFailures += 'bgg-csv'
}

# --- Feed 3a: BGA game-list unit (fetch -> build) --------------------------
# Sequential within the unit: only build the CSV if the fetch produced its JSON.
$fetchCode = Invoke-Step 'bga game-list fetch' 'feeds\bga\fetch_game_list.py'
if ($fetchCode -eq 0) {
    if ((Invoke-Step 'bga build-games-csv' 'feeds\bga\build_games_csv.py') -ne 0) {
        $feedFailures += 'bga-game-list'
    }
} else {
    Log 'Skipping bga build-games-csv because the fetch failed.'
    $feedFailures += 'bga-game-list'
}

# --- Phase A commit: bank feeds 1 & 2 before ELO ---------------------------
Commit-Push "Data refresh $stamp" | Out-Null

# =========================================================================
# PHASE B -- Feed 3b: BGA ELO (runs LAST; best-effort; separate commit)
# =========================================================================
# Resume-driven progress loop: each pass scrapes only the games not yet in
# today's done-list; the done-list line count IS the progress counter.
#   done == total  -> DONE, stop
#   grew this pass -> resume (another immediate pass -- recovers a scraper that
#                     died mid-pass without waiting for the 15-min task retry)
#   no growth      -> stop (this run can't get further right now)
# No hard pass cap -- progress is the brake, and the task's 1h runtime cap is
# the ultimate backstop against a pathological slow drip. If the run ends
# not-complete, the final exit code is non-zero so the task's 15-min retry gives
# it another (resuming) go; run_id keeps climbing so nothing is re-scraped.
$gamesFile = Join-Path $RepoRoot "data\bga\bga_games_$dateStr.csv"
$doneFile  = Join-Path $RepoRoot "data\bga\elo\bga_elo_done_$dateStr.csv"
$eloStatus = 'SKIPPED'

# --- Weekly gate (see elo-wiring-plan.md): scrape ELO at most once a week, on a
# Sunday, with daily catch-up until a complete capture lands, then rest again.
# ELO's footprint (~1,300 requests / ~30 min) is polite per-request but too
# conspicuous to run daily for no gain -- weekly snapshots carry the same signal.
# Derived purely from the committed done-lists, no separate state: "this week" =
# most recent Sunday (inclusive) .. today. If ANY day in that window already has
# a COMPLETE ELO capture, today is a rest day (NOTDUE). Otherwise ELO is due --
# Sunday's normal run, or a catch-up on Mon/Tue/... after a missed/failed Sunday.
$today = (Get-Date).Date
$lastSunday = $today.AddDays(-[int]$today.DayOfWeek)   # DayOfWeek enum: Sunday = 0
$capturedDay = $null
for ($d = $lastSunday; $d -le $today; $d = $d.AddDays(1)) {
    if (Test-EloCompleteForDay ($d.ToString('yyyyMMdd'))) { $capturedDay = $d; break }
}

if ($null -ne $capturedDay) {
    Log ('ELO already captured this week ({0}) -- weekly cadence, standing down until next Sunday (NOTDUE).' -f $capturedDay.ToString('yyyy-MM-dd'))
    $eloStatus = 'NOTDUE'
} elseif (-not (Test-Path $gamesFile)) {
    Log 'ELO due this week, but no game list for today -- ELO stands down (SKIPPED).'
} else {
    $total = Count-DataLines $gamesFile
    Log "ELO: $total games expected today."
    $after = 0
    while ($true) {
        $before = Count-DataLines $doneFile
        Invoke-Step 'bga elo scrape' 'feeds\bga\scrape_elo.py' | Out-Null
        $after = Count-DataLines $doneFile
        if ($after -ge $total) { break }
        if ($after -gt $before) { Log "ELO progress: $after/$total -- resuming."; continue }
        break
    }
    if ($after -ge $total) {
        Log "ELO complete: $after/$total."
        $eloStatus = 'DONE'
    } elseif ($after -gt 0) {
        Log "ELO partial: $after/$total -- will retry via the task."
        $eloStatus = 'PARTIAL'
    } else {
        Log "ELO captured nothing (0/$total) -- will retry via the task."
        $eloStatus = 'NONE'
    }
    # --- Phase B commit: whatever ELO captured (complete or partial) --------
    Commit-Push "BGA ELO $stamp" | Out-Null
}

# =========================================================================
# Final status + exit code
# =========================================================================
if ($feedFailures.Count -gt 0) {
    $feedStatus = 'PARTIAL(' + ($feedFailures -join ',') + ')'
} else {
    $feedStatus = 'OK'
}
# Exit 1 (-> the task's 2x @15min retry) whenever a cheap feed failed OR ELO was
# DUE but did not fully complete. NOTDUE (a deliberate weekly rest day) is a
# success like DONE and must NOT trigger a retry -- only PARTIAL/NONE/SKIPPED do.
# The retry resumes, so it is always cheap; once the task's retries run out we
# simply keep whatever was captured.
$retry = ($feedFailures.Count -gt 0) -or (($eloStatus -ne 'DONE') -and ($eloStatus -ne 'NOTDUE'))
$code = if ($retry) { 1 } else { 0 }
Finish "$feedStatus/$eloStatus" $code
