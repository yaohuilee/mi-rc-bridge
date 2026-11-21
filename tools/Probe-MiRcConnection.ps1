# Read public GATT characteristics on the paired MI RC so the bridge can
# identify its existing HCI connection in the USB capture.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')]
    [string]$RemoteAddress
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$le = [Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$cache = [Windows.Devices.Bluetooth.BluetoothCacheMode, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$serviceType = [Windows.Devices.Bluetooth.GenericAttributeProfile.GattDeviceServicesResult, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$characteristicType = [Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicsResult, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$readType = [Windows.Devices.Bluetooth.GenericAttributeProfile.GattReadResult, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
$readFlag = [Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]::Read
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Wait-WinRt($operation, [type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (!$task.Wait(4000)) { throw 'The Bluetooth read timed out.' }
    $task.Result
}

$numericAddress = [Convert]::ToUInt64($RemoteAddress.Replace(':', ''), 16)
$device = Wait-WinRt ($le::FromBluetoothAddressAsync($numericAddress)) $le
if (!$device) { throw 'The paired MI RC was not found.' }
try {
    if (!$device.DeviceInformation.Pairing.IsPaired -or $device.ConnectionStatus -ne 'Connected') {
        throw 'MI RC is not currently connected. Wake it with any button.'
    }
    # Try values from Device Information before the battery level.
    foreach ($id in @('180a', '180f')) {
        $uuid = [guid]"0000$id-0000-1000-8000-00805f9b34fb"
        $result = Wait-WinRt ($device.GetGattServicesForUuidAsync($uuid, $cache::Cached)) $serviceType
        foreach ($service in $result.Services) {
            try {
                $items = Wait-WinRt ($service.GetCharacteristicsAsync($cache::Cached)) $characteristicType
                foreach ($characteristic in $items.Characteristics) {
                    if ($characteristic.Uuid.ToString().Substring(4,4) -notin @('2a24','2a29','2a19') -or
                        !$characteristic.CharacteristicProperties.HasFlag($readFlag)) { continue }
                    $start = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                    $read = Wait-WinRt ($characteristic.ReadValueAsync($cache::Uncached)) $readType
                    if ($read.Status -ne 'Success') { continue }
                    [pscustomobject]@{
                        # For this MI RC's DIS/BAS table, the value handle is
                        # one higher than WinRT's characteristic declaration handle.
                        handle = $characteristic.AttributeHandle + 1
                        start = $start
                        end = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                    } | ConvertTo-Json -Compress
                }
            } finally { $service.Dispose() }
        }
    }
} finally { $device.Dispose() }
