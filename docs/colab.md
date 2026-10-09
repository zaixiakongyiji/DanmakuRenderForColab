# Colab 单场录制试跑

本功能将一场 B 站直播的视频、ASS 弹幕、渲染和备份放到 Colab。首版只支持一个标准 B 站直播间链接，不投稿、不自动清理媒体、不自动释放运行时。原来的 `main.py` 和本地配置继续按原方式使用。

## 1. 发布与准备

本地分支为 `codex/colab-poc`，远程 `colab` 指向 `zaixiakongyiji/DanmakuRenderForColab`，`origin` 保留原仓库。使用前确认该远程已有此分支和 Colab 入口。

提交时仅纳入 Colab 新增文件及相关 DMR 适配修改。保留工作区原有修改，不把 Cookie、个人配置、日志、录像或会话文档加入公开仓库。建议提交说明：`feat: 添加 Colab 单场录制渲染与备份流程`。

发布后，在 Colab 中打开 fork 的 `notebooks/colab_record.ipynb`，选择 GPU 运行时并按顺序执行。Notebook 默认读取该 fork 的 `codex/colab-poc` 分支，已有检出不会自动覆盖；每次结果记录实际 commit SHA。

请在自己的本地配置中确认现有观看 Cookie 文件的位置，然后手动上传一份到 **私有 Drive**：

```text
MyDrive/DMRColab/credentials/bilibili.json
```

文件必须包含 `cookie_info.cookies` 列表及非空 `SESSDATA`。Notebook 只复制这份 JSON 的 Cookie 列表到运行时私有目录，不复制 refresh token。不自动登录或刷新 Cookie；缺失、格式错误时在录制前停止。Cookie 能否在云端继续使用需实测；失效时从本地更新私有 Drive 文件。

Drive 授权由本人完成。不要把 Cookie 贴进代码、Notebook 文本、截图或仓库。程序读取 Cookie 不代表投稿功能被开启。

## 2. 参数和预检

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--url` | 必填，无默认值 | 单个 B 站直播间；在 Notebook 的 `ROOM_URL` 中填写 |
| `--run-dir` | 必填，Notebook 自动生成 | 临时磁盘的独立运行目录 |
| `--drive-root` | `/content/drive/MyDrive` | 已挂载的 Drive 根目录 |
| `--cookie` | Drive 下上述私有路径 | 本地已有登录 JSON 的云端副本 |
| `--segment` | 300 秒 | 分段时间，实际边界取决于源流关键帧 |
| `--initial-wait` | 900 秒 | 首次未开播等待上限 |
| `--offline-grace` | 180 秒 | 连续确认下播等待时间 |
| `--max-record` | 43200 秒 | 开始直播后的本场时间上限 |
| `--drain-timeout` | 7200 秒 | 停止录制后渲染、合并、校验和备份的总收尾上限 |
| `--min-free-gib` | 10 GiB | 临时盘剩余空间保护阈值 |
| `--preflight-only` | 关闭 | 仅验证环境，不连接直播间 |

公开 Notebook 的 `ROOM_URL` 留空，必须自行填写后再执行参数单元格。若只录制约一小时，设置 `MAX_RECORD_HOURS = 1`；达到上限后继续完成渲染、合并和备份，因此总运行时间可能超过一小时。不要将填写了个人参数或保存了运行输出的 Notebook 提交到公开仓库。

实际入口仅支持 Linux/Colab；状态机测试支持 Windows。Colab 专用依赖在 `colab_support/requirements.txt`，不需要 biliup、其他平台或视频下载工具。

预检检查系统工具、Noto Sans CJK SC 字体、观看 Cookie 格式，并实际执行两秒合成视频的 ASS 烧录和 H.264 NVENC 编码。任何一步失败都停止，不静默回退 CPU 编码。Notebook 显示预检成品供人工检查中文字形。正式运行会再次做预检，防止运行时或依赖改变后沿用旧结果。

系统 FFmpeg 是否包含可用 NVENC、驱动与编码接口是否匹配，必须由当前实例实测。本地测试不能证明 Colab T4 已通过；若预检失败，按输出检查当前实例环境，不直接开始录制。

## 3. 录制、备份与结束

入口直接使用项目默认参数，覆盖云端运行参数，不读取本地 `configs/global.yml`，不扫描 `DMR-*.yml`，不启动宿主、Uploader、Cleaner 或 WebService，不执行在线更新。

视频先写临时磁盘。每个完成分段与其 ASS 同时进入备份队列，渲染通过现有 Render 类串行处理；成品完成后再备份。录制屏障、全部分段渲染和分段备份完成后，入口按分段编号合并 `rendered/` 成品，检查各段媒体流签名、总时长和全片解码，再将 `merged/complete.mp4` 校验备份。合并失败会保留所有分段并将本场标记为失败，不会静默改用重新编码。仅处理关闭写入的分段，使用会话及分段编号去重。取流时选择可用最高画质，优先同画质 AVC，并记录返回的 quality 和 stream_type；低于 10000 时提示检查登录及画质。

备份目录：

```text
MyDrive/DMRColab/runs/<运行编号>/
  source/       原片分段
  danmaku/      ASS 文件
  rendered/     弹幕版 MP4 分段
  merged/       全场合并 MP4（stream copy，经过时长和全片解码校验）
  manifest.json 状态、耗时、计数、校验值、错误和源代码版本
  events.log    白名单事件日志
