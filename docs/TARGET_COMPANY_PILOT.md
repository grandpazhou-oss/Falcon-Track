# 目标日企名单试点

## 实施计划

1. 在公开仓库的独立分支上核对现有读取、归档和去重能力，核验 5 家研究示例的官方依据与入口使用范围。
2. 建立手动命令和独立证据目录，分开保存事件、原文版本与营业判断，使用离线夹具验证日期、法人和重复处理。
3. 用同一命令执行低频原站烟测，报告实际预算、失败、未知及历史样例，再做全量回归。


## 运行

在项目目录，使用完成依赖安装的 Python。下例为 macOS，Windows 对应解释器为 `.venv\Scripts\python.exe`。配置、命令和输出均支持 UTF-8。

```sh
# 只列名单与入口，零网络请求
.venv/bin/python manage.py target_watch

# 读取所选母公司的当前官方列表，每个入口最多取 2 篇详情
.venv/bin/python manage.py target_watch --company shibaura --online \
  --max-requests 8 --details-per-entry 2 --output ~/falcon-target-pilot

# 5 家全名单验收，同时核读明确标记的历史参考公告
.venv/bin/python manage.py target_watch --online --include-references \
  --max-requests 18 --details-per-entry 2 --output ~/falcon-target-pilot
```

首次用空目录；续跑指向同一目录，保留原有人工字段、版本与每次运行记录。可重复传入 `--company`，或用 `--config <json>` 指定自己的名单。命令不读写工作台业务表，也没有注册到后台任务或来源页。一个目录同一时刻只允许一个写入进程。

### 研究示例，未经营业选定

[可编辑名单](../config/target_companies.json) 容纳 1–30 家。每家分别登记母公司正式名称、可证实的别名、官网、各类入口、法人名称、地点、关系说明、出处、核对日期和访问条件。没有核实到的别名、独立 IR 或 RSS 入口留空。法人可标 `verified`、`unknown`、`conflict`。已核必须有官网依据；跨母公司重复声称已核会阻止运行，应先核对合资或重名关系。新增配置中的网址必须与登记官网同源。扩充名单只改变这条人工命令的覆盖范围。

