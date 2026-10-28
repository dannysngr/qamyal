# Qamyal: собирает комплект для Mac в папку -Out (по умолчанию — общая папка с Mac; на Mac скопировать её
# и запустить: bash Qamyal-mac/mac/install.sh или собрать образ: bash Qamyal-mac/mac/build_dmg.sh).
# Только код и модели — записи, голоса и настройки этого компьютера не копируются.
param([string]$Out = "D:\Обменник\Qamyal-mac")
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force "$Out\app", "$Out\mac", "$Out\models" | Out-Null

# программа: код, страницы, иконка, «средний голос»; без профиля Chrome, кэшей, настроек и Windows-разведчика Skype
robocopy "$Root\app" "$Out\app" /E /NFL /NDL /NJH /NJS /NP `
  /XD ".ui-profile" "__pycache__" "editor_cwd" "skype" "scout" "mac" `
  /XF "settings.json" "*.pyc" "preview.png" "qamyal.ico" | Out-Null
New-Item -ItemType Directory -Force "$Out\app\skype" | Out-Null
Copy-Item "$Root\app\skype\SkypeProbe.cs" "$Out\app\skype\"

# Mac: оболочка, помощник звука, установщик, иконка
Get-ChildItem "$Root\mac" -File | Where-Object { $_.Name -ne "make_kit.ps1" } | Copy-Item -Destination "$Out\mac"

# GigaAM из исходников (без сборочного мусора и примеров)
robocopy "$Root\downloads\gigaam\GigaAM-main" "$Out\gigaam" /E /NFL /NDL /NJH /NJS /NP `
  /XD "build" "gigaam.egg-info" ".github" "tests" "train_utils" "triton_scripts" "assets" | Out-Null

# модели, которые проще привезти, чем скачивать: GigaAM и разметка голосов (Whisper скачает install.sh)
robocopy "$Root\models\gigaam" "$Out\models\gigaam" /E /NFL /NDL /NJH /NJS /NP | Out-Null
New-Item -ItemType Directory -Force "$Out\models\diar\sherpa-onnx-pyannote-segmentation-3-0" | Out-Null
Copy-Item "$Root\models\diar\wespeaker_en_voxceleb_resnet34_LM.onnx" "$Out\models\diar\"
Copy-Item "$Root\models\diar\sherpa-onnx-pyannote-segmentation-3-0\model.onnx", "$Root\models\diar\sherpa-onnx-pyannote-segmentation-3-0\LICENSE" `
  "$Out\models\diar\sherpa-onnx-pyannote-segmentation-3-0\"

# скрипты для macOS — только с переводом строки LF, иначе bash на Mac споткнётся о \r
Get-ChildItem $Out -Recurse -Include *.sh, *.py, *.swift, *.txt, *.md, *.js, *.css, *.html | ForEach-Object {
  $t = [IO.File]::ReadAllText($_.FullName)
  if ($t.Contains("`r`n")) { [IO.File]::WriteAllText($_.FullName, $t.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false))) }
}

$mb = [math]::Round(((Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))
"Комплект готов: $Out ($mb МБ)"
