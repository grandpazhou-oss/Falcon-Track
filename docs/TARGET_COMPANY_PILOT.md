# 目标日企名单试点 r2

这条手动命令监控指定日企的大陆经营变化。营业可以回查公告、判断项目进度，并自行决定是否联系。结果写进专用目录，工作台分级和营业反馈由原有流程管理。5 家企业都是研究示例，未经营业选定。

## 运行

先在项目目录执行 `.venv/bin/python manage.py target_watch` 查看有效配置。默认仅显示名单。macOS 示例：

```sh
.venv/bin/python manage.py target_watch --online --mode incremental \
  --max-requests 40 --max-details-per-company 3 --edinet-days 7 \
  --output "$HOME/falcon-target-watch"
```

Windows PowerShell 可在项目目录直接运行：

```powershell
.\.venv\Scripts\python.exe manage.py target_watch --online --mode incremental --max-requests 40 --max-details-per-company 3 --edinet-days 7 --output "$env:LOCALAPPDATA\FalconTrack\target-watch"
```

首次使用空目录，之后使用同一目录。可重复传入 `--company shibaura`、`--company nishikawa` 选择企业，或用 `--config` 替换名单。运行过程中有文件锁。请求预算含 robots 和跳转，取值 1–300；详情上限按企业计算，取值 1–30，旧参数 `--details-per-entry` 是这一上限的别名。

- **增量运行**是默认方式。按入口保存 URL 的 SHA-256、标题和发布日期指纹、日期水位、未完成详情和服务端提供的 ETag / Last-Modified。服务器返回 304 时仍会处理上轮积压。相同日期的新链接仍可进入预筛；未知日期只靠指纹去重。
- **历史回查**用 `--mode baseline` 重读当前索引，包括低于日期水位的条目。加 `--include-references` 可纳入名单中显式登记的历史参考链接，仍受合规分级和预筛约束。报告和新证据标记 `is_baseline`。同一地址标题和日期均未改变的正文订正，需要这种回查才能发现。未实现历史网站翻页或关键词搜索。
- **断点续跑**用同一配置、同一输出目录加 `--resume`。同日同范围的中断批次可跳过已经完成的索引；变更日期或名单会重新检查索引。每次调用重新计算请求与单企上限。失败项保留记录，待处理详情可继续重试。

## 四级读取范围

合规档位以**入口**为单位，企业可以同时有多个档位。实时 robots 与访问限制始终优先。升级档位前需核对具体入口的使用条件并更新审阅日期。

| 档位 | 读取内容 | 输出 |
|---|---|---|
| L1 `full_archive` | 同源官方列表或固定订阅，按标题分配详情预算 | 列表与获准读取的 HTML/PDF 原件、二进制 SHA-256、事实摘录、XPath 或真实 PDF 页码 |
| L2 `statutory_api` | 每个日期对全部选中提交者只请求一次文书清单。只处理 180 临时、120 年度、160 半期报告，优先 CSV ZIP；无 CSV、有 PDF 时读取 PDF | 官方文书编号、提交者代码、提交时间、文件哈希及 CSV 行或 PDF 页码。定期报告限设备计划与关联公司章节；集团报告进一步核对同句目标主体 |
| L3 `metadata_pointer` | 仅在内存处理已审阅的固定 RSS 或列表。拦截索引跳转，不下载标题所指详情 | 高分提示只含企业编号、标题、日期、链接、分数、命中词与 `pointer_only` 状态。原文由营业点击外链核实 |
| L4 `manual_watch` | 人工入口，自动 HTTP 0 次 | 在批次报告列出链接和人工状态 |

### 企业与代码

