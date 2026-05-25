#!/bin/bash
echo ""
echo "💰 我的记账本 — 启动中..."
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
  echo "❌ 未找到 Python3，请先安装 Python：https://www.python.org/downloads/"
  exit 1
fi

# 安装依赖
echo "📦 安装依赖（首次运行需要一点时间）..."
pip3 install flask flask-cors plaid-python -q

echo ""
echo "✅ 依赖安装完毕"
echo ""
echo "🌐 请在浏览器打开：http://localhost:5000"
echo "   （按 Ctrl+C 关闭服务器）"
echo ""

python3 app.py