```

备份使用临时名称写入，关闭后重新读取验证大小与 SHA-256，再改成正式名称；最多尝试三次。检查点与备份使用同一串行队列，避免并发覆盖。这里验证的是**挂载目录读回一致性，不是 Drive 服务端持久性保证**，请在 Drive 网页抽查成品。

下播状态查询失败会打断“连续下播”计时，不直接判为下播。到达录制上限、收到受控停止或空间不足时，停止接收新录制工作，等待视频线程提交最后一段，再停止弹幕线程，最后等待全部渲染及备份。下载器完成屏障与任务登记共同决定结束，不把队列暂时为空当作完成。

程序结束后保留运行时。受控停止可在运行目录创建 `STOP` 文件；Notebook 的运行单元格捕获第一次中断会请求受控停止，再次中断通过独立的 `FORCE_STOP` 文件明确请求强制结束。Notebook 启动的监督进程使用独立进程会话，不接收 Notebook 进程组的中断；监督器自身的首次 SIGINT 也只请求正常收尾，不根据 `STOP` 是否存在推断中断次数。强制中断、收尾超时、渲染失败、备份失败、空间不足以及没有录到有效分段都不是成功。

监督进程对整个 worker 进程组设置截止时间，包含被第三方 I/O 或 FFmpeg 卡住的情况。若最终元数据备份失败或强制超时，Drive 清单可能停留在旧状态，以本地清单和退出码为准，不能仅看 Drive 上某份 success 就认定本次完全成功。

本地 `console.log` 提供环境预检及录制阶段信息；清单的 `phase` 和最近 30 条 `diagnostics` 可区分取流、最长约 15 秒的分辨率探测、弹幕就绪和 FFmpeg 启动。FFmpeg 只记录固定错误类别（例如 `http_forbidden`、`network_timeout`、`decoder_missing`）与退出码，不保存原始错误行。备份事件日志也包含这些白名单诊断；备份仅包含白名单事件日志，不包含第三方原始日志、完整配置或签名流 URL。所有原片和成品都保留在临时盘，首版不自动删除，因此长直播应关注磁盘使用。没有跨运行时续录或自动恢复功能；新试跑使用新的运行编号。

合并采用流复制，不再次编码视频。合并前要求分段编号连续、媒体流参数一致，并预留约成品分段总大小的 1.1 倍加最低磁盘余量；分辨率或编码参数变化时会明确失败。合并校验会解码全片，耗时计入两小时收尾上限；时长与解码通过仍需人工检查音画及弹幕同步。

## 4. 验证与切换

离线测试：

```text
python -m unittest discover -s checks -p "test_colab*.py" -v
```

真实适配测试需要上述 Colab Python 依赖，以及 PATH 或项目 tools 目录中的 FFmpeg/ffprobe。测试仅在本机回环 HTTP 服务提供合成视频，不连接直播间、不读取真实账号 Cookie。

首次云端试跑将 `MAX_RECORD_HOURS` 设置为 `0.2`，验证两个以上五分钟分段和限时收尾。随后恢复参数，执行一场一到两小时的自然下播试跑：

1. 核对直播画质、视频和弹幕连接，查看是否出现重连。
2. 核对原片、ASS、成品数量，播放每个分段，重点检查最后一段及弹幕同步。
3. 检查本地退出码为 0、清单为 success、`merge.status` 和 `merge.backup.status` 均为 success，Drive 中 `merged/complete.mp4` 可读取；渲染或备份失败必须出现在错误列表。
4. 查看每段实际渲染耗时、排队时间及与视频时长的比例；弹幕条数为 0 需要结合直播内容判断，不能自动等同于正常捕获。
5. 从 Colab 界面记录运行前后的 CU，Notebook 可保存 `usage.json`；不按显卡型号推算消耗。
6. 检查完毕、确认需保留的数据均已保存后，手动断开并删除运行时。

云端真实验收完成前，不切换现有正式任务。后续本地自动开播触发、浏览器自动连接和正式迁移单独实施。

## 5. 更新 Notebook 和失败后重试

升级源码不会自动更新 Drive 中已经保存的 Notebook 单元格。更新后请从 fork 的 `codex/colab-poc` 分支重新打开 `notebooks/colab_record.ipynb`，另存一份私有副本并填写参数。现有运行时目录不会自动拉取更新；确认没有活动任务后再手动 `git pull --ff-only`，或使用新运行时。

每次重试都重新执行参数单元格，生成新的 `RUN_ID`。开始运行前若已有清单、锁或停止标记，会明确拒绝复用目录，不会把旧的 `failed` 输出误当成新任务结果。保留原目录供检查，不删除原片或失败证据。
