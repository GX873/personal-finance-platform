# 2026-09-29 部署验收记录

- 发布提交：`b274efc40b083a5cdd98c6261a1e04d44ba2e170`
- 服务器 release：`/opt/personal-finance/releases/b274efc40b083a5cdd98c6261a1e04d44ba2e170`
- 数据库迁移：`0004 (head)`
- 本地验证：`465 passed`；Ruff、mypy、`git diff --check` 通过
- 服务状态：`finance-app.service`、`finance-daily.timer`、`finance-backup.timer`、`tailscaled`、`nginx` 均为 `active`
- 定时器：每日检查与备份定时器均为 `enabled`
- 健康检查：`http://127.0.0.1/health` 返回 `{"status":"ok","version":"0.1.0"}`
- 数据保护：部署前备份已创建；`assets`、`holdings`、`transactions`、`cash_buckets`、`price_snapshots`、`users` 的行数与备份一致

GitHub 推送未完成：当前执行环境到 `github.com:443` 的连接被网络策略阻断，未使用强制推送，也未改写远端历史。
