# 通信日志功能更新说明

## 实现的功能

### 1. 通信日志界面限制显示 20 条消息
- **常数定义**: `MAX_LOG_MESSAGES = 20`
- **实现方式**: 新增 `_refresh_log_display()` 方法
- **工作原理**: 
  - 所有接收到的日志消息存储在 `_log_messages` 列表中（无限制）
  - 界面显示时只显示最后 20 条消息
  - 当有新消息到达时，自动更新显示

### 2. 通信日志保存到本地
- **日志保存位置**: `~/.relay_controller/logs/`
- **日志文件名格式**: `relay_log_YYYY-MM-DD.txt`
- **实现方式**: 新增 `_save_log_to_file()` 和 `_init_log_file()` 方法
- **工作原理**:
  - 程序启动时自动创建日志目录（如不存在）
  - 每条日志消息实时追加到当前日期的日志文件中
  - 每天自动创建新的日志文件

## 代码修改详情

### 新增导入
```python
from pathlib import Path
from datetime import datetime
```

### 修改的类: `MainWindow`

#### 新增属性
```python
MAX_LOG_MESSAGES = 20  # 类常数：最多显示20条消息
self._log_messages: list[str] = []  # 存储所有日志消息
self._log_file_path = self._init_log_file()  # 日志文件路径
```

#### 新增方法

1. **_init_log_file()** - 初始化日志文件
   - 创建 `~/.relay_controller/logs/` 目录
   - 返回当前日期的日志文件路径

2. **_save_log_to_file(message)** - 保存日志到文件
   - 以追加模式写入日志文件
   - 包含异常处理

3. **_refresh_log_display()** - 刷新界面显示
   - 清空显示窗口
   - 显示最后 20 条消息

4. **_clear_log()** - 清空日志
   - 清空内存中的日志消息列表
   - 清空界面显示

#### 修改的方法

1. **_append_log(message)** - 日志添加方法
   - 将消息添加到 `_log_messages` 列表
   - 调用 `_refresh_log_display()` 更新界面（只显示 20 条）
   - 调用 `_save_log_to_file()` 保存到本地文件

2. **清空日志按钮连接**
   - 改为连接 `_clear_log()` 方法而非直接调用 `clear()`

## 使用说明

### 查看日志文件
日志文件保存在：
```
~/.relay_controller/logs/relay_log_YYYY-MM-DD.txt
```

在 macOS 终端中查看日志：
```bash
cat ~/.relay_controller/logs/relay_log_2024-05-29.txt
```

### 功能特性
- ✅ 界面最多显示最近 20 条消息
- ✅ 所有日志消息实时保存到本地文件
- ✅ 每天自动创建新的日志文件
- ✅ 完整保留所有日志历史（本地文件）
- ✅ 清空日志按钮清空界面和内存显示

## 技术细节

### 日志流程
```
SerialThread.log.emit() 
  ↓
MainWindow._append_log()
  ├─ 添加时间戳格式化
  ├─ 存储到 _log_messages 列表
  ├─ 刷新显示（只显示最后20条）
  └─ 保存到本地文件
```

### 日志文件特性
- 文件编码: UTF-8
- 每行一条日志消息
- 格式: `[HH:MM:SS] 消息内容`
- 文件大小: 取决于运行时间（无大小限制）
