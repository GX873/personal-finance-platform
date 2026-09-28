# Tailscale 私网访问设计

日期：2026-09-28

## 目标

让本人已授权的电脑和手机无需 SSH 端口转发即可随时访问个人理财平台，同时不把网站、应用端口或数据库暴露到公网。

## 选定方案

阿里云 ECS 加入用户的 Tailscale 私有网络。Nginx 只监听服务器回环地址，Tailscale Serve 在 Tailnet 内提供自动管理证书的 HTTPS 地址。应用继续只监听 `127.0.0.1:8000`，SQLite、上传文件、备份和环境文件保持原有权限与位置。

访问链路：

```text
已登录同一 Tailscale 账号的电脑或手机
  -> Tailnet 加密连接
  -> https://<设备名>.<tailnet>.ts.net
  -> Tailscale Serve
  -> Nginx 127.0.0.1:80
  -> finance-app 127.0.0.1:8000
```

不启用 Tailscale Funnel，因为 Funnel 会把服务发布到互联网。首期也不启用 Tailscale SSH；现有公网 SSH 密钥入口继续作为维护和故障恢复通道，并保留阿里云安全组的来源 IP 限制。

## 组件与配置

1. 在 Ubuntu 24.04 上通过 Tailscale 官方软件源安装 `tailscale`，启用 `tailscaled` systemd 服务。
2. 执行非交互式 SSH 功能关闭的设备登录，生成一次性网页登录地址，由用户在浏览器中登录自己的 Tailscale 账号并授权该 ECS。
3. 启用 Tailnet 的 MagicDNS 与 HTTPS，并用 Tailscale Serve 将私网 HTTPS 请求代理到 `http://127.0.0.1:80`。
4. 验证私网 HTTPS 地址可登录、会话与主要页面正常后，将 Nginx 的监听地址由所有网络接口改为 `127.0.0.1:80` 和 `[::1]:80`。
5. 将 `FINANCE_SESSION_HTTPS_ONLY` 设置为 `true` 并重启应用，使登录会话 Cookie 仅通过 HTTPS 发送。
6. 确认阿里云安全组不再允许公网入站 TCP 80/443；TCP 22 维持当前受限规则。

## 数据与安全边界

- Tailscale 只改变访问路径，不迁移或读取理财数据库。
- 只有加入同一 Tailnet 且被账号授权的设备可以访问网站。
- 网站自身的管理员登录仍然保留，Tailscale 设备认证不能替代网站密码。
- Uvicorn、SQLite、备份目录和环境文件均不对 Tailnet 或公网直接开放。
- 保留应用原有安全响应头、上传大小限制、审计日志和备份定时器。
- 不记录或提交 Tailscale 登录令牌、认证密钥、Tailnet 名称等私密配置。

## 部署顺序与中断处理

部署采用“先增加私网入口，再收紧旧入口”的顺序：

1. 记录当前服务、端口、Nginx 配置和健康状态，并备份待修改的服务器配置文件。
2. 安装并启动 Tailscale，不修改 Nginx 和阿里云安全组。
3. 用户完成一次网页登录授权；如果授权未完成，停止部署，现有 SSH 隧道访问不受影响。
4. 启用并验证 Tailscale Serve 的 HTTPS 地址。
5. 将 Nginx 收紧到回环地址并启用安全 Cookie，再次执行完整验证。
6. 最后关闭阿里云公网网站端口；保留 SSH 恢复入口。

若安装失败、认证超时或私网 HTTPS 不可用，不进行端口收口。若收口后验证失败，恢复备份的 Nginx 和环境配置并重新加载服务，原 SSH 隧道仍可作为临时访问方式。

## 验证标准

- `tailscale status` 显示 ECS 在线且属于正确的 Tailnet。
- `tailscale serve status` 显示 HTTPS 入口仅在 Tailnet 内提供服务，Funnel 未启用。
- 已授权电脑和手机均可通过固定 `https://...ts.net` 地址打开登录页并正常登录。
- 未连接 Tailscale 的设备不能通过服务器公网 IP 的 80、443 或 8000 端口访问网站。
- 服务器上 Nginx 仅监听回环地址，应用仍仅监听 `127.0.0.1:8000`。
- 首页和 `/health` 返回成功，登录 Cookie 带有 `Secure` 属性。
- `finance-app`、Nginx、每日检查和备份定时器保持 active，近期日志没有新增错误。
- 服务器重启后 `tailscaled`、Tailscale Serve、Nginx 和应用能够自动恢复。

## 客户端使用

电脑或手机安装官方 Tailscale 客户端，使用同一账号登录并保持连接，然后收藏分配到的 HTTPS 地址即可。客户端退出登录或关闭 Tailscale 时，网站不可访问；不需要运行 SSH 命令，也不需要保持 PowerShell 窗口。
