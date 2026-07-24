# Windows 品牌图标与后台进程修复设计

## 目标

修复 Windows 安装包中的两个问题：

1. Web 标签页仍显示旧的黑底白字 favicon；
2. 应用运行期间持续显示 `runtime\python\python.exe` 控制台窗口。

修复必须保留管理后台上传自定义 Logo/Favicon 的能力。

## 根因

### Favicon

Windows 的 `packaging/build_windows.py::write_brand_json` 只写入品牌名称、页面标题和版本，没有把 `packaging/brand.toml` 指向的唯一蓝圈图写入 `logo`、`favicon` 种子。

此外，`seed_branding.seed_if_needed` 会保留数据库中所有非空的用户字段。旧版本已经写入的黑白内置图因此会永久覆盖新安装包中的默认图。

### Python 控制台

Windows 托盘进程通过 `runtime\python\python.exe` 启动 uvicorn。创建参数只有 `CREATE_NEW_PROCESS_GROUP`，没有 `CREATE_NO_WINDOW`。父进程由 `pythonw.exe` 运行且没有控制台，Windows 会为这个 console 程序创建一个可见窗口。

## 设计

### Windows 品牌种子

扩展 Windows `BuildConfig` 和 `brand.json` 生成逻辑，读取 `brand.toml` 中的 `logo` 与 `favicon`，以 PNG Data URL 写入 `branding_seed`。Windows 和 macOS 使用相同的唯一源文件：

`kimi-cli/web/public/logo.png`

不增加第二份图标文件。

### 旧内置图迁移

在 branding seed 中携带旧内置黑白图的 SHA-256：

`dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6`

启动时对现有 `logo` 和 `favicon` Data URL 解码并计算 SHA-256：

- 字段为空：写入当前蓝圈图；
- 字段等于旧内置图哈希：迁移为当前蓝圈图；
- 字段为其他值：视为用户自定义，保持不变；
- Data URL 无法解析：保持不变并记录警告。

该迁移是幂等的，首次升级后不会反复写数据库。

### 隐藏后台进程

Windows uvicorn 子进程创建标志改为：

```python
subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
```

标准输出和错误仍写入现有 `server.log`。进程仍保持独立进程组；如果无控制台环境下 `CTRL_BREAK_EVENT` 不可用，现有异常处理会回退到 `TerminateProcess`，不会遗留后台进程。

设置窗口仅在用户主动打开时运行，不是本次常驻窗口的来源；本次不扩大修改范围。

## 验证

新增测试覆盖：

1. Windows `brand.json` 同时包含当前 Logo 与 Favicon Data URL；
2. 空字段会写入当前图；
3. 旧内置黑白图会迁移；
4. 用户自定义 Data URL 不被覆盖；
5. Windows uvicorn 创建标志同时包含 `CREATE_NEW_PROCESS_GROUP` 和 `CREATE_NO_WINDOW`；
6. macOS/POSIX 启动参数保持不变；
7. 唯一图标源测试继续通过。

远端 Windows 安装包验收：

1. 安装或升级后，Web 标签页显示蓝圈图；
2. 应用运行期间不出现 `runtime\python\python.exe` 控制台窗口；
3. 托盘启动、停止和重启后端正常；
4. 管理后台上传的自定义 Logo/Favicon 在升级后仍保留。
