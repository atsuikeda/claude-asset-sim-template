# 1日1回の自動運用(Windows)。タスクスケジューラから呼ばれる。手で実行しても動く。
Set-Location $PSScriptRoot
# claude コマンドの場所がタスクスケジューラの PATH に無いときは、次の行を有効にして場所を書く
# $env:Path = "C:\Users\あなたの名前\AppData\Local\...\bin;$env:Path"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\$(Get-Date -Format 'yyyy-MM-dd').log"
$prompt = "CLAUDE.md の「毎回の手順」に従って、今日の運用を1回分実行してください。確認の質問はせず、最後まで自分で進めてください。"

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') 開始 ===" | Out-File $log -Append -Encoding utf8
claude -p $prompt --max-turns 60 2>&1 | Out-File $log -Append -Encoding utf8
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') 終了 (exit $LASTEXITCODE) ===" | Out-File $log -Append -Encoding utf8
