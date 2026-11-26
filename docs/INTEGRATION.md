# 任务接入

已有的训练或推理脚本可以直接登记。需要结构化进度、指标或协作停止时，再接入 Reporter。

## 普通命令

通过界面登记任务的 `cwd` 与 `argv`，例如：

```json
["C:\\Python\\python.exe", "-u", "train.py", "--config", "configs/run.json"]
```

工作台直接执行参数数组，不进行 shell 展开。解释器完整路径、脚本、环境和数据由项目自己管理；无需统一训练框架。相对参数由 `cwd` 解析。日志采集 stdout/stderr；Python 任务以 UTF-8 无缓冲输出，其他程序建议自行输出 UTF-8。

恢复命令可独立声明，例如 `[..., "--resume"]`。它不会自动注入检查点参数，也不验证 checkpoint 的算法语义；脚本必须读取自己的检查点并核实配置。未声明恢复命令时，不显示恢复按钮。

## 任务分组

任务定义可选 `group`：`preflight`（前检）、`formal`（正式实验）、`""`（未分组）。同阶段工作沿用同一 `project`，由 group 分开任务；`kind` 仍表示实验、训练、推理或演示，Reporter 的 `stage` 仍表示运行内部进度。

新建任务省略 group 时归入未分组；已有任务通过旧登记脚本更新且省略 group 时，保留当前分组。显式传空字符串可清除分组。可在界面编辑分组；只影响当前任务组织，已有尝试快照和恢复命令不变。工作台不根据名称推断任务用途。

## 可选进度上报

把单文件 [reporter.py](../reporter.py) 放进项目可导入路径即可使用，无第三方依赖。环境变量未设置时，上报不写入文件，任务仍可独立运行。

```python
import os
from pathlib import Path
from reporter import Reporter

report = Reporter(interval=1.0)
work = Path(os.environ['MLWB_WORK_DIR'])
for step in range(total):
    # 项目真实更新与保存逻辑在这里；不要为上报强制 GPU 同步。
    report.progress('训练', step + 1, total, {'loss': loss_scalar})
    if report.stop_requested():
        # save_checkpoint 必须实际保存成功；由项目提供此函数。
        save_checkpoint(work / 'checkpoint.pt')
        report.progress('训练', step + 1, total, force=True)
        report.checkpoint('checkpoint.pt')
        raise SystemExit(75)
report.artifact('preview.png', '输出预览')
```

`metrics` 可以是已经取得的有限数值或布尔标记。曲线把 `true` / `false` 显示为 1 / 0，原始事件仍保存布尔值；时间、步数和总量不能用布尔值。默认按 1 秒节流，阶段变化和阶段完成立即上报；`force=True` 用于最后状态。上报单条上限 32 KiB。记录错误会明确抛出，不能静默伪装成功。

`MLWB_WORK_DIR` 是逻辑运行的持久工作区，恢复时不变。任务可以使用现有自己的输出目录：在任务表单指定产物根，传给 `artifact()` 的是该根下的相对路径。引用可以跨尝试指向同一个检查点，但不是不可变快照；如果需要历史 checkpoint，请由项目写不同文件名。

`MLWB_RUN_DIR` 是本次尝试记录目录，恢复时改变。`MLWB_EVENTS_FILE` 是本次 JSONL 事件文件；`MLWB_STOP_FILE` 是该尝试的协作停止请求路径。每个尝试只有一个事件写入者；多进程训练应由主进程汇总，避免并发文件写入。工作台不拦截或加载模型。

## 事件格式

每行一个 UTF-8 JSON 对象，必需 `time`（Unix 秒）与 `type`。页面使用事件提供的阶段单位和总量，不猜测训练含义。

```json
{"type":"progress","time":1794090000,"stage":"训练","done":20,"total":100,"unit":"步","metrics":{"loss":0.2}}
{"type":"checkpoint","time":1794090001,"path":"checkpoint.pt"}
{"type":"artifact","time":1794090002,"path":"preview.png","label":"预览"}
```

