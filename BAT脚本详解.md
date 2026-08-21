# start.bat 逐行详解 —— Windows 批处理入门

> 以 `G:\MiniProject2026\hangulTranslate\start.bat` 为例，逐行讲清楚每句话是什么意思，
> 并教你以后自己写出类似的启动脚本（.bat / .cmd 都是批处理，写法一样）。

---

## 1. 脚本全貌

```bat
@echo off
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "MAIN=%~dp0main.py"

if not exist "%PYTHON%" (
    echo [ERROR] 未找到虚拟环境: %PYTHON%
    echo 请先运行: python -m venv .venv
    pause
    exit /b 1
)

if not exist "%MAIN%" (
    echo [ERROR] 未找到主程序: %MAIN%
    pause
    exit /b 1
)

echo 正在通过虚拟环境启动 main.py ...
"%PYTHON%" "%MAIN%" %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo 程序已结束，退出码: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
```

它做的事情一句话概括：**把命令行的工作目录切到脚本所在文件夹，然后用项目自带的 `.venv` 里的 Python 去运行 `main.py`，运行完显示退出码并等待按键。**

---

## 2. 逐行讲解

### 第 1 行：`@echo off`

- `echo` 在批处理里有两个意思：一是"输出文字"（后面接内容），二是"回显"（把每条命令本身打印出来）。
- 批处理默认是 **echo on**：每执行一行，屏幕会先打印这一行的命令原文，再打印结果，看起来非常乱。
- `echo off` 关闭回显；`@` 表示"这一行本身也不要显示"。
- 所以 `@echo off` 是几乎所有 bat 的第一行，作用：**让后续命令安静地执行，只显示我们想显示的内容**。

### 第 2 行：`cd /d "%~dp0"`

这是全脚本最关键的一行，拆开看：

| 部分 | 含义 |
| --- | --- |
| `cd` | change directory，切换当前目录 |
| `/d` | 连盘符一起切换（比如当前在 C 盘，要切到 G 盘必须加 `/d`） |
| `%~dp0` | 当前这个 bat 文件所在的"盘符 + 文件夹路径"，结尾带 `\` |
| `"..."` | 双引号，防止路径里有空格时被拆开 |

- `%0` 代表"这个 bat 自己"。`%0` 的完整值是脚本路径，比如 `G:\MiniProject2026\hangulTranslate\start.bat`。
- `~d` 取盘符 → `G:`；`~p` 取路径 → `\MiniProject2026\hangulTranslate\`；合起来 `%~dp0` = `G:\MiniProject2026\hangulTranslate\`。
- 这一行的作用：**不管你从哪个文件夹双击启动，都先把"当前目录"切到脚本所在目录**。这样后面用相对路径（比如 `.venv\Scripts\python.exe`）才找得到东西。

### 第 3~4 行：用 `set` 定义变量

```bat
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "MAIN=%~dp0main.py"
```

- `set 变量名=值` 用来定义一个变量（环境变量）。之后用 `%变量名%` 引用。
- `%~dp0` 拼上 `.venv\Scripts\python.exe`，得到一个**绝对路径**，不依赖"当前目录是哪儿"。
- 引号写在 `=` 外面、包住整个值：`set "VAR=值"`。这是刻意为之——如果不加引号，`值` 后面万一带了空格（比如编辑时不小心按到），空格会被算进变量值里，导致路径错误。这是 bat 的经典坑。

### 第 6~11 行：`if not exist` 检查 + 报错退出

```bat
if not exist "%PYTHON%" (
    echo [ERROR] 未找到虚拟环境: %PYTHON%
    echo 请先运行: python -m venv .venv
    pause
    exit /b 1
)
```

- `if not exist "路径"` ：判断这个文件/文件夹**不存在**时，执行后面括号里的内容。
- `( ... )` 是一个**语句块**：把多条命令打包成一组，条件成立时一起执行。缩进只是为了好看。
- 变量要用 `%PYTHON%` 包在引号里：路径含空格时不会被拆坏。
- 括号里的内容：
  - `echo [ERROR] ...`：打印错误提示，`%PYTHON%` 会被替换成实际的路径。
  - `pause`：暂停，显示"请按任意键继续"。**双击运行时窗口不会闪退**，用户才能看到错误。
  - `exit /b 1`：退出批处理，退出码 1。**`/b` 表示只退出这个 bat，不关掉整个 cmd 窗口**。

这组检查的意义：环境缺失时给出**人能看懂的提示**，而不是让 Python 抛出一堆看不懂的报错。

### 第 16 行：运行主程序

```bat
"%PYTHON%" "%MAIN%" %*
```

- `"%PYTHON%"`：被替换成 `.venv\Scripts\python.exe` 的完整路径，用引号包住，相当于命令行里执行：`"G:\...\.venv\Scripts\python.exe" "G:\...\main.py"`。
- `%*`：**把调用这个 bat 时传入的所有参数，原样转交给 Python**。
  - 例：双击运行 → 没参数；
  - 在终端里 `start.bat --help` → Python 收到 `--help`；
  - `start.bat --input 1.txt --workers 4` → 全部透传。
- 这一行执行完，`%ERRORLEVEL%` 会被自动设置成 Python 程序的退出码（0 = 成功，非 0 = 失败/出错）。

### 第 17~21 行：保存退出码 + 收尾

```bat
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo 程序已结束，退出码: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
```

- `%ERRORLEVEL%` 是 cmd 内置变量，永远保存**上一条命令的退出码**。
- 第一时间把它存进 `EXIT_CODE`：因为紧接着的 `echo` 命令本身也会改变 `%ERRORLEVEL%`，不先保存就丢掉了。
- `echo.`：输出一个空行（`echo` 后面跟 `.` 就是打空行的惯用写法）。
- `pause`：等用户按任意键，防止窗口闪退。
- `exit /b %EXIT_CODE%`：把程序真正的退出码传出去，方便别人（或别的脚本）判断这次运行是否成功。

---

## 3. 核心语法速查表

| 语法 | 含义 | 例子 |
| --- | --- | --- |
| `@echo off` | 关闭命令回显（第一行固定用它） | `@echo off` |
| `echo 文字` | 打印一行文字 | `echo 开始处理...` |
| `echo.` | 打印一个空行 | `echo.` |
| `cd /d "路径"` | 切换目录（含盘符） | `cd /d "%~dp0"` |
| `set "变量=值"` | 定义变量（引号防尾部空格） | `set "PY=%~dp0.venv\Scripts\python.exe"` |
| `%变量%` | 引用变量 | `"%PY%" --version` |
| `%0` | bat 自身的路径 | — |
| `%~dp0` | bat 所在盘符+目录（末尾带 `\`） | `%~dp0main.py` |
| `%*` | bat 收到的所有参数 | `"%PY%" "%MAIN%" %*` |
| `%1` `%2` | 第 1、第 2 个参数 | 拖文件到 bat 上，`%1` = 文件路径 |
| `if exist "路径" ( ... )` | 存在则执行块 | `if exist "%F%" ( echo 找到了 )` |
| `if not exist "路径" ( ... )` | 不存在则执行块 | 本脚本的用法 |
| `pause` | 暂停等按键，防闪退 | `pause` |
| `exit /b 退出码` | 退出批处理并返回退出码 | `exit /b 1` |
| `%ERRORLEVEL%` | 上一条命令的退出码 | `set "C=%ERRORLEVEL%"` |
| `>nul` | 把输出丢弃到空设备 | `chcp 65001 >nul` |
| `2>&1` | 把错误输出也合并到正常输出 | `"%PY%" main.py 2>&1` |

---

## 4. 三个最容易踩的坑

### 坑 1：中文乱码（编码问题）

- cmd 默认按系统代码页解析 bat。中文 Windows 的系统代码页是 **936（GBK/ANSI）**。
- 所以**含中文的 bat 要另存为"ANSI / GBK"编码**，双击后中文才正常。
- 如果你用 VS Code：右下角点编码 → "通过编码保存" → 选 `Simplified Chinese (GB2312)` 或 GBK。
- 如果想用 UTF-8 保存，就需要在文件开头加 `chcp 65001 >nul` 切换代码页（且换行必须是 CRLF，见坑 2）。**最简单省事：直接存 ANSI/GBK。**

### 坑 2：换行符必须是 CRLF

- Windows 批处理要求 **CRLF（回车+换行）** 行尾。
- 如果文件是 LF（Linux 风格换行），cmd 解析会错乱，出现莫名其妙"某某不是内部或外部命令"的报错。
- VS Code 右下角把 "LF" 改成 "CRLF" 即可；记事本保存默认就是 CRLF。

### 坑 3：路径里有空格必须加引号

- `C:\Program Files\...` 这类带空格的路径，不包引号会被拆成两截。
- 规则：**凡是路径，一律用 `"..."` 包起来**，包括 `if exist "%变量%"` 里的判断。

---

## 5. 自己写启动脚本的模板

以后给别的 Python 项目写启动 bat，直接套这个模板：

```bat
@echo off
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] 找不到虚拟环境: %PY%
    echo 请先执行: python -m venv .venv
    pause
    exit /b 1
)

