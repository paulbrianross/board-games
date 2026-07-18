# run_pipeline.ps1 -- data-refresh orchestrator.
#
# Runs the feed/process script(s), then commits + pushes any new data to GitHub.
# Built to be called unattended by Task Scheduler, but safe to run by hand.
#
# Design: the scripts are "dumb" producers (they just save files); THIS wrapper
# owns the repo-level concern of committing + pushing, and the cadence gates.
#
# GOVERNING RULE (see pipeline-wiring-plan.md): every consumer reads the
# MOST-RECENT file of the kind it needs -- never a hardcoded "today's" file. The
# only place a date survives is the weekly gate, expressed as "take the newest
# file of this kind, is ITS date within this week's window?".
#
# WEEKLY-TUESDAY CADENCE: each feed captures at most once a week, gated on a
# last-Tuesday(inclusive)->today window. A miss is caught up on Wed/Thu/Fri (still
# in-week) rather than waiting a whole week. Gates read committed dated files only
# (pure history; no mtime, which breaks on fresh clones).
#
# FOUR PHASES, each mapping onto a process (processes/<name>/):
#   Phase A -- cheap feeds : bgg-csv download + BGA game-list (fetch->build), each
#              weekly-gated, then ONE commit ("Data refresh"). Banked first.
#   Phase B -- BGA ELO     : weekly-gated; long (~30 min) resume-driven scrape;
#              SEPARATE commit ("BGA ELO"). Best-effort -- never blocks Phase A.
#   Phase C -- combine     : resolver -> care-about filter. Runs IFF Phase A
#              committed new data (its inputs are Phase-A feeds). Transient work/
#              output -- NO commit.
#   Phase D -- bgg-api     : weekly-gated fetch of the care-about set; SEPARATE
#              commit ("BGG API refresh"). Consumes Phase C's care-about list.
# The two long tails (B, D) run after the cheap feeds are safely committed, so a
# hang/crash in either can never strand feeds 1 & 2 uncommitted.

$ErrorActionPreference = 'Stop'

# Resolve the repo root from this script's own location, so it works no matter
# what directory the scheduler invokes it from.
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

$BggCsvDir = Join-Path $RepoRoot 'data\bgg-csv'
$BgaDir    = Join-Path $RepoRoot 'data\bga'
$EloDir    = Join-Path $RepoRoot 'data\bga\elo'
$ApiDir    = Join-Path $RepoRoot 'data\bgg-api'

# --- logging ---------------------------------------------------------------
# Log to a gitignored logs/ folder so run history is available for the "verify
# it's really working" step, without the logs themselves polluting the repo.
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir 'pipeline.log'

