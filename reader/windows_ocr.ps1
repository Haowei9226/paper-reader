$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]

if (!$env:READER_OCR_LANGUAGE) {
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object {
        @{ tag = $_.LanguageTag; name = $_.DisplayName }
    })
    $default = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    @{ languages = $languages; default_language = $default.RecognizerLanguage.LanguageTag;
       max_dimension = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension } | ConvertTo-Json -Depth 4 -Compress
    return
}

$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Wait-WinRt($operation, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}

$language = [Windows.Globalization.Language]::new($env:READER_OCR_LANGUAGE)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if (!$engine) { throw 'The selected Windows OCR language is not available.' }

# Keep the page image in memory between Python and the local recognizer.
$inputBytes = [System.IO.MemoryStream]::new()
$stream = $null
$bitmap = $null
try {
    [Console]::OpenStandardInput().CopyTo($inputBytes)
    $inputBytes.Position = 0
    $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($inputBytes)
    $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Wait-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object {
        @{ text = $_.Text; words = @($_.Words | ForEach-Object {
            $rect = $_.BoundingRect
            @{ text = $_.Text; box = @($rect.X, $rect.Y, $rect.Width, $rect.Height) }
        }) }
    })
    @{ angle = $result.TextAngle; lines = $lines } | ConvertTo-Json -Depth 6 -Compress
} finally {
    if ($bitmap) { $bitmap.Dispose() }
    if ($stream) { $stream.Dispose() }
    $inputBytes.Dispose()
}
