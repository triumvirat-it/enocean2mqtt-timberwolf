<#
.SYNOPSIS
  Prueft, packt und veroeffentlicht enocean2mqtt -- nur ueber die Positivliste.

.DESCRIPTION
  Ohne Schalter: nur pruefen. Laeuft auch als pre-push-Haken.
    1. Jede Datei im Stand muss auf release-liste.txt stehen.
    2. Kein Zugangsdatum (Token, Schluessel, Tresor-Verweis) im Stand und in
       den noch nicht gepushten Commits.
    3. Nichts aus der lokalen Sperrliste (release-sperrliste.txt, nicht im
       Repo) -- dort stehen anlagenspezifische Daten: echte EnOcean-IDs,
       IP-Adressen, Namen.
  Fundstellen werden nur mit Datei und Anzahl gemeldet, nie mit Inhalt.

  -Paket            baut dist/enocean2mqtt-source.tar.gz fuer Portainer
                    ("Build a new image"): Stand aus git, ohne test-data/,
                    Dockerfile im Hauptordner.
  -Veroeffentlichen legt das GitHub-Release v<Version> an und haengt das
                    Paket an. Braucht $env:GITHUB_TOKEN, einen gepushten Stand
                    und einen Abschnitt "## <Version>" in CHANGELOG.md.

  Warum es das gibt: docs/Veroeffentlichen.md.

.EXAMPLE
  .\release.ps1
  .\release.ps1 -Paket
  $env:GITHUB_TOKEN = '...'; .\release.ps1 -Veroeffentlichen
#>
param(
    [switch]$Paket,
    [switch]$Veroeffentlichen,
    [string]$Ref = 'HEAD',
    [string]$Gegenstelle = 'origin/main'
)

# Nicht 'Stop': Windows PowerShell 5.1 macht aus jeder stderr-Zeile von git
# einen Abbruch. git-Fehler werden ueber $LASTEXITCODE geprueft, Datei- und
# Webaufrufe brechen einzeln mit -ErrorAction Stop ab.
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Set-Location $PSScriptRoot
$fehler = New-Object System.Collections.Generic.List[string]

function Melde([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

# --- Version ---------------------------------------------------------------
$init = (git show "${Ref}:app/__init__.py") -join "`n"
$version = ([regex]::Match($init, '__version__\s*=\s*"([^"]+)"')).Groups[1].Value
if (-not $version) { throw "Version in app/__init__.py nicht gefunden" }
Melde "enocean2mqtt $version ($Ref)"

# --- 1. Positivliste ------------------------------------------------------
$roh = git show "${Ref}:release-liste.txt" 2>$null
if ($LASTEXITCODE -ne 0) { throw "release-liste.txt fehlt im Stand $Ref -- erst committen" }
$liste = @($roh | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })

function Erlaubt([string]$pfad) {
    foreach ($e in $liste) {
        if ($e.EndsWith('/')) { if ($pfad.StartsWith($e)) { return $true } }
        elseif ($pfad -like $e) { return $true }
    }
    return $false
}

$dateien = @(git -c core.quotepath=off ls-tree -r --name-only $Ref)
foreach ($f in $dateien) {
    if (-not (Erlaubt $f)) { $fehler.Add("nicht auf der Positivliste: $f") }
}

# --- 2./3. Inhalt ---------------------------------------------------------
$muster = [ordered]@{
    'GitHub-Token'        = 'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
    'Tresor-Verweis'      = 'op:/{2}'   # 1Password; so geschrieben, damit es sich nicht selbst findet
    'Privater Schluessel' = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
    'Slack-Token'         = 'xox[abprs]-[A-Za-z0-9-]{10,}'
    'AWS-Schluessel'      = 'AKIA[0-9A-Z]{16}'
}
$sperrDatei = Join-Path $PSScriptRoot 'release-sperrliste.txt'
if (Test-Path $sperrDatei) {
    $n = 0
    foreach ($z in [IO.File]::ReadAllLines($sperrDatei, [Text.Encoding]::UTF8)) {
        $z = $z.Trim()
        if (-not $z -or $z.StartsWith('#')) { continue }
        $n++
        $muster["Sperrliste Zeile $n"] = $z
    }
    Melde "Sperrliste: $n Eintraege"
} else {
    Write-Host "WARNUNG: release-sperrliste.txt fehlt -- es werden nur Zugangsdaten gesucht, keine Anlagendaten." -ForegroundColor Yellow
}

# a) der ganze Stand
$musterDatei = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllLines($musterDatei, [string[]]@($muster.Values), (New-Object Text.UTF8Encoding $false))
    $treffer = @(git -c core.quotepath=off grep -I -i -c -E -f $musterDatei $Ref 2>$null)
} finally { Remove-Item $musterDatei -ErrorAction SilentlyContinue }
foreach ($t in $treffer) {
    # Ausgabe: <ref>:<pfad>:<anzahl>
    $teile = $t -split ':'
    $pfad = $teile[1..($teile.Count - 2)] -join ':'
    $namen = @($muster.Keys | Where-Object {
        git -c core.quotepath=off grep -I -i -q -E -e $muster[$_] $Ref -- $pfad 2>$null
        $LASTEXITCODE -eq 0
    })
    $fehler.Add("Fund in ${pfad}: $($namen -join ', ')")
}