echo 启动中 ...
"%PY%" main.py %*

set "C=%ERRORLEVEL%"
echo.
echo 结束，退出码: %C%
pause
exit /b %C%
```

- 想加"检查 main.py 是否存在"，照抄 `if not exist ... ( ... )` 那段即可。
- 想先装依赖再跑：`"%PY%" -m pip install -r requirements.txt` 放在运行 main.py 之前。
- 想传固定参数：`"%PY%" main.py --input 小说.txt --workers 4`。
- 想让用户输参数：`set /p INPUT=请输入文件名: ` 会把用户输入存进 `%INPUT%`。

---

## 6. 进阶小技巧

- **拖文件到 bat 图标上**：被拖的文件路径会成为 `%1`（第二个是 `%2`），可以写成"把这个文件拖进来就处理它"。
- **`start "" "路径"`**：在新窗口里打开程序，比如 `start "" "%PY%" main.py`。
- **`call 别的.bat`**：在一个 bat 里调用另一个 bat，并等它执行完。
- **`for %%f in (*.txt) do ...`**：循环处理目录里所有 txt。注意批处理里循环变量写 `%%f`（两个百分号）。
- **`if errorlevel 1`**：等价于"退出码 >= 1"，常用来判断失败：`if errorlevel 1 ( echo 失败 )`。

---

## 7. 写完之后自查清单

1. 第一行是 `@echo off`？
2. 用了 `cd /d "%~dp0"` 切到脚本目录？
3. 路径都加引号了？
4. 中文的话，文件保存成 ANSI/GBK 了？
5. 换行是 CRLF？
6. 最后有 `pause` 防止闪退？
7. 需要用退出码的地方，先 `set "X=%ERRORLEVEL%"` 保存了？

按照这个清单检查完，你的 bat 基本就不会出幺蛾子了。