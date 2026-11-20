# 主要依赖和许可记录

以下记录来自本次开发环境中已安装包的版本与元数据。普通安装以锁定的 `requirements.txt` 为准；完整解析图见 `uv.lock`。此表只列主要直接依赖，不是全部传递依赖的法律审查。

| 依赖 | 本次安装版本 | 包元数据标注的许可 | 用途 |
| --- | --- | --- | --- |
| Django | 5.2.19 | BSD-3-Clause | 网页、表单、ORM 与迁移 |
| Scrapy | 2.20.0 | BSD-3-Clause | 公开网页采集 |
| Prefect | 3.9.2 | Apache-2.0 | 本地工作流运行和记录 |
| Pydantic | 2.14.3 | MIT | 模型输出结构验证 |
| HTTPX | 0.28.1 | BSD-3-Clause | 可选模型 HTTP 请求 |
| python-dotenv | 1.2.4 | BSD-3-Clause | 本机配置 |
| Waitress | 3.0.2 | ZPL 2.1 | 本机网页服务 |
| psycopg | 3.3.9 | LGPL-3.0-only | 可选 PostgreSQL 驱动 |
| pypdf | 6.21.4 | BSD-3-Clause | 文本层 PDF 有界解析，页码与未处理范围 |
| keyring | 25.7.0 | MIT | OS 凭据库保存模型/检索 Key |
| keyring | 25.7.0 | MIT | OS 凭据库保存模型/检索 Key |
| filelock | 3.32.7 | MIT | 单机跨进程互斥 |

本项目自有代码的 MIT 许可不替代以上许可。模型 API、访问的商业网站、会员采购文件和工商数据服务，不因技术栈开源而自动变成免费或可自由再分发的内容。

当前接通来源不需要浏览器。可选 `scrapy-playwright` 和开发用 Playwright 不在默认安装所需的运行路径上；浏览器二进制也不包含在源码 ZIP 中。