# b) Zeilen, die in noch nicht gepushten Commits dazugekommen sind -- auch
#    solche, die ein spaeterer Commit wieder entfernt hat: sie gingen sonst
#    mit der Historie hinaus.
git rev-parse -q --verify $Gegenstelle *> $null
if ($LASTEXITCODE -eq 0) {
    $neu = @(git log -p --format= "$Gegenstelle..$Ref" | Where-Object { $_.StartsWith('+') -and -not $_.StartsWith('+++') })
    foreach ($name in $muster.Keys) {
        $re = New-Object Text.RegularExpressions.Regex($muster[$name], 'IgnoreCase')
        $anzahl = @($neu | Where-Object { $re.IsMatch($_) }).Count
        if ($anzahl) { $fehler.Add("Fund in noch nicht gepushten Commits ($Gegenstelle..$Ref): $name, $anzahl Zeile(n)") }
    }
} else {
    Write-Host "WARNUNG: $Gegenstelle unbekannt -- Commits vor dem Push nicht einzeln geprueft." -ForegroundColor Yellow
}

# --- Changelog --------------------------------------------------------------
$changelog = (git show "${Ref}:CHANGELOG.md" 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0) { $fehler.Add("CHANGELOG.md fehlt im Stand $Ref") }
$abschnitt = [regex]::Match($changelog,
    "(?ms)^## $([regex]::Escape($version))[ \t][^\n]*\n(.*?)(?=^---[ \t]*$|^## |\z)")
if ($Veroeffentlichen -and -not $abschnitt.Success) {
    $fehler.Add("CHANGELOG.md hat keinen Abschnitt '## $version'")
}

