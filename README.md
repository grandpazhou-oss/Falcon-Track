# Falcon Track v0.1

商机雷达 / 商機レーダー是一套在本机运行的物流商机工作台。它把公开信息源、线索证据、营业反馈和可选的 AI 分析集中在一个页面。

- 从公开来源发现采购需求、企业公告及在华经营动态
- 按证据把线索分为 A、B、C 级，保留原文、判断依据和人工反馈
- 支持中文和日语界面以及浅色、深色主题
- 可选 Flash 快速筛选和 Pro 深度分析，密钥通过系统凭据库或环境变量读取

## 本机运行

建议使用 Python 3.12。项目使用 Django、SQLite、Scrapy 和本机 Prefect。

```bash
python3 scripts/local.py setup
.venv/bin/python scripts/local.py start
```

启动后打开 [本机工作台](http://127.0.0.1:8765/)。Windows 可依次运行 `setup-windows.cmd` 和 `start-windows.cmd`。自动采集、搜索和模型分析默认关闭。在「数据来源」可手动采集，在「AI 设置」可配置可选连接；`data/` 保存本机数据库和采集原件。

## 开发与测试

```bash
uv sync --extra dev --frozen
uv run pytest -q
uv run python manage.py test
uv run python manage.py check
```

新增来源见 [采集器说明](docs/ADD_SOURCE.md)，组件及许可见 [依赖说明](docs/DEPENDENCIES.md)。

## 许可证

[MIT](LICENSE)