未知总量用 `null`。进度没有线性时间保证，首版不推断整体 ETA。资源预算和科学验收继续由项目脚本实施。

## 执行语义

- 同一工作台数据目录最多一个活动任务，数据库唯一约束阻止双击或并发启动。
- 恢复使用原尝试保存的命令快照，任务定义后续编辑不改变该快照。
- 退出码 0 → 正常结束；请求停止后退出码 75 → 协作停止；其他非零 → 失败；强制停止 → 强制终止。
- 脚本忽略停止请求时界面保持“停止请求中”，不会声称安全退出。用户可选择强制终止。
- 正常结束并不等于项目验收或研究成功。工作台的身份和运行记录不替代研究登记体系。
- 浏览器和 HTTP 服务独立于 worker。Windows worker 拥有任务及子进程树；worker 异常退出时 OS 清理其子进程，服务恢复后标记中断。
- 只监听回环地址；启动/停止需要来源与令牌校验。这个服务是受信任本机用户工具，不是多租户远程平台。
- 任务参数和日志可能包含敏感内容，应由项目避免写入秘密。工作台不保存完整环境变量快照。

## 外部运行目录观测（4 文件同步协议）

需要被动观测在本机同步文件夹（例如网络磁盘或桌面同步目录）中产生日志和指标的远端或独立运行时，可在启动工作台时传入一个或多个观测目录：

```powershell
.\start.ps1 -WatchDir "D:\path\to\sync_runs"
# 或：workbench.py --watch-dir ... （也可设置环境变量 MLWB_WATCH_DIR，多路径用系统分隔符连接）
```

每个受观测运行在 `<watch_dir>/<attempt_id>/` 下维护标准目录结构（`attempt_id` 仅使用字母、数字与 `._-`）：

1. `meta.json`：必填 `project`、`name`、`status`（如 `starting` / `running` / `stopping` / `completed` / `failed` / `stopped`）；推荐含 `created`、`started`、`heartbeat`（Unix 秒）、`cooperative_stop`、`kind`、`group`、`source_label` 及可选自由字典 `runtime`。活跃态超出 `--heartbeat-timeout`（默认 180 秒）未更新心跳时界面显示“心跳超时”。
2. `events.jsonl` 与 `stdout.log`：与本地任务格式完全相同的追加事件流与文本日志，继续享受 `monitor.sqlite` 增量读取。
3. `stop.request`：界面点击“请求安全停止”后写入的停机请求标志文件（不提供强制终止、远端启动或恢复；外部运行也不占用本机单任务槽）。
4. `artifacts/`：随任务登记的轻量产物或预览图根目录；工作台只允许下载在 `events.jsonl` 中显式上报过的相对路径，禁止路径穿越。

若外部 runner 在 `<watch_dir>` 上级目录的 `runs/` 下伴随写入标准 `.manifest.json`（含 `tar_name`、`tar_sha256`、`tar_bytes`、`source_out_rel` 与逐文件 `path`/`sha256` 列表）及 `.tar` 归档包（可在 `meta.json` `runtime.archive_manifest` 声明相对路径），工作台会在界面显示 **“拉取到本地”** 按钮：首次按项目名称绑定并持久保存本地根目录，拉取时严格校验归档包与逐文件 SHA256，一致文件自动跳过，遇本地修改冲突先提示确认后再覆盖。

标准库 `reporter.py` 额外提供了 `ExternalRun` 上下文辅助类，负责原子写 `meta.json`、节流心跳（默认 30 秒一次以减少同步盘小文件写放大）、暴露 `run.reporter` 与 `run.env()`，并在捕获 `SystemExit(75)` 且检测到 `stop.request` 时自动收口为 `stopped`：

```python
from reporter import ExternalRun

with ExternalRun(watch_dir, project='Demo', name='Remote Train', source_label='Remote GPU') as run:
    for step in range(total):
        run.reporter.progress('训练', step + 1, total, {'loss': loss_scalar})
        if run.reporter.stop_requested():
            save_checkpoint()
            raise SystemExit(75)
```
