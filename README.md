# calibre-douban enhanced

这是一个用于 Calibre 桌面版的豆瓣读书元数据下载插件，基于 `https://book.douban.com` 网页解析图书信息和封面。

本仓库 fork 自 Gary Fu 的 `calibre-douban`，原项目采用 Apache-2.0 协议。原作者 README 保留在本文末尾。

## 本 fork 的主要改动

- 支持豆瓣登录 Cookie，可填写普通 `Cookie` 请求头、Netscape 格式 Cookie 文本，或 Netscape Cookie 文件路径。
- 支持自定义 User-Agent，方便与已完成豆瓣验证的浏览器环境保持一致。
- 有豆瓣 ID 时直达 `https://book.douban.com/subject/{id}/`，减少搜索请求。
- 有 ISBN 时优先访问 `https://book.douban.com/isbn/{isbn}/`，失败后才回退到搜索页。
- 兼容 `new_douban`、`douban`、当前插件 ID 保存的豆瓣 identifier。
- 识别豆瓣人机验证页面，触发后停止本轮 fallback 搜索，避免继续加重风控。
- 增加调试日志选项，便于排查搜索、下载、解析、封面缓存等问题。
- 网络请求增加超时、响应解码兜底、并发任务异常隔离。
- 打包脚本跳过 `__pycache__`、`.pyc`、`.pyo`。
- GitHub Actions 根据 `PROVIDER_VERSION` 自动构建并发布 Release。

## 安装

从本仓库 Release 页面下载 `NewDouban.zip`，然后在 Calibre 中安装：

1. 打开 Calibre。
2. 进入 `首选项` -> `插件`。
3. 点击 `从文件加载插件`。
4. 选择下载的 `NewDouban.zip`。
5. 重启 Calibre。

也可以本地构建：

```bash
python build.py
```

构建产物位于：

```text
out/NewDouban.zip
```

## 推荐配置

插件安装后，在 Calibre 的元数据下载插件设置中配置 `New Douban Books`。

### douban concurrency size

并发查询数。建议设为 `1` 或较低值。豆瓣较容易触发访问限制，过高并发会增加人机验证概率。

### douban random delay

建议保持开启。插件会在详情页请求前随机等待一小段时间。

### douban login cookie

可选，但推荐配置。支持三种格式：

普通 Cookie 请求头：

```text
bid=...; dbcl2=...; ck=...
```

Netscape Cookie 文本：

```text
# Netscape HTTP Cookie File
.douban.com	TRUE	/	FALSE	1893456000	bid	xxxx
```

Netscape Cookie 文件路径：

```text
C:\Users\you\Downloads\douban-cookies.txt
```

如果豆瓣网页端已经出现人机验证，请先在同一登录账号的浏览器中完成验证，再重新导出 Cookie 或确认 Cookie 仍有效。

### douban user agent

可选。建议填写你完成豆瓣验证的浏览器 User-Agent，让插件请求环境与浏览器更一致。

Chrome 或 Edge 可在开发者工具的 Network 面板中打开任意豆瓣请求，复制请求头里的 `User-Agent`。

### douban debug logging

默认关闭。排错时开启后，日志会输出：

- 请求 URL、响应状态、耗时
- 搜索页和详情页 HTML 长度
- 搜索结果数量
- 解析出的字段
- 是否命中 Cookie、自定义 User-Agent、封面缓存
- 人机验证页面的关键信息

调试日志不会输出 Cookie 原文。

## 降低豆瓣人机验证概率

无法保证完全避免豆瓣质询，但可以降低概率：

- 优先使用 ISBN 或已有豆瓣 ID 查询，减少搜索页请求。
- 并发数设为 `1`。
- 开启随机延迟。
- 配置登录 Cookie。
- 配置与你浏览器一致的 User-Agent。
- 批量下载元数据时分批执行。
- 出现人机验证后，在网页端完成验证，再回 Calibre 重试。

## 自动发布

`.github/workflows/release.yml` 会在推送到 `main` 后自动：

1. 从 `src/__init__.py` 读取 `PROVIDER_VERSION`。
2. 生成 `vX.Y.Z` tag。
3. 执行 `python build.py`。
4. 上传 `out/NewDouban.zip` 到 GitHub Release。

发布新版本前请先修改：

```python
PROVIDER_VERSION = (x, y, z)
```

如果同名 tag 已存在且不是当前提交，workflow 会失败，避免同一个版本号覆盖不同代码。

## 许可

本 fork 保留原项目 Apache-2.0 许可。原作者信息和许可证声明见 `LICENSE` 以及下方原 README。

---

## 原项目 README

## calibre-douban
Calibre douban metadata download plugin.
Based on https://book.douban.com web pages.

### Calibre插件

最近在使用calibre-web管理电子书，不过很多时候还是需要用到Calibre桌面版软件，批量管理，编辑电子书等功能，在calibre-web上已经使用calibre-web-douban-api搜素豆瓣元数据，但是桌面版Calibre软件缺没有办法使用，不过calibre可以使用插件，而且是使用python开发，因此可以把calibre-web-douban-api改造一下包装成calibre插件，简单元数据插件还是比较容易的

### 安装方法

下载地址：[NewDouban.zip](https://github.com/fugary/calibre-douban/releases/latest/download/NewDouban.zip)

从release页面下载zip包，然后再calibre中安装为插件即可。

参考文档：https://fugary.com/?p=423
