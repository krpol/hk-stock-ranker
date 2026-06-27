# hk-stock-ranker 启动指南

## 项目概述
港股股东回报排名工具，提供数据采集、排名计算与 Flask Web UI 浏览功能。

## . - hk-stock-ranker Web 服务

### 快速启动

```bash
cd /Users/huangzhen/hk-stock-ranker
source .venv/bin/activate
python main.py --web
```

**启动后访问**：http://localhost:5001

```yaml
subProjectPath: .
command: python main.py --web
cwd: .
port: 5001
previewUrl: http://localhost:5001
description: Flask Web UI，展示港股回报率排名，支持分页、周期选择和 Excel 下载
```
