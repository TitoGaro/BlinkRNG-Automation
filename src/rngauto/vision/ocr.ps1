param([string]$Path)
# OCR integrado de Windows (Windows.Media.Ocr). Usa el idioma del perfil del usuario (las cifras se leen igual
# en cualquier idioma). Salida: una línea por palabra reconocida: «x_centro|y_centro|texto».
$ErrorActionPreference = 'Stop'
# PowerShell 5.1 no sabe esperar operaciones asíncronas de WinRT: se busca por reflexión el método genérico
# AsTask(IAsyncOperation<T>) para convertirlas en Task de .NET y esperarlas.
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
# Espera de forma síncrona una IAsyncOperation que devuelve un valor de tipo $type.
function Await($op, [Type]$type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
# Carga los tipos de WinRT que se usan abajo (sin esto PowerShell no los resuelve por nombre).
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
# Si ningún idioma del perfil tiene OCR instalado, vale cualquier otro: solo se leen cifras.
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    foreach ($lang in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        if ($null -ne $engine) { break }
    }
}
if ($null -eq $engine) { Write-Error 'No hay ningún idioma de OCR instalado en Windows'; exit 2 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
# Salida en UTF-8, que es como la decodifica ocr.py.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
foreach ($line in $result.Lines) {
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        "{0:F0}|{1:F0}|{2}" -f ($r.X + $r.Width / 2), ($r.Y + $r.Height / 2), $w.Text
    }
}
