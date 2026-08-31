[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId,

    [Parameter()]
    [string]$HostExecutable = '',

    [Parameter()]
    [string]$DatabasePath = (
        Join-Path $env:LOCALAPPDATA 'game-stack-planner\catalog.sqlite3'
    ),

    [Parameter()]
    [string]$InstallDirectory = (
        Join-Path $env:LOCALAPPDATA 'GameStackPlanner\NativeMessaging'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$hostName = 'jp.game_stack_planner'
$allowedOrigin = "chrome-extension://$ExtensionId/"

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw 'LOCALAPPDATA is required for a per-user installation.'
}

if ([string]::IsNullOrWhiteSpace($HostExecutable)) {
    $hostCommand = Get-Command `
        -Name 'fafnir-native-host.exe' `
        -CommandType Application `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $hostCommand) {
        $hostCommand = Get-Command `
            -Name 'game-stack-native-host.exe' `
            -CommandType Application `
            -ErrorAction Stop |
            Select-Object -First 1
    }
    $HostExecutable = $hostCommand.Source
}
$resolvedHostExecutable = (
    Resolve-Path -LiteralPath $HostExecutable -ErrorAction Stop
).Path
if (-not (Test-Path -LiteralPath $resolvedHostExecutable -PathType Leaf)) {
    throw "Native host executable was not found: $resolvedHostExecutable"
}
if ([System.IO.Path]::GetExtension($resolvedHostExecutable) -ne '.exe') {
    throw 'HostExecutable must be the installed Windows .exe entry point.'
}

$absoluteInstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
$manifestPath = Join-Path $absoluteInstallDirectory "$hostName.json"
$configurationPath = Join-Path $absoluteInstallDirectory 'host_config.json'
$absoluteDatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
if ($absoluteDatabasePath.StartsWith('\\')) {
    throw 'DatabasePath must be on a local filesystem.'
}

foreach ($pathValue in @(
    $resolvedHostExecutable,
    $manifestPath,
    $configurationPath,
    $absoluteDatabasePath
)) {
    if ($pathValue -match "[%`r`n]") {
        throw "Native Messaging paths must not contain %, CR, or LF: $pathValue"
    }
}

$hostManifest = [ordered]@{
    name = $hostName
    description = 'Fafnir Unity Asset Store candidate and owned RAG receiver'
    path = $resolvedHostExecutable
    type = 'stdio'
    allowed_origins = @($allowedOrigin)
}
$hostConfiguration = [ordered]@{
    allowed_origin = $allowedOrigin
    database_path = $absoluteDatabasePath
}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

if ($PSCmdlet.ShouldProcess(
    $absoluteInstallDirectory,
    'Install the Fafnir Native Messaging host'
)) {
    New-Item -ItemType Directory -Path $absoluteInstallDirectory -Force |
        Out-Null
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($hostManifest | ConvertTo-Json -Depth 4),
        $utf8NoBom
    )
    [System.IO.File]::WriteAllText(
        $configurationPath,
        ($hostConfiguration | ConvertTo-Json -Depth 3),
        $utf8NoBom
    )
}

$registrySubKey = "Software\Google\Chrome\NativeMessagingHosts\$hostName"
if ($PSCmdlet.ShouldProcess(
    "HKCU:\$registrySubKey",
    'Register the host in 32-bit and 64-bit registry views'
)) {
    foreach ($view in @(
        [Microsoft.Win32.RegistryView]::Registry32,
        [Microsoft.Win32.RegistryView]::Registry64
    )) {
        $baseKey = $null
        $hostKey = $null
        try {
            $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
                [Microsoft.Win32.RegistryHive]::CurrentUser,
                $view
            )
            $hostKey = $baseKey.CreateSubKey($registrySubKey, $true)
            if ($null -eq $hostKey) {
                throw "Could not create registry key in $view view."
            }
            $hostKey.SetValue(
                '',
                $manifestPath,
                [Microsoft.Win32.RegistryValueKind]::String
            )
        }
        finally {
            if ($null -ne $hostKey) { $hostKey.Dispose() }
            if ($null -ne $baseKey) { $baseKey.Dispose() }
        }
    }
}

Write-Output "Native Messaging host manifest: $manifestPath"
Write-Output "Native Messaging host configuration: $configurationPath"
Write-Output "Catalog database: $absoluteDatabasePath"
Write-Output "Allowed extension origin: $allowedOrigin"
