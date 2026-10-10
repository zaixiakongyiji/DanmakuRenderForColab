# DanmakuRenderForColab

基于 DanmakuRender v5 的 Colab 直播录制与弹幕渲染工具。将耗时的录制、渲染、合并和备份放到云端运行，也可由本地检测开播后触发 Notebook。保留原有桌面入口。

`本地检测开播 → Colab 录制与弹幕采集 → 分段渲染与 Drive 备份 → 可选 B 站多 P 投稿 → 下播收尾与全场合并归档 → 自动模式请求释放运行时`

当前 Colab 流程限定一个 B 站直播间、一个投稿账号和一个活动运行时。自动触发模式在结果持久化后请求释放运行时；手动测试模式保留运行时。Drive 媒体不自动删除。

## 功能与验证状态

- 默认每 3600 秒录制一段，保留原片、ASS 和带弹幕成品，使用 FFmpeg 与 H.264 NVENC；预检失败会停止。
- 每段文件复制到 Drive 后读回校验大小和 SHA-256，清单记录录制、渲染、备份和投稿状态。
- 可选自动投稿：第一段创建稿件，后续段按序追加到同一 BVID；不足 120 秒的段仍备份，但不投稿。全场合并视频仅用于归档。
- 支持本地自动触发、运行回执、受控停止和按运行编号补传；结果不明的投稿会停止追加，供核对。

2026-10-10：88 项本地离线测试通过。此前用户已试跑云端录制、渲染、备份和合并；本次新增自动触发及多 P 投稿仍需真实云端和测试账号验收。

## Colab 快速开始

1. 从本仓库 `codex/colab-poc` 分支打开 [notebooks/colab_record.ipynb](notebooks/colab_record.ipynb)，在 Colab 保存一份私有副本并将 Notebook 设置为支持 NVENC 的 NVIDIA GPU 类型；首次登录和授权可先使用 CPU，监控期间无需保持连接。
2. 将现有观看登录 JSON 手动放入私有 Drive 的 `MyDrive/DMRColab/credentials/bilibili.json`。要求 `cookie_info.cookies` 列表包含非空 `SESSDATA`；程序不调用登录工具。
3. 按顺序执行源码准备、依赖安装、Drive 授权、运行参数、预检、正式运行和结果检查单元格。手动运行时填写 `ROOM_URL`。
4. 先关闭投稿做短试跑：设置 `SEGMENT_SECONDS = 300`、`MAX_RECORD_HOURS = 0.2`；只录一小时设置 `MAX_RECORD_HOURS = 1`。录制结束后的渲染、备份及投稿收尾可能使总运行时间更长。
5. 核对退出码、清单以及 Drive 文件，播放检查音画和弹幕同步，手动模式检查后自行断开；自动模式先汇总和持久化结果，再请求释放运行时。

录像及清单位于 `MyDrive/DMRColab/runs/<run_id>/`：`source/` 保存原片，`danmaku/` 保存 ASS，`rendered/` 保存弹幕版分段，`merged/complete.mp4` 保存全场归档。`manifest.json` 分别汇总录制备份、合并及投稿结果。

运行中第一次中断请求受控停止，等待已完成分段收尾；再次中断请求强制停止，不能视为正常成功。

仓库更新不会自动同步到已另存的 Drive Notebook。更新时重新打开仓库版本并保存私有副本；已有源码检出需在活动任务结束后手动拉取或使用新运行时。

## 启用 B 站多 P 自动投稿

公开示例默认关闭投稿。将 [colab_support/upload.example.yml](colab_support/upload.example.yml) 复制到私有 Drive 的 `MyDrive/DMRColab/config/upload.yml`，填写账号备注 `account`、账号校验 `expected_uid`、分区 `tid`、转载属性 `copyright`、来源及标题、简介和标签，再设置 `enabled: true`。转载须填写来源。

投稿 Cookie 单独放入 `MyDrive/DMRColab/credentials/bilibili_upload.json`，要求 `cookie_info.cookies` 包含非空 `SESSDATA` 和 `bili_jct`。观看与投稿凭据可以属于不同账号；启用投稿后，程序在录制前校验身份，配置完成后每场自动投稿。

原片、ASS 和成品均备份成功后，分段才进入串行投稿队列。`submitted` 表示核对了稿件编号与分 P，不代表审核通过或已经公开；`unknown` 必须先核对远端，不能盲目重传。凭据和真实配置只保存在私有 Drive，不上传到公开仓库。

## 本地检测开播与自动触发

Windows 用户也可以双击 `start_colab_monitor.cmd`。首次运行加 `--setup`，按提示选择任务配置、填写 Google Drive for desktop 的本地同步目录（例如 `G:\你的云端硬盘`）、私有 Colab Notebook 地址和专用浏览器 CDP 地址；设置会保存在被 Git 忽略的 `.temp` 中。选择 `DMR-example.yml` 后，入口会自动读取其中的直播间地址。

```powershell
.\start_colab_monitor.cmd --setup
```

设置完成后，直接双击 `start_colab_monitor.cmd` 即可开始本地监控。它会在连续两次确认开播后向 Drive 写入运行请求，打开指定 Notebook 并点击一次“全部运行”。首次登录、Drive 授权和保存参数可以使用 CPU 运行时。在 Notebook 顶部选择“读取本地触发请求”，将正式副本保存为 GPU 类型，然后断开准备运行时。监控期间无需连接 GPU；开播后点击 Run all 才申请运行时，实际 GPU 型号由预检记录。若新运行时要求再次授权，仍须人工完成，不承诺无人值守。之后无需再次填写路径。