| 企业编号 | 日本母公司 | 已核到的在华主体及关系 | 本次处理 |
|---|---|---|---|
| otsuka | 大塚製薬株式会社 | 天津大塚飲料有限公司；公告明确称为子公司 | 有固定 RSS 和新闻列表；整篇归档暂缓。[原发公告](https://www.otsuka.co.jp/company/newsreleases/2026/20260205_1.html) · [使用条件](https://www.otsuka.co.jp/terms-of-use/) |
| nishikawa | 西川ゴム工業株式会社 | 湖北西川密封系統有限公司；列为中国生产据点，持股比例待核 | 新闻列表、IR 列表和指定 PDF。[湖北工厂公告](https://www.nishikawa-rbr.co.jp/upfile/20250917news%20.pdf) |
| shibaura | 芝浦機械株式会社 | 芝机精密机械（安徽）有限公司；公告明确为发行人新设的制造法人 | 新闻列表、IR 列表和 HTML 详情。[安徽公告](https://www.shibaura-machine.co.jp/jp/NEWS/press/20260420.html) |
| oval | 株式会社オーバル | 合肥オーバル儀表有限公司；合并报表子公司 | 新闻、IR 和 PDF 登记为参考，归档待授权。[合肥公告](https://www.oval.co.jp/fwp/wp-content/uploads/2026/04/20260428-2.pdf) · [使用条件](https://www.oval.co.jp/attention/) |
| sumibe | 住友ベークライト株式会社 | 蘇州住友電木有限公司；公告称中国集团企业，直接持股比例待核 | 新闻、IR 和详情登记为参考，归档待授权。[苏州公告](https://www.sumibe.co.jp/topics/2024/it-materials/0924_01/index.html) · [使用条件](https://www.sumibe.co.jp/site-policy/) |

名单核对日为 2026-11-20。上述链接用于核对研究示例；只有出现在批次 `reads` 中并带有本次原件哈希的记录，才计入命令实取。未把企业规模或是否可能外包当作准入事实。

### 入口与请求预算

- `list` 读一个列表页，按标题选取可能相关的同源公告；`rss` 读取固定 RSS/RDF/Atom，从条目取同源详情。两者均只展开一层。顶部列表题名没有相关词时会漏报，索引返回零篇也有记录。
- `detail` 读取明确列入名单或在上述入口发现的 HTML；`pdf` 复用项目独立进程解析器读取文本与真实页码。历史参考 URL 需要显式添加 `--include-references`；历史分页和按词搜索均未实现，传入 `--query` 会返回不支持。
- 请求上限涵盖 robots、正常页面及重定向。沿用项目默认 1.1 秒共享限速、逐跳公网地址校验、TLS、超时与体积上限。本命令额外拒绝跨源的正文及 robots 跳转。每个入口失败独立记录；触发访问限制时停止该入口。
- `reviewed` 表示已审阅相应入口，网络请求仍须通过实时 robots 检查。`hold` 直接跳过。三个暂缓站点可供人工打开链接核查，只有取得适用于企业内部归档的依据后才应修改访问状态。

### 怎样读结果

- `target-events.json` 的 `events` 是累计事件。首版为每篇原发披露选择一条归属明确的核心变化，其他具名事实放在 `other_matching_passages`，有地点和动作但句内主体不足的原话放在 `passages_for_review`。不会用一篇跨国财报自动生成多个销售项目。
- `evidence` 按版本保存来源 URL、原件相对路径、SHA-256、抓取时间、原文摘录与 HTML XPath 或 PDF 页码；日期字段各有独立证据。日期允许精确到年、月或日，空值表示未取得对应证据。过期计划单列为 `elapsed_plan`。单纯“董事会决议”不能证明已经动工或投产。
- `sales_status` 和 `manual_note` 属于人工判断；`logistics_inference`、`sales_question` 用于初次访谈。续跑不覆盖人工记录。没有写入采购状态或营业分数的路径。
- `raw/research/<SHA-256>.*` 保存响应原件；`runs/<批次>.json` 记录入口、发现链接数、实际访问、未展开项、受限原因、失败和未知。原始文档保存在本地。
- 同一企业的相同文档哈希、可确证的相同摘录转载、或同址同主体及项目标题明确标记的更正，关联到原事件；保留各来源与不同版本。关系不确定时另存并标 `possible_related`；人工复核时可对照原件。未承诺跨措辞、跨语言或跨集团站点的自动消歧。

### 批次统计口径

| 字段 | 分母与含义 |
|---|---|
| configured_companies / selected_companies | 名单总企业数 / 本批选中数 |
| attempted_sources | 实际进入读取流程的配置入口数；同批已读 URL 会复用，记录在 reused_urls |
| requests / successful_reads | 实际发出的 HTTP（含 robots、跳转）；成功取得并存档的业务页面或文件（包含列表） |
| documents | 已解析可读的详情数，不含列表，也不把正文过短、PDF 无文本层算成成功 |
| candidate_events / new_events / duplicates | 本批命中披露数 / 新建记录数 / 已识别事件的重复观察数；疑似关联数单列 suspected |
| failures / terms_holds / budget_skips | 请求或解析失败的读取数 / 因使用范围暂缓的入口数 / 开始前已无预算的入口数 |
| unknown | 命中披露中主体、公告日、阶段或该阶段所需的日期仍有缺口的条数；具体缺项列在 unknown_fields。计划未到期时不因缺实际日计错 |
| time_groups | 按命中披露分列近期事实、未来安排、过期未证实计划、历史事实、仅知披露过旧、时间待核；近期开窗取 365 天 |

## 验收与限制

本轮在 macOS 上实测。两个独立原发入口能到达候选，其他来源的情况按批记录。固定 RSS、异常响应、跨来源转载和修订在离线样例中验证。本轮没有可报告的近期新项目；小样本不能估算整体召回率、采购意愿或转化率。详见[验收记录](TARGET_COMPANY_ACCEPTANCE.md)。

停止运行该命令即可停用。需要回退源码时，公开仓库可回退本次提交；本机保留原有业务改动，在应用反向补丁前先用 `git apply --reverse --check` 检查。原始输出目录可独立保留回查，回退不涉及数据库迁移。
