# 问卷星自动填写脚本启动器 (Windows PowerShell)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 优先使用 Codex 运行时自带的 Node.js,找不到则使用系统 node
$runtimeNode = 'C:\Users\江建锋\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$node = if (Test-Path $runtimeNode) { $runtimeNode } else { 'node' }

# 让脚本能找到 Codex 运行时自带的 playwright 模块
$env:NODE_PATH = 'C:\Users\江建锋\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'

& $node (Join-Path $root 'fill_wjx.js') @args
exit $LASTEXITCODE