| 编号 | 日本企业 | EDINET 本体 / 母公司 | 当前公开入口 |
|---|---|---|---|
| otsuka | 大塚製薬株式会社 | E00956 / E21183 | 大塚製薬官方固定 RSS 为 L3。法定报告同时查大塚控股，须关联具体业务主体 |
| nishikawa | 西川ゴム工業株式会社 | E01112 | 新闻与 IR 列表 L1 |
| shibaura | 芝浦機械株式会社 | E01482 | 新闻与 IR 列表 L1 |
| oval | 株式会社オーバル | E02270 | 官网要求事先联系，保留 L4；经产省 gBizINFO 的该法人披露索引为 L3，通常只显示「有価証券報告書」 |
| sumibe | 住友ベークライト株式会社 | E00819 | 2026 年度新闻目录 L3，跨年需核对下一年度的公开索引 |

[静态名单](../config/target_companies.json) 可登记 1–30 家。字段包括中日名称、行业、营业负责人、提交者代码及核验出处、已核对的在华主体名称/别名、关系状态和证据，以及入口类型、使用条件和核对日期。其余四家未登记额外母公司过滤代码。修改源配置后重新运行查看名单；配置中不填写密钥或运行水位。新增 HTML 列表可指定 `links_xpath` 与 `title_xpath` 精确取到新闻标题。固定订阅读取本身不提供查询能力。

### EDINET 密钥

已有金融厅 EDINET API v2 凭据时，可在 macOS 钥匙串或 Windows 凭据管理器登记服务 `logistics-lead-radar-v02`、账户 `edinet-api-v2`。已有项目环境带有 keyring 时，下面的命令在本机隐式提示输入密钥：

```sh
.venv/bin/python -m keyring set logistics-lead-radar-v02 edinet-api-v2
```

Windows 用 `.\.venv\Scripts\python.exe` 替换解释器。也可由启动器注入进程环境变量 `EDINET_API_KEY`。避免写进项目的 .env、配置文件或命令行参数。没有凭据时每个 L2 入口都会记录 `skipped_missing_api_key` 并继续其他档位。客户端只向固定金融厅 TLS 主机发送带密钥的业务请求，对外账本仅保留无凭据的 URL 与 HTTP 状态；API 响应错误正文不会进入日志。文档的 `url` 是不带认证参数的接口位置，回查本地原件不需要密钥。[金融厅 API 规格](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html) · [CSV 有无标志说明](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0090_001.html)

## 标题评分与业务判断

[外置规则](../config/title_prescore_rules.json) 与项目城市表共同评分。地域或在华主体取最高一档（45 / 60），强、中动作也取最高一档（35 / 25）。弱全球信号加 15，常规噪音减 80，无正向地域时单一外国地名减 60。日本「中国地方」「中国支社」「中山町」等搭配先从地域匹配中去掉。同一标题独立提到上海等大陆地点时仍保留该证据。

45 分起先处理，25–44 分在高优先级与预算允许时补抓；L3 仅输出 45 分起的外链。25 分以下写入跳过账本。**这里的优先级只决定先读哪篇公告**。事件仍须用正文验证主体、动作与地点，不设置 A/B 商机等级。标题与实际经营场所可能不同。公告日、计划日、实际日期分别保留出处；归档日不代作事件时间。

## 文件与回退

- `watch_state.json` 保存运行水位与断点；`target-events.json` 保存累计研究事件、各版证据与人工状态。相同事件复用 r1 的保守去重，模糊关系单列。重新分析时保留人工备注。
- `raw/research/` 保存 L1 原件与 L2 法定报告。L3 只保留标题元数据。各轮 `runs/*.json` 记录请求、来源结果、候选/重复/未知、L3 提醒、未完成量和失败原因；`prescore_skipped.jsonl` 可追查被过滤的标题和规则。
- 新开独立输出目录即可重新建水位；保留旧目录以便回查。停止运行本命令即可停用。回退代码可在干净的发布分支上 revert r2 提交，使用 r1 输出备份。无需数据库迁移或重启工作台。

实际离线回放与真实网站验证分别记在 [r2 验收报告](TARGET_COMPANY_ACCEPTANCE_R2.md)。[r1 记录](TARGET_COMPANY_ACCEPTANCE.md) 保留作历史对照。