`--drive-sync-root` 指的是本机 Drive 同步目录，因为本地触发器需要把请求和回执写成文件，让 Google Drive 同步到 Colab。这个目录只传递控制文件，不保存云端录制过程中的视频。也可以继续使用命令行入口：

本地安装项目依赖，另安装浏览器适配依赖：

```powershell
python -m pip install -r colab_support/monitor_requirements.txt
```

需要 Google Drive for desktop 同步目录，以及已登录、保存为 GPU 类型并配置好参数的私有 Notebook；监控期间无需连接运行时。先使用仅打开页面的模式：

```powershell
python colab_trigger.py --url "https://live.bilibili.com/<房间号>" --drive-sync-root "<Drive 同步根目录>" --notebook-url "<自己的 Notebook URL>"
```

准备好启用 loopback CDP 的专用浏览器后，可自动点击一次“全部运行”：

```powershell
python colab_trigger.py --url "https://live.bilibili.com/<房间号>" --drive-sync-root "<Drive 同步根目录>" --notebook-url "<自己的 Notebook URL>" --auto-run --cdp-url http://127.0.0.1:9222
```

自动触发时 Notebook 的 `ROOM_URL` 留空，读取同步的运行请求。遇到登录、授权、页面忙碌或无法确认的状态会报告 `needs_attention`；不会自动授权、重启运行时或保活。详细浏览器条件与回执协议见 [Colab 文档](docs/colab.md)。

自动模式的 `manifest.json` 与 `ack.json` 包含 `runtime_release`：`requested` 表示已保存释放意图并请求 `runtime.unassign()`，不表示已确认释放；`unknown` 表示保存或释放异常。最终回执后等待至少 60 秒，浏览器必须明确显示未连接才允许按下播确认重新武装；仍连接或无法判断时报告 `needs_attention`，不重复点击。

失败任务会先补存未备份媒体到 `recovery/`，保存失败或仍有活动录制进程时保留运行时供检查。源码准备、依赖安装、Drive 授权等认领前失败由认领超时报告；不能保证这些阶段自动释放。`/content` 文件会随运行时释放丢失，请从 Drive 检查最终结果。

## 补传与离线测试

在挂载 Drive 的云端环境中，可补传已关闭任务的校验备份；先确认没有活动运行或其他补传实例：

```bash
python colab_upload.py --run-id <run_id> --drive-root /content/drive/MyDrive
```

补传沿用原稿件并跳过已确认提交内容；未知结果、事务冲突或校验失败会停止。详细配置、超时、错误处理及验收步骤见 [docs/colab.md](docs/colab.md)。

本地离线回归：

```bash
python -m unittest discover -s checks -p "test_colab*.py" -v
```

## 本 fork 的来源与 Colab 改造

本仓库 DanmakuRenderForColab 基于 [SmallPeaches/DanmakuRender](https://github.com/SmallPeaches/DanmakuRender) 的 v5 分支继续开发，保留原项目及其贡献者的来源说明。录制、直播取流、弹幕采集、ASS 写入和渲染直接复用或适配原有 DMR 模块；新增 Colab Notebook、单场协调器、Drive 校验备份、收尾合并及本地触发与回执代码。

Colab 路径支持单直播间录制、弹幕渲染、分段及全场成品备份，新增本地自动触发与可选 B 站多 P 投稿。一场直播对应一个稿件，每个有效渲染分段追加为一个 P；全场合并仅用于 Drive 归档。公开配置默认关闭投稿，启用需在私有 Drive 填写独立投稿凭据和账号配置。新增流程完成离线测试，真实自动触发和投稿尚待云端验收，详见 [Colab 文档](docs/colab.md)。

截至 2026-10-10，检查的上游 v5 根目录及本地版本未发现 LICENSE、COPYING 或 NOTICE 许可证文件；上游 README 保留“本程序仅供研究学习使用！”说明。本 fork 不据此宣称原代码采用 MIT、Apache、GPL 等许可证，也不替原作者重新授权；本段仅记录来源与当前查证结果。原代码的版权归相应权利人所有，第三方依赖的许可证需分别核对。

以下保留原项目介绍。

结合网络上的代码写的一个能录制带弹幕直播流的小工具，主要用来录制包含弹幕的视频流。     
- 可以录制纯净直播流和弹幕，并且支持在本地预览带弹幕直播流。
- 可以自动渲染弹幕到视频中，并且渲染速度快。
- 支持同时录制多个直播。    
- 支持录播自动上传至B站。     

此版本为全新设计的版本5，包含以下新功能：     
- 支持动态载入配置文件。
- 支持更加复杂的录制、上传、渲染和清理逻辑。
- 支持搬运直播回放或者视频。
- 支持使用webhook与其他录制软件协同。

旧版本可以在分支v1-v4找到。     


## 使用说明
**如果你是纯萌新建议看我B站的专栏安装：https://www.bilibili.com/read/cv26348023**         

### 安装与使用文档      
[**安装文档**](docs/installation.md)       
[**使用文档**](docs/usage.md)     

[**服务器录播示例**](https://github.com/SmallPeaches/DanmakuRender/discussions/368)

### 可选参数
程序运行时可以指定以下参数
- `--config` 指定全局配置文件，默认`configs/global.yml`
- `--version` 查看版本号
- `--skip_update` 跳过版本检查

## 更多
感谢 THMonster/danmaku, wbt5/real-url, ForgQi/biliup, ForgQi/stream-gears 的工作。     
出现问题欢迎大家提issue讨论。       

**本程序仅供研究学习使用！**
