# 个人理财平台

这是一个面向单用户的自托管个人理财平台，用于记录现金、基金持仓和交易流水，自动更新有日期依据的基金净值，并生成基于真实数据的每日操作建议。

平台不会连接券商执行交易，也不会自动买入或卖出。持仓导入、现金确认和交易记录均需要用户明确确认；缺失或过期的数据会保持“未知”状态，不会被当成可投资资金。

## 主要功能

- 记录账户、现金桶、基金持仓、交易流水和持仓成本
- 管理固定支出、流动储备、投资预算和自由支配预算
- 导入 CSV、XLSX 和持仓截图，先预览、再确认、后入账
- 自动获取中国公募基金的已公布净值，并保留日期和来源
- 按“核心资产 + 少量卫星仓位”生成条件式建议
- 给出买入、持有、分批减仓、卖出或不操作建议
- 显示建议金额、触发条件、最大风险和止损纪律
- 通过邮件发送每日检查结果，可选接入微信兼容通知渠道
- 保存审计记录，支持自动备份、完整性校验和数据导出
- 使用 FastAPI、SQLAlchemy、SQLite、Nginx 和 systemd，适合小型云服务器

## 当前理财规则

默认预算规则以每月工资 `4000 元`为基础：

| 用途 | 每月金额 | 规则 |
| --- | ---: | --- |
| 固定支出 | 2700 元 | 日常必要开支 |
| 流动储备 | 1000 元 | 放在余额宝或银行卡，不用于高波动投资 |
| 投资预算 | 200 元 | 只有在现金已确认且风险条件满足时才可使用 |
| 自由支配 | 100 元 | 不计入投资资金 |

这些金额是预算上限，不代表账户里已经存在对应现金。系统只有在余额经过确认后，才会给出明确的新买入建议。

## 技术环境

- Python 3.12 或更高版本
- FastAPI + Uvicorn
- SQLAlchemy 2 + Alembic
- SQLite
- Jinja2 + 原生 JavaScript/CSS
- openpyxl、Pillow、Tesseract OCR
- pytest、Ruff、mypy
- 生产环境：Ubuntu 24.04、Nginx、systemd

## 本地运行

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

Windows PowerShell 使用：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

初始化数据库和管理员：

```bash
python -m alembic upgrade head
python -m finance_app.cli init-admin --username admin
uvicorn finance_app.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。管理员密码通过交互提示输入，不会回显。

本地开发默认使用 `./data/finance.db`。`.env`、`data/`、`uploads/` 和 `backups/` 可能包含凭据或个人财务数据，不应提交到 Git。

## 在阿里云服务器部署

部署脚本面向 Ubuntu 24.04：

```bash
sudo bash deploy/install.sh
```

安装脚本会：

- 创建无登录权限的 `financeapp` 系统用户
- 将版本安装到 `/opt/personal-finance/releases/<版本>`
- 将 `/opt/personal-finance/current` 指向当前版本
- 将数据库、备份和上传文件放在 `/var/lib/personal-finance/`
- 将环境配置放在 `/etc/personal-finance/finance.env`
- 安装并启用应用、每日检查和备份服务
- 配置 Nginx，仅由 Nginx 转发到本机应用端口
- 保留 SSH、阿里云管理代理和阿里云备份服务

安装脚本不会修改阿里云安全组。请在阿里云控制台单独配置安全组规则。

检查服务状态：

```bash
systemctl status finance-app.service --no-pager
systemctl status finance-daily.timer finance-backup.timer --no-pager
curl --fail --silent http://127.0.0.1/health
```

## 通过 SSH 隧道访问网站

推荐让网站只通过 SSH 隧道访问。阿里云安全组只需允许当前电脑公网 IP 的 TCP 22，不需要向公网开放网站端口。

先确认私钥能够登录：

```powershell
ssh -i "$env:USERPROFILE\.ssh\finance_platform_second_pc" root@<服务器公网IP> whoami
```

如果返回 `root`，使用同一个私钥建立隧道：

```powershell
ssh -N -T `
    -L 127.0.0.1:18080:127.0.0.1:80 `
    -o BatchMode=yes `
    -o IdentitiesOnly=yes `
    -o ExitOnForwardFailure=yes `
    -i "$env:USERPROFILE\.ssh\finance_platform_second_pc" `
    root@<服务器公网IP>
```

保持这个 PowerShell 窗口运行，然后在浏览器打开 `http://127.0.0.1:18080`。