# Write-Host (not Write-Output) on purpose: Log is called from inside functions
# whose return value we read back (Invoke-Step, Commit-Push), and Write-Output
# there would land in that return value and corrupt it. Write-Host goes to the
# console + (via Add-Content) the file, but never into the pipeline.
function Log($msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

# Every deliberate exit routes through Finish, so the log ALWAYS ends with one
# status line carrying the outcome + exit code. That makes the ABSENCE of such a
# line a definitive signal that the run was killed externally (reboot / Task
# Scheduler timeout / power loss) rather than finishing on its own.
# Final status reads "<feeds>/<elo>/<combine>/<api>", e.g. OK/DONE/OK/DONE:
#   feeds   : OK | PARTIAL(<failed feeds>)
#   elo     : DONE (complete) | PARTIAL | NONE (zero) | SKIPPED (due, no game
#             list) | NOTDUE (already captured this week)
#   combine : OK | FAILED | NOTNEEDED (Phase A committed nothing new)
#   api     : DONE | FAILED | NOTDUE (captured this week) | SKIPPED (combine failed)
# exit 1 (so Task Scheduler retries, up to its fixed 2x @15min) whenever a feed
# failed, ELO was due but did not complete, combine failed, or the API fetch
# failed. Retries RESUME/are gated, so they are always cheap.
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

# Run one script, streaming its output into the log LIVE, and return its exit
# code. cmd /c "... 2>&1" merges stdout+stderr in order INSIDE cmd so PS 5.1
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

# Run a git command, streaming its merged stdout+stderr into the log, and return
# its exit code. Same cmd /c "... 2>&1" trick as Invoke-Step: git writes some of
# its normal output to stderr (e.g. the push status), and merging the streams
# INSIDE cmd stops PS 5.1 from wrapping that stderr as error records -- which under
# ErrorActionPreference='Stop' could throw on a perfectly good push. $LASTEXITCODE
# carries git's real exit code (cmd /c returns it).
function Invoke-GitLogged($cmdline) {
    cmd /c "$cmdline 2>&1" | ForEach-Object { Log "  [git] $_" }
    return $LASTEXITCODE
}

# Commit + push any new data under the given message. Returns 'PUSHED' if it
# committed, 'NOOP' if nothing changed. A git failure is fatal (Finish 'ERROR'):
# if we can't commit/push we must not pretend success. Called once per commit
# phase; Phase A's return value drives whether Phase C runs.
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
    # Run each git step through Invoke-GitLogged so its output lands in the log
    # (commit summary, push confirmation) instead of in this function's return
    # value -- Phase A reads that value ('NOOP'/'PUSHED') to decide whether Phase C
    # runs, so it must stay a clean token. Invoke-GitLogged sends git's output to
    # Log (Write-Host, off-stream) and returns only the exit code, consumed here.
    if ((Invoke-GitLogged 'git add data/') -ne 0) { Log 'ERROR: git add failed.'; Finish 'ERROR' 1 }
    if ((Invoke-GitLogged "git commit -m ""$message""") -ne 0) { Log 'ERROR: git commit failed.'; Finish 'ERROR' 1 }
    if ((Invoke-GitLogged 'git push origin main') -ne 0) { Log 'ERROR: git push failed.'; Finish 'ERROR' 1 }
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

# The most-recent bga_games_<YYYYMMDD>.csv (FileInfo), or $null if there is none.
# Sorting by Name = chronological (YYYYMMDD sorts lexically). ELO and its gate
# both key on this newest list, not a hardcoded today.
function Get-LatestGameList {
    Get-ChildItem -Path $BgaDir -Filter 'bga_games_*.csv' -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
}

# The per-feed weekly gate: "take the newest file matching $glob in $dir, is ITS
# date (captured by $dateRegex group 1, yyyyMMdd) on or after $windowStart?".
# True = already captured this week -> the feed stands down. Reads committed
# dated files only -- no mtime.
function Test-CapturedThisWeek($dir, $glob, $dateRegex, $windowStart) {
    $newest = Get-ChildItem -Path $dir -Filter $glob -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
    if ($null -eq $newest) { return $false }
    if ($newest.Name -match $dateRegex) {
        $d = [datetime]::ParseExact($Matches[1], 'yyyyMMdd', $null)
        return ($d -ge $windowStart)
    }
    return $false
}

# Is an ELO done-list complete -- does it cover every game in the game-list
# snapshot it scraped (its 'sourced' date)? Reads pure committed history.
function Test-EloDoneComplete($donePath, $sourced) {
    $totalD = Count-DataLines (Join-Path $BgaDir "bga_games_$sourced.csv")
    if ($totalD -le 0) { return $false }
    return ((Count-DataLines $donePath) -ge $totalD)
}

# The ELO weekly gate: has some ELO capture DOWNLOADED this week already completed?
# Scans committed done-lists (named bga_elo_done_sourced<S>_downloaded<D>.csv),
# keys on the DOWNLOADED date being in the window, and uses each file's SOURCED
# date to find the game list for the completeness count. Keying on downloaded (not
# sourced) is the point: a this-week capture scraped against a slightly older game
# list still counts -- the list is ~1,300 near-identical ids week to week, so its
# age is immaterial; the ELO reading is what we're capturing.
function Test-EloCapturedThisWeek($windowStart) {
    $doneFiles = Get-ChildItem -Path $EloDir -Filter 'bga_elo_done_sourced*_downloaded*.csv' -ErrorAction SilentlyContinue
    foreach ($f in $doneFiles) {
        if ($f.Name -match 'sourced(\d{8})_downloaded(\d{8})\.csv$') {
            $sourced = $Matches[1]
            $downloaded = [datetime]::ParseExact($Matches[2], 'yyyyMMdd', $null)
            if (($downloaded -ge $windowStart) -and (Test-EloDoneComplete $f.FullName $sourced)) {
                return $true
            }
        }
    }
    return $false
}

Log '=== pipeline run start ==='

$stamp = Get-Date -Format 'yyyy-MM-dd'

# This week's window: last Tuesday (inclusive) .. today. DayOfWeek enum: Sun=0..
# Sat=6, Tuesday=2. The modulo gives days-since-most-recent-Tuesday (0 on Tue).
$today = (Get-Date).Date
$lastTuesday = $today.AddDays(-(((([int]$today.DayOfWeek) - 2 + 7)) % 7))
Log ("Weekly window: {0} (Tue) .. {1} (today)." -f $lastTuesday.ToString('yyyy-MM-dd'), $today.ToString('yyyy-MM-dd'))

# Track which cheap feeds failed so we can still commit the ones that succeeded,
# then report a PARTIAL status (exit 1 -> the task retries the failed feeds).
$feedFailures = @()

# =========================================================================
# PHASE A -- cheap feeds (weekly-gated; bank these BEFORE the long tails)
# =========================================================================

# --- Feed 1: BGG bulk CSV download -----------------------------------------
if (Test-CapturedThisWeek $BggCsvDir 'boardgames_ranks_*_downloaded*.zip' '_downloaded(\d{8})\.zip$' $lastTuesday) {
    Log 'Feed 1 (bgg-csv) already captured this week -- skipping (weekly cadence).'
} elseif ((Invoke-Step 'bgg-csv download' 'processes\bgg-csv\download_bgg_ranks.py') -ne 0) {
    $feedFailures += 'bgg-csv'
}

# --- Feed 3a: BGA game-list unit (fetch -> build) --------------------------
# Sequential within the unit: only build the CSV if the fetch produced its JSON.
if (Test-CapturedThisWeek $BgaDir 'bga_games_*.csv' 'bga_games_(\d{8})\.csv$' $lastTuesday) {
    Log 'Feed 3a (BGA game list) already captured this week -- skipping (weekly cadence).'
} else {
    $fetchCode = Invoke-Step 'bga game-list fetch' 'processes\bga\fetch_game_list.py'
    if ($fetchCode -eq 0) {
        if ((Invoke-Step 'bga build-games-csv' 'processes\bga\build_games_csv.py') -ne 0) {
            $feedFailures += 'bga-game-list'
        }
    } else {
        Log 'Skipping bga build-games-csv because the fetch failed.'
        $feedFailures += 'bga-game-list'
    }
}

# --- Phase A commit: bank feeds 1 & 3a. Its result drives Phase C. ----------
$phaseAResult = Commit-Push "Data refresh $stamp"

# =========================================================================
# PHASE B -- BGA ELO (weekly-gated; runs against the MOST-RECENT game list)
# =========================================================================
$eloStatus = 'SKIPPED'

# Weekly gate: if some ELO capture DOWNLOADED this week already completed, today is
# a rest day (NOTDUE). Otherwise ELO is due -- Tuesday's normal run, or a Wed..Fri
# catch-up after a missed/failed Tuesday.
if (Test-EloCapturedThisWeek $lastTuesday) {
    Log 'ELO already captured this week -- standing down until next Tuesday (NOTDUE).'
    $eloStatus = 'NOTDUE'
} else {
    $gamesInfo = Get-LatestGameList
    if ($null -eq $gamesInfo) {
        Log 'ELO due this week, but no game list found at all -- ELO stands down (SKIPPED).'
    } else {
        # Name the done-list to match the scraper exactly:
        # bga_elo_done_sourced<gamelist-date>_downloaded<today>.csv. ${..} braces
        # are required because '_' is a valid variable-name char.
        $null = ($gamesInfo.Name -match 'bga_games_(\d{8})\.csv$')
        $sourced = $Matches[1]
        $downloaded = Get-Date -Format 'yyyyMMdd'
        $doneFile = Join-Path $EloDir "bga_elo_done_sourced${sourced}_downloaded${downloaded}.csv"
        $total = Count-DataLines $gamesInfo.FullName
        Log "ELO: $total games expected (sourced $($gamesInfo.Name), downloaded $downloaded)."

        # Resume-driven progress loop: each pass scrapes only games not yet in the
        # done-list; the done-list line count IS the progress counter.
        #   done == total  -> DONE, stop
        #   grew this pass -> resume (recovers a pass that died mid-run without
        #                     waiting for the 15-min task retry)
        #   no growth      -> stop (this run can't get further right now)
        # No hard pass cap -- progress is the brake, the task's 1h cap the backstop.
        $after = 0
        while ($true) {
            $before = Count-DataLines $doneFile
            Invoke-Step 'bga elo scrape' 'processes\bga\scrape_elo.py' | Out-Null
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
        # Phase B commit: whatever ELO captured (complete or partial).
        Commit-Push "BGA ELO $stamp" | Out-Null
    }
}

# =========================================================================
# PHASE C -- combine (resolver -> care-about). Runs IFF Phase A committed.
# =========================================================================
# C's inputs are Phase-A feeds (BGA game list + BGG bulk CSV); ELO is NOT an
# input. So C rebuilds the (transient, gitignored work/) care-about list exactly
# when those inputs changed -- i.e. when Phase A pushed -- and no more. Nothing
# to commit here; the care-about list is reconstructible scratch.
$combineStatus = 'NOTNEEDED'
if ($phaseAResult -eq 'PUSHED') {
    Log 'Phase A committed new data -- running combine (Phase C).'
    if ((Invoke-Step 'combine resolve-bgg-ids' 'processes\combine\resolve_bgg_ids.py') -eq 0) {
        if ((Invoke-Step 'combine build-care-about' 'processes\combine\build_care_about.py') -eq 0) {
            $combineStatus = 'OK'
        } else {
            $combineStatus = 'FAILED'
        }
    } else {
        Log 'Skipping build-care-about because the resolver failed.'
        $combineStatus = 'FAILED'
    }
} else {
    Log 'Phase A committed nothing new -- combine (Phase C) not needed this run.'
}

# =========================================================================
# PHASE D -- bgg-api (weekly-gated fetch of the care-about set; own commit)
# =========================================================================
$apiStatus = 'SKIPPED'
if ($combineStatus -eq 'FAILED') {
    # Inputs changed but the care-about list couldn't be refreshed -- do NOT fetch
    # against a stale/absent list. The FAILED combine already trips a retry.
    Log 'Phase D (bgg-api) skipped -- combine failed, care-about list not refreshed.'
} elseif (Test-CapturedThisWeek $ApiDir 'bgg_api_*.zip' 'bgg_api_(\d{8})\.zip$' $lastTuesday) {
    Log 'Feed D (bgg-api) already captured this week -- standing down (NOTDUE).'
    $apiStatus = 'NOTDUE'
} else {
    # The fetcher reads the most-recent care-about list itself; if none exists it
    # exits non-zero -> FAILED -> retry.
    if ((Invoke-Step 'bgg-api fetch' 'processes\bgg-api\fetch_bgg_api.py') -eq 0) {
        $apiStatus = 'DONE'
        Commit-Push "BGG API refresh $stamp" | Out-Null
    } else {
        $apiStatus = 'FAILED'
    }
}

# =========================================================================
# Final status + exit code
# =========================================================================
if ($feedFailures.Count -gt 0) {
    $feedStatus = 'PARTIAL(' + ($feedFailures -join ',') + ')'
} else {
    $feedStatus = 'OK'
}

# Exit 1 (-> the task's 2x @15min retry) on any not-fully-healthy outcome. NOTDUE
# / NOTNEEDED are deliberate stand-downs and count as success. Retries resume or
# are re-gated, so they stay cheap; once the task's retries run out we keep
# whatever was captured.
$retry = $false
if ($feedFailures.Count -gt 0) { $retry = $true }
if (($eloStatus -ne 'DONE') -and ($eloStatus -ne 'NOTDUE')) { $retry = $true }
if ($combineStatus -eq 'FAILED') { $retry = $true }
if ($apiStatus -eq 'FAILED') { $retry = $true }
$code = if ($retry) { 1 } else { 0 }
Finish "$feedStatus/$eloStatus/$combineStatus/$apiStatus" $code
