param(
    [Parameter(Mandatory = $true)][string]$Parts,        # directory from split-sparse.py
    [string]$Fastboot = "fastboot",                      # path to fastboot.exe
    [string]$Partition = "userdata",
    [string]$Log = "flash-parts.log"
)

# Flash the sparse parts produced by split-sparse.py, in order.
#
# The device must already be in fastbootd (`fastboot getvar is-userspace` -> yes):
# on the m2391 the bootloader accepts sparse images and silently writes nothing,
# while fastbootd honours them and allows 256 MiB per transfer.

$ErrorActionPreference = 'SilentlyContinue'
Remove-Item $Log -ErrorAction SilentlyContinue
$parts = Get-ChildItem $Parts -Filter "$Partition.part*.sparse" | Sort-Object Name
if (-not $parts) { Write-Error "no parts in $Parts"; exit 1 }
Add-Content $Log "start $(Get-Date -Format 'HH:mm:ss') parts=$($parts.Count)"

$failed = 0
foreach ($p in $parts) {
    $t0 = Get-Date
    $out = & $Fastboot flash $Partition $p.FullName 2>&1
    $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $ok = ($out | Select-String -Pattern "Finished|OKAY" | Measure-Object).Count -gt 0
    $status = if ($ok) { "OK" } else { "FAIL"; }
    if (-not $ok) { $failed++ }
    Add-Content $Log ("{0} {1,12:N0}B {2,7:N1}s {3}" -f $p.Name, $p.Length, $secs, $status)
    if (-not $ok) { $out | Select-Object -Last 6 | ForEach-Object { Add-Content $Log ("    " + $_) } }
}
Add-Content $Log "end $(Get-Date -Format 'HH:mm:ss') failed=$failed"
Get-Content $Log | Select-Object -Last 4
exit $failed