私钥文件名必须与当前电脑上实际存在的文件一致。如果测试登录使用的是 `finance_platform_second_pc`，隧道命令也必须使用该文件；引用另一台电脑上的 `aliyun_finance_ed25519` 会出现 `Identity file ... not accessible` 和 `Permission denied`。

## 管理员密码

服务器上的命令必须使用生产数据库路径。修改管理员密码：

```bash
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance change-password --username admin
```

密码修改后，新登录必须使用新密码；已经登录的浏览器应在网页中主动退出，再使用新密码重新登录。不要把密码放进命令行、环境文件、Git 提交、Issue 或聊天记录。

## 数据录入与导入

结构化文件支持 CSV 和 XLSX，持仓截图支持 PNG、JPG 和 JPEG。导入过程分为三步：

1. 上传文件并解析候选数据。
2. 检查基金代码、名称、份额、成本、市值、现金和日期。
3. 明确确认后写入账本并生成审计记录。

截图 OCR 只生成候选值，不能直接写入数据库。未知现金必须保持未知，不能用 `0` 代替；当前可投资现金经过用户明确确认为零时，才可以记录为 `0`。

详细字段说明和限制见 [数据导入指南](docs/data-import.md)。

## 每日检查与建议

`finance-daily.timer` 每五分钟唤醒一次任务，应用只在设置页面配置的北京时间窗口内执行每日检查。重复调用具有幂等保护，不会产生重复提醒。

查看状态和日志：

```bash
systemctl status finance-daily.timer finance-daily.service --no-pager
journalctl -u finance-daily.service --since today --no-pager
```

手动执行一次指定日期检查：

```bash
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance daily-check --date 2026-09-25
```

只读演练：

```bash
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance daily-check --scheduled --dry-run
```

演练模式不发送通知、不请求行情，也不保存更改。普通基金可自动更新净值；没有公开基金代码的金额型产品需要人工更新金额，否则系统会将相关数据标记为过期。

## 邮件通知

非敏感设置可以在登录后的网站设置页修改。SMTP 主机、账号和授权码保存在服务器环境文件中：

```ini
FINANCE_SMTP_HOST=smtp.example.com
FINANCE_SMTP_PORT=465
FINANCE_SMTP_SECURITY=ssl
FINANCE_SMTP_USERNAME=finance@example.com
FINANCE_SMTP_AUTHORIZATION_CODE=<邮箱授权码>
FINANCE_SMTP_SENDER=finance@example.com
FINANCE_SMTP_RECIPIENT=owner@example.com
```

应使用邮箱服务商生成的客户端授权码，不要使用邮箱登录密码。修改环境配置后重启应用：

```bash
systemctl restart finance-app.service
```

## 备份、校验与导出

备份定时器默认每天创建经过校验的 SQLite 快照，并保留最近 14 份。

```bash
systemctl status finance-backup.timer finance-backup.service --no-pager
ls -l /var/lib/personal-finance/backups
```

手动备份：

```bash
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance backup \
  --directory /var/lib/personal-finance/backups --keep 14
```

恢复校验不会覆盖线上数据库：

```bash
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance restore-check \
  /var/lib/personal-finance/backups/<备份文件>.sqlite3
```

完整的运维、回滚和日志说明见 [运维手册](docs/operations.md)。

## 开发与发布检查

运行完整发布检查：

```bash
bash scripts/release-check.sh
```

该脚本会在私有临时目录中执行：

- pytest 和覆盖率检查
- Ruff 静态检查
- mypy 类型检查
- 临时数据库迁移
- 导入流程冒烟测试
- Shell 脚本语法检查
- 常见敏感信息模式扫描

也可以分别执行：

```bash
python -m pytest -q
ruff check .
mypy finance_app
```

## 项目目录

```text
finance_app/       应用、账本、行情、通知和页面代码
alembic/           数据库迁移
deploy/            Ubuntu、Nginx 和 systemd 部署文件
docs/              运维、导入、设计和验证文档
scripts/           发布检查脚本
tests/             自动化测试
```

## 安全原则

- 不把 `.env`、数据库、备份、截图、密码、SMTP 授权码或 SSH 私钥提交到 Git
- 不直接编辑运行中的 SQLite 数据库
- 不把流动储备金当成高波动投资资金
- 不根据缺失、过期或无法核实的数据生成确定性买卖建议
- 不自动执行交易，所有资金操作由用户在正式交易平台手动确认
- 公网 HTTP 仅适合受限的临时引导环境；长期公网访问应配置 TLS

本项目用于个人资产记录和决策辅助，不承诺收益，也不能替代基金合同、产品说明书或持牌专业意见。
