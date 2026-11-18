# ML Workbench

本机单用户的机器学习任务工作台。通过浏览器手动启动实验、训练或推理，查看分阶段进度、指标、日志和产物。工作台仅依赖 Python 标准库；任务继续使用各自的解释器和环境。

## 首次启动

在 Windows PowerShell 中进入仓库目录，使用 Python 3.11 或更高版本：

```powershell
python -m venv --without-pip .venv
.\start.ps1
```

打开终端打印的本机地址。默认端口由系统分配；需要固定端口时使用 `.\start.ps1 -Port 8765`。首次启动会创建 `data/`，无需准备数据库或导入旧任务。点击“添加 CPU 演示”，再点击“启动新运行”，即可用合成数据走完一次流程，无需 GPU。

## 管理任务

- 登记项目、任务名称、工作目录和启动命令。命令使用 JSON 参数数组，首项为可执行文件的完整路径。任务可分为前检、正式实验或未分组；面板记住分组展开状态和最后查看的位置。
- 普通命令可直接查看状态与标准输出。需要阶段、指标曲线、检查点或产物链接时，按[任务接入说明](docs/INTEGRATION.md)接入可选的 [Reporter](reporter.py)。产物保留在原目录。
- 每个数据目录同时执行一个任务。工作台按任务和每次执行保存监测进度，切换任务或重开面板时继续增量读取。关闭浏览器或管理服务不会停止已启动的 worker。
- 安全停止、恢复需要任务脚本显式支持。恢复使用原执行的配置快照；强制终止可能丢失尚未保存的进度。Windows 已验证进程树清理，其他系统未验证。

## 本地数据与测试

`data/` 保存本机任务配置、执行记录、日志、监测缓存和界面偏好。它不随源码分发；重新克隆仓库不会带入这些记录。`.qa/` 是测试工作区。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

前端状态测试另需 Node.js，运行面板不需要 Node.js：

```powershell
node tests/test_groups_ui.cjs
node tests/test_monitor_ui.cjs
```
