# Paw 原生组件源码

各组件在 `components/` 下独立维护。`prompt-core` 保留原 Python 模块名，编译时将 `resources/` 中的默认工作区模板写入资源扩展模块。主项目构建只需要组件二进制和清单。

当前 Windows 发行目标使用 CPython 3.11.9 x64 和带 Windows SDK 的 MSVC x64。组件编译环境单独创建，编译输出可以指向主项目的 `native-libs/`。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\components\prompt-core\requirements-build.txt
.\.venv\Scripts\python.exe -B .\components\prompt-core\build.py --output-root ..\copaw\native-libs
```

交付时同步 `prompt-core/<构建目标>/` 整个目录。改变主项目调用接口时，同步更新组件 `API_VERSION` 和主项目 `native-libs/required.json` 中的接口版本。
