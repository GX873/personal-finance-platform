# Tailscale 部署验证记录

日期：2026-09-28

## 结果

- 阿里云 ECS 已加入个人 Tailscale 网络。
- Tailscale Serve 已启用，入口为 Tailnet-only HTTPS。
- Tailscale Funnel 未启用。
- Nginx 仅监听 `127.0.0.1:80` 和 `[::1]:80`。
- 应用仍仅监听 `127.0.0.1:8000`。
- `FINANCE_SESSION_HTTPS_ONLY=true` 已启用。
- `tailscaled`、Nginx、理财应用、每日检查定时器和备份定时器均为 active。
- 私网首页跳转登录页成功，`/health` 返回 HTTP 200。
- 从非 Tailnet 网络验证，公网 TCP 80、443、8000 均不可达。
- 最新 SQLite 备份通过完整性校验。

## 客户端

已授权的电脑安装 Tailscale 并登录同一账号后，通过 Tailscale 控制台显示的 `.ts.net` HTTPS 地址访问。Windows 系统代理已将 `*.ts.net` 加入绕过列表；其他设备如使用代理，也需要对该域名设置直连。

## 保留的恢复入口

- SSH 公钥维护入口保持可用，并继续受阿里云安全组来源限制。
- Nginx 收口前的配置保存在服务器 `/etc/nginx/sites-available/personal-finance.pre-tailscale`。
- 服务器 DNS 使用 Netplan 持久配置，避免证书续期时再次使用不可用的 DHCP DNS。

本记录不包含 Tailscale 登录链接、设备密钥、Tailnet 凭据、环境变量值、Cookie 或理财数据库内容。
