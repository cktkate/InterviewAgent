# Windows EXE 打包说明

桌面版只是现有 Web 应用的原生窗口外壳。原来的启动命令、API、WebSocket、网页入口和业务代码均保持不变。

## 生成 EXE

在项目根目录打开 PowerShell：

```powershell
pip install -r requirements.txt
.\scripts\build_windows.ps1
```

构建结果：

```text
dist\InterviewAgent\InterviewAgent.exe
```

这是文件夹形式的便携版。发布时需要复制整个 `dist\InterviewAgent` 文件夹，不能只复制其中的 EXE。

## 首次运行

1. 将项目当前使用的 `.env` 复制到 `dist\InterviewAgent\.env`。不要把包含密钥的 `.env` 提交到 Git 或公开发送。
2. 确保原项目依赖的 MySQL、Redis、Weaviate 等服务已经启动。
3. 双击 `InterviewAgent.exe`。

应用默认使用 `127.0.0.1:8000`，如果该端口被占用，会自动尝试 8001–8019。设置了 `APP_PORT` 时会严格使用指定端口。

## 不打包直接调试桌面窗口

```powershell
pip install -r requirements-desktop.txt
python desktop_launcher.py
```

Windows 10/11 通常已经安装 WebView2 Runtime。如果窗口无法打开，请安装 Microsoft Edge WebView2 Runtime 后重试。
