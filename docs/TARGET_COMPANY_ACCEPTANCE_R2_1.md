# Falcon Track 目标日企监控 r2.1 验收

2026-11-21，macOS，Python 3.12。**热修和离线回归通过。金融厅认证 API 的在线验收仍受阻。** 两次正式运行均向清单端点发送了带密钥的 HTTPS 请求，均收到真实 HTTP 403。金融厅端返回 HTML 拦截页，尚无证据确认或否定密钥有效性。独立补测从发行人官网取得并解析一份真实年度报告。

## 两轮实网结果

两个批次各从空目录启动，分别有独立请求账本和水位。以下请求数包括网页 robots。定期报告按提交日筛选。

| 批次 | 实际请求 / 上限 | EDINET 结果 | 网页与事件 |
|---|---:|---|---|
| 5 家近 7 天，增量；批次 `20261120T162435400215Z-2caed2` | 16 / 40 | 11 月 21 日清单 HTTP 403；停止当前批次 EDINET，其余 6 天未读。API robots 请求 0 | 7 个网页或 RSS 索引成功；2 篇详情，1 条芝浦安徽历史计划、1 篇无可归属动作；住友苏州 2 条仅标题提示 |
| オーバル E02270，2026-06-19；批次 `20261120T162534186770Z-ff547f` | 3 / 12 | 指定日清单 HTTP 403。该日オーバル年度报告编号 S100YDYH；本次从 API 下载文书 0 份 | gBizINFO 法人索引 HTTP 200，1 个标题，0 篇详情、0 事件 |

常规批次的有效索引分别为大塚 RSS（5 个标题）、西川新闻／IR（10／10）、芝浦新闻／IR（88／49）、オーバル gBizINFO（1）、住友年度新闻（101）。住友使用当年展开的目录。网页账本保留了各站 robots 的 200 或 404。芝浦泰国关闭公告得 −35 分，详情请求 0。安徽记录的原计划期已过，未产生营业升级。[オーバル文书来源](https://info.gbiz.go.jp/hojin/edinetDownload?docId=S100YDYH)

**故障定位。** 在两次业务运行之外，以完全相同的认证客户端和指定日做 1 次限速诊断。系统钥匙串有可读值，进程未设置 EDINET 环境变量，凭据首尾无空白。响应为 HTTP 403、`text/html`、带边缘服务追踪头，正文匹配 `The request is blocked`。诊断只保存这些分类结果。官方文档说明，无效订阅密钥通常以 JSON 业务状态 401 报告。本次没有得到该状态，也没有得到成功清单。若需解除实网阻塞，可向 EDINET 支持报送访问时间、主机、HTTP 403 和拦截提示，请其确认访问条件。恢复后先用下方单日命令核对，再查 7 日清单。[金融厅 API 规格](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf) · [官方访问故障 FAQ](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0090_001.html) · [官方联系入口](https://www.fsa.go.jp/search/edinet-Information/information-00.html)

## 发行人原件独立补验

由本次取得的芝浦機械 L1 IR 索引中的「第103期有価証券報告書」链接进入。官网 robots 返回 404，随后同源文件 HTTP 200，共 2 次请求。文件 5,871,296 字节，SHA-256 为 `5a722a295f37d481a84b2566175b0e4b565c231041366be656c4a4beea07c5cf`。原件保存在本机独立验证目录。[发行人 IR 索引](https://www.shibaura-machine.co.jp/jp/NEWS/ir/) · [第 103 期年度报告 PDF](https://www.shibaura-machine.co.jp/documents/jp/ir/library/yuho/2025/103_yuho.pdf)

| 核验项 | 实际结果 |
|---|---|
| 主体 | PDF 第 1 页的芝浦機械株式会社与 E01482 对应名单同一主体 |
| 章节与页码 | 第 2 页为目录。正文「関係会社の状況」在 PDF 第 12–13 页，「設備の新設、除却等の計画」在第 36 页。复用法定报告 PDF 章节提取，共 156 行 |
| 抽取判断 | 第 12 页的上海关联公司行能证明所在地；这两节中没有足以独立归属的大陆产能变化，规则输出 0 条候选 |
| 读取范围 | 文件共 148 页，原有安全上限读取前 80 页。结论限于已读取的目标章节 |

本次发行人文件与金融厅接口分开统计。真实金融厅 CSV ZIP、文件编码、CSV 行定位及其主体归属质量仍待认证通道恢复后验证。

## 改动与离线回归

- 只有 L2 配置 `edinet_api_v2` 及精确匹配的官方 HTTPS 清单、文书端点启用认证通道。不请求 API robots，不跟随重定向；共享跨进程限速至少 1.2 秒。密钥仅在 TLS 客户端组装请求时加入，关闭 HTTP 调试回显。业务状态 401、429 熔断；404 可按原文书标志从 CSV 降级至 PDF。EDINET 报告、事件及对应证据写入「出典：EDINET（金融庁）」。
- 住友使用 `{year}`。12 月、1 月跨年、2 月均通过离线回放；1 月读取当年和上年两份索引，独立去重。网页 robots 超时仅在剩余预算内退避重试一次；403、404、格式无效均不重试。L3 的详情与落盘限制继续生效。
- 断网 pytest **614 passed + 31 subtests**，有 6 条既有依赖警告。Django test **157 passed**；`manage.py check` 为 0 问题；`makemigrations --check --dry-run` 为 `No changes detected`。测试覆盖白名单、无 API robots 请求、HTTP 200 下的业务错误、停读、出处与混淆目录。UTF-8 BOM、UTF-16 BOM、无 BOM 的 UTF-16LE 三种 CSV 使用合成样本验证；Windows 实机未测。
- 两轮批次与补验共 7 个已存档原件的 SHA-256 重算一致。对 28 个本地批次、原件及诊断／审计文件按真实密钥及其 URL 编码形式扫描，命中 0。无 Tavily 或模型调用，业务数据库通过只读方式核验。

## 正式库前后逐行核对

| 表 | 前 / 后行数 | 两次逐行 SHA-256 |
|---|---:|---|
| review | 0 / 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| salesfeedback | 0 / 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| keywordcandidate | 563 / 563 | `2b6f5ce6ce860dfe93f421cc0139ae643b67c16c0bc328c6a39486cc04342e27` |
| lead | 395 / 395 | `aeb3da895e5d5ca3c58d0822f36470dfa43a39c2cc17707db3f35672c2f49a2f` |
| researchevent | 136 / 136 | `5563064ae75f07b05edbfe73e9694ba2713068e0e9ec5e0f1f9e9c7c0fde71df` |
| modelcall | 94 / 94 | `0dcb8162304c104dfa02146f7c06d584a8e47e7752e8811d6b70aa09df36290a` |
| searchcall | 88 / 88 | `914146d997204f818ef2ae898c0463796f3838d85a84dfb97e96b1250036d333` |
| analysisrun | 59 / 59 | `e9b76f5f5de8805e61e8f86caad137e2361071476c97ba12c3afa88e026c2f24` |

## 复现

在项目目录使用两个独立空目录执行；密钥取自系统凭据库或进程环境变量。

```sh
.venv/bin/python manage.py target_watch --online --mode incremental --max-requests 40 --edinet-days 7 --output "$HOME/falcon-r21-week"
.venv/bin/python manage.py target_watch --online --mode incremental --company oval --max-requests 12 --edinet-date 2026-06-19 --output "$HOME/falcon-r21-oval"
```

实际运行的完整账本在相应批次目录下的 `runs/*.json`。本版本无数据库迁移。回退程序时恢复 r2 配套的配置和代码，保留两套独立验收目录。
