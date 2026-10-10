# DanmakuRender-5 —— 一个录制带弹幕直播的小工具（版本5）
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
