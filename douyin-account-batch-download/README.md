# 抖音批量下载＋转文字 · Codex Skill v2

**如果你正在 GitHub 源码仓库查看本文件，请先从 [Releases 下载完整安装包](https://github.com/anshuyi/douyin-text-extractor-skill/releases/latest)。仓库源码与 GitHub 自动生成的 Source code ZIP 不含运行环境和模型，不能直接作为免配置安装包。下文的“本包”指 Releases 完整版。**

给一个账号主页分享链接，自动逐条下载公开视频、识别说话内容、保存文字，检查通过后删除本工具下载的对应临时视频。不登录抖音、不读取 Cookie、不上传视频转写。

## 学员怎么用

将 ZIP 保存到自己的 Windows x64 电脑，告诉 Codex：

> 请安装这个抖音文字素材提取 Skill，保留 runtime 环境和模型，然后提取这个账号视频里的说话内容：［账号主页分享短链］。成功转写的视频按工具默认规则清理。

也可将整个 `douyin-account-batch-download` 文件夹解压到自己 `%USERPROFILE%\.codex\skills\`，重新打开 Codex后使用。已有同名旧版时，让 Codex 先备份再更新。下载结果不要存到 Skill 目录里。

本包带 Python 3.12.10、识别依赖和 large-v3-turbo 多语言模型。正常使用无需安装 Python、FFmpeg、显卡驱动、填写 API Key，也无需首次联网下载模型。需要 Windows 10/11 x64、网络及可访问的公开抖音页面；建议至少 8 GB 内存，并为解压和失败视频留足磁盘空间。模型使压缩包较大，这是免去首次下载和兼顾转写质量的代价。CPU 转写需要等待，进度逐条显示。本包未验证 Windows ARM、Mac、Linux。

若电脑缺少微软 Visual C++ x64 运行库，启动检查会提示，尚不会下载视频。让 Codex 帮你从[微软官方](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)下载 x64 安装程序，确认后安装/修复再重试即可。不从不明网站复制 DLL，不自动修改系统。这是可能需要的一次性系统组件，不能承诺所有电脑完全零配置。

## 保存位置和重试

默认在自己的“下载\抖音文字素材\作者ID\文字\作品ID”内：`正文.txt` 是含来源信息的机器语音稿，`字幕.srt` 是时间轴字幕，`transcript.json`、`record.json` 保存识别和完整性信息。作者目录的 `catalog.json` 是作品清单，`processing-report.json` 记录结果。

无有效语音、低置信度、异常重复、损坏或转写失败的视频保留在 `.work/作品ID/`；不会当成成功结果删除。需要保留所有成功视频核对时，让 Codex 加 `-KeepVideos`。默认清理仅限本工具下载且记录所有权的文件，不删除以前下载的其他视频。

中断后给同一个账号、同一个输出目录重跑即可：完整且未改动的文字会跳过，自有临时视频会复用。修改过文字稿时不会覆盖，应先核对或另选输出目录。

需要账号“分享主页”短链，单条作品不能代替。遇到登录要求、验证码、限流或接口变化会报告失败，不要求登录账号。`partial` 代表有未完成项或只处理了测试样本，不能说整号完成。

所有文字是机器初稿，可能有错字、漏字、语言误判或未检测到的幻觉。价格、姓名、数字和重要信息需要校对；工具识别语音，不自动描述画面或提取全部屏幕文字。

## 来源与许可

下载基于抖音精选公开页面及分页接口。[Python](https://www.python.org/)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[识别模型](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo)的许可保留在 `runtime/python/LICENSE.txt`、各依赖 `.dist-info` 目录、`runtime/model-turbo/LICENSE.txt`。环境版本及哈希见 `runtime/bundle-info.json`。

包内无制作者账号、Cookie、旧视频、私人文字素材或运行日志。只保存有权使用的内容。
