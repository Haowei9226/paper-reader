param([switch] $Desktop)
$ErrorActionPreference = 'Stop'
$readerRoot = $PSScriptRoot
$readerPython = Join-Path $readerRoot '.venv\Scripts\pythonw.exe'
$readerLauncher = Join-Path $readerRoot 'Launch-Reader.pyw'
$readerIcon = Join-Path $readerRoot 'assets\reader.ico'
foreach ($readerFile in @($readerPython, $readerLauncher, $readerIcon)) {
    if (-not (Test-Path -LiteralPath $readerFile -PathType Leaf)) {
        throw "Missing $readerFile. Run Setup.cmd first."
    }
}

$readerShell = New-Object -ComObject WScript.Shell
$readerName = Split-Path $readerRoot -Leaf
$readerDirectories = @($readerRoot)
if ($Desktop) { $readerDirectories += [Environment]::GetFolderPath('DesktopDirectory') }
$readerArguments = '-B "{0}"' -f $readerLauncher
foreach ($readerDirectory in $readerDirectories) {
    $readerLinkPath = Join-Path $readerDirectory ($readerName + '.lnk')
    if (Test-Path -LiteralPath $readerLinkPath) {
        $readerExisting = $readerShell.CreateShortcut($readerLinkPath)
        if ($readerExisting.TargetPath -ne $readerPython -or $readerExisting.Arguments -ne $readerArguments) {
            $readerLinkPath = Join-Path $readerDirectory ($readerName + ' (Paper Reader).lnk')
            if (Test-Path -LiteralPath $readerLinkPath) {
                $readerExisting = $readerShell.CreateShortcut($readerLinkPath)
                if ($readerExisting.TargetPath -ne $readerPython -or $readerExisting.Arguments -ne $readerArguments) {
                    throw "This shortcut already exists and points to a different app: $readerLinkPath"
                }
            }
        }
    }
    $readerShortcut = $readerShell.CreateShortcut($readerLinkPath)
    $readerShortcut.TargetPath = $readerPython
    $readerShortcut.Arguments = $readerArguments
    $readerShortcut.WorkingDirectory = $readerRoot
    $readerShortcut.IconLocation = "$readerIcon,0"
    $readerShortcut.Description = 'Open the local bilingual literature reader'
    $readerShortcut.Save()
    Write-Output $readerLinkPath
}