if ($fehler.Count) {
    Write-Host "ABBRUCH -- nichts veroeffentlichen:" -ForegroundColor Red
    $fehler | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Melde "Pruefung bestanden: $($dateien.Count) Dateien, keine Funde"

# --- Paket ------------------------------------------------------------------
$ziel = Join-Path $PSScriptRoot 'dist\enocean2mqtt-source.tar.gz'
if ($Paket -or $Veroeffentlichen) {
    # Windows-bsdtar, nicht das GNU-tar aus Git: bsdtar packt mit UID 0, das
    # braucht der Portainer-Build.
    $tar = Join-Path $env:WINDIR 'System32\tar.exe'
    if (-not (Test-Path $tar)) { $tar = 'tar' }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("e2m-release-" + [guid]::NewGuid().ToString('N'))
    $src = Join-Path $tmp 'src'
    New-Item -ItemType Directory -Force $src | Out-Null
    try {
        git archive --format=tar -o (Join-Path $tmp 'export.tar') $Ref
        if ($LASTEXITCODE -ne 0) { throw "git archive ist fehlgeschlagen" }
        & $tar -xf (Join-Path $tmp 'export.tar') -C $src
        if ($LASTEXITCODE -ge 2) { throw "Entpacken ist fehlgeschlagen" }
        $td = Join-Path $src 'test-data'
        if (Test-Path $td) { Remove-Item -Recurse -Force $td -ErrorAction Stop }
        Copy-Item (Join-Path $src 'docker\Dockerfile') (Join-Path $src 'Dockerfile') -ErrorAction Stop
        New-Item -ItemType Directory -Force (Split-Path $ziel) -ErrorAction Stop | Out-Null
        if (Test-Path $ziel) { Remove-Item $ziel -ErrorAction Stop }
        Push-Location $src
        try { & $tar -czf $ziel . ; $rc = $LASTEXITCODE } finally { Pop-Location }
        if ($rc -ge 2 -or -not (Test-Path $ziel)) { throw "tar ist mit $rc abgebrochen" }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
    Melde "Paket: dist\enocean2mqtt-source.tar.gz ($([math]::Round((Get-Item $ziel).Length / 1KB)) KB)"
}

# --- GitHub-Release ---------------------------------------------------------
if ($Veroeffentlichen) {
    if (-not $env:GITHUB_TOKEN) { throw 'GITHUB_TOKEN ist nicht gesetzt' }
    git fetch -q origin
    $sha = (git rev-parse "$Ref^{commit}").Trim()
    $stand = (git rev-parse $Gegenstelle).Trim()
    if ($sha -ne $stand) { throw "$Ref ist nicht gepusht ($Gegenstelle steht woanders) -- erst pushen" }

    $remote = (git remote get-url origin).Trim()
    $slug = ([regex]::Match($remote, 'github\.com[:/](.+?)(\.git)?$')).Groups[1].Value
    $tag = "v$version"
    $hdr = @{
        Authorization          = "Bearer $($env:GITHUB_TOKEN)"
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
    }
    $gibtEs = $true
    try { Invoke-RestMethod "https://api.github.com/repos/$slug/releases/tags/$tag" -Headers $hdr -ErrorAction Stop | Out-Null }
    catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { $gibtEs = $false } else { throw } }
    if ($gibtEs) { throw "Release $tag gibt es schon -- Version in app/__init__.py erhoehen" }

    # Relative Links aus dem Changelog zeigen im Release ins Leere.
    $text = [regex]::Replace($abschnitt.Groups[1].Value.Trim(),
        '\]\((?!https?://|#)([^)]+)\)', "](https://github.com/$slug/blob/$tag/`$1)")
    $text += "`n`n---`n`n**Download ``enocean2mqtt-source.tar.gz``**: deploy-fertiges Source-Bundle " +
             "inklusive Dockerfile fuer ""Build a new image"" in Portainer. Enthaelt nur " +
             "Beispiel-/Platzhalterdaten. Einspielen: [DEPLOY.md](https://github.com/$slug/blob/$tag/DEPLOY.md), " +
             "Einschraenkungen: [KNOWN_LIMITATIONS.md](https://github.com/$slug/blob/$tag/KNOWN_LIMITATIONS.md)."

    $json = @{ tag_name = $tag; target_commitish = $sha; name = $tag; body = $text
               draft = $false; prerelease = $false; make_latest = 'true' } | ConvertTo-Json
    $rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$slug/releases" -Headers $hdr `
        -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType 'application/json; charset=utf-8' -ErrorAction Stop
    $upload = ($rel.upload_url -replace '\{.*\}', '') + '?name=enocean2mqtt-source.tar.gz'
    Invoke-RestMethod -Method Post -Uri $upload -Headers $hdr -InFile $ziel -ContentType 'application/gzip' -ErrorAction Stop | Out-Null
    Melde "Release $tag veroeffentlicht: $($rel.html_url)"
}
