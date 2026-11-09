# VPS PatentDown

独立部署的专利 PDF 下载 API。服务接收公开号，从 Google Patents 获取 PDF，校验文件头后返回文件。无需安装桌面客户端、浏览器或 Selenium。

运行环境：Python 3.12、Gunicorn、systemd、Nginx。推荐通过 Cloudflare 代理提供 HTTPS，应用仅监听本机地址。

## API

### 健康检查

`GET /health` 无需令牌，正常响应：

```json
{"status": "ok"}
```

### 下载 PDF

`POST /download`

请求头：

```http
Authorization: Bearer <YOUR_TOKEN>
Content-Type: application/json
```

请求体：

```json
{"patent_number": "CN111877788A"}
```

成功响应为 `application/pdf`，包含 `Content-Disposition`、`Content-Length` 和 `X-Patent-Number`。公开号不区分大小写，允许空格；仅接受字母和数字构成的公开号，不接受 URL。

Bash 调用示例：

```bash
export BASE_URL=https://patent.example.com
read -rsp 'API token: ' AUTH_TOKEN
export AUTH_TOKEN
printf '\n'
curl --fail --connect-timeout 10 --max-time 130 \
  "$BASE_URL/download" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"patent_number":"CN111877788A"}' \
  --output CN111877788A.pdf
unset AUTH_TOKEN
```

失败响应格式：

```json
{"ok": false, "error": "说明文字", "code": "invalid_request"}
```

| HTTP | code | 调用方处理 |
| --- | --- | --- |
| 400 | invalid_request | 检查 JSON 对象和公开号字符串 |
| 401 | unauthorized | 检查 Bearer 令牌 |
| 413 | invalid_request | 请求体不得超过 16 KiB |
| 429 | busy | 按 Retry-After 等待后重试；默认 5 秒 |
| 502 | not_found / no_pdf | 核对公开号，或使用其他专利数据渠道 |
| 502 | denied / blocked | 上游拒绝或限流，延迟重试，避免密集请求 |
| 502 | timeout / too_large / failed | 上游超时、文件超过限制或文件校验失败 |
| 500 | internal_error | 查看服务端日志 |

不存在的路由和不支持的方法可能由 Flask/Nginx 返回普通 404/405 页面；调用方不要假定所有错误都是 JSON。

## 下载限制

- 默认最多 2 个并发下载，槽位保留到响应关闭。
- 单篇 PDF 最大 64 MiB；HTML 页面最大 8 MiB。
- PDF 超过 2 MiB 后暂存到临时文件，响应关闭后释放。
- 当前实现保留 50 KiB 最小文件阈值；小于该值的 PDF 会被拒绝。
- 上游业务时间预算默认 100 秒，并设置连接及单次读取超时。一次在途读取可能使总耗时略超过预算。
- 推荐客户端请求超时 130 秒，并对 429 实现有限次数重试。
- 每个线程使用独立 HTTP Session；逐跳检查 HTTPS 重定向，只访问 Google Patents 和指定 PDF 存储域名。
- 不包含 PDF 缓存或异步任务队列。Google 的可用性和访问限制会影响下载结果。

## 本地测试

测试使用模拟响应，不访问 Google，也不需要真实令牌：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m unittest discover -s tests -v
```

`requirements.lock` 固定已验证环境的依赖版本；`requirements.txt` 为直接依赖清单。更新依赖后应重新运行测试。

临时启动 API 调试时，设置环境变量 `AUTH_TOKEN` 后执行 `python server.py`。生产环境使用下方 systemd/Gunicorn 部署方式。

## Ubuntu 24.04 部署

### 1. 安装程序

```bash
sudo apt update
sudo apt install -y git python3-venv nginx certbot
git clone https://github.com/vvangpc/vps-patentdown.git
cd vps-patentdown
sudo install -d -m 755 /opt/patentsdown-server
sudo install -m 644 server.py requirements.txt requirements.lock /opt/patentsdown-server/
sudo python3 -m venv /opt/patentsdown-server/venv
sudo /opt/patentsdown-server/venv/bin/pip install -r /opt/patentsdown-server/requirements.lock
id patentsdown || sudo useradd --system --user-group --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin patentsdown
```

### 2. 配置令牌和服务

```bash
sudo install -m 600 deploy/patentsdown.env.example /etc/patentsdown-server.env
sudoedit /etc/patentsdown-server.env
```

使用 `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 生成随机令牌，填入环境文件的 `AUTH_TOKEN=`。环境文件由 root 持有，权限保持 0600，不提交到 Git。程序在令牌为空或仍为旧默认占位值时拒绝启动。

```bash
sudo install -m 644 deploy/patentsdown-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now patentsdown-server
curl --fail http://127.0.0.1:8000/health
```

提供的 unit 使用专用低权限账户、只读系统、私有临时目录、256 MiB 内存上限和 4 个线程。保持 1 个 worker：并发信号量是进程内的，增加 worker 会同时增加总并发数。Gunicorn 的非必要控制套接字已关闭，服务由 systemd 管理。

### 3. 绑定域名与签发证书

以下模板使用 `patent.example.com`；安装前替换成自己的域名。

1. 在 Cloudflare 添加该主机的 A 记录指向 VPS，并开启代理。
2. 放行 HTTPS 443 和 ACME 校验使用的 HTTP 80；不要开放应用端口 8000。
3. 安装临时 HTTP 配置，完成首次证书签发：

```bash
sudo install -d -m 755 /var/www/patentsdown-acme
sed 's/patent.example.com/YOUR_DOMAIN/g' deploy/nginx-http.conf.example \
  | sudo tee /etc/nginx/sites-available/patentsdown.conf >/dev/null
sudo ln -s /etc/nginx/sites-available/patentsdown.conf /etc/nginx/sites-enabled/patentsdown.conf
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/patentsdown-acme -d YOUR_DOMAIN
```

首次使用 Certbot 时按提示完成账户和条款确认。签发期间保留 `/.well-known/acme-challenge/` 可访问；不要将该路径纳入登录或人机验证规则。

4. 安装 HTTPS 配置及 Cloudflare 来源白名单：

```bash
sudo install -m 644 deploy/cloudflare-allow.conf /etc/nginx/snippets/patentsdown-cloudflare-allow.conf
sed 's/patent.example.com/YOUR_DOMAIN/g' deploy/nginx.conf.example \
  | sudo tee /etc/nginx/sites-available/patentsdown.conf >/dev/null
sudo nginx -t
sudo systemctl reload nginx
sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
sudo install -m 750 deploy/renew-nginx.sh /etc/letsencrypt/renewal-hooks/deploy/patentsdown-nginx-reload
sudo systemctl enable --now certbot.timer
```

5. 在 Cloudflare 选择 **Full (strict)**，然后验证域名 HTTPS 和证书续期：

```bash
curl --fail https://YOUR_DOMAIN/health
sudo certbot renew --cert-name YOUR_DOMAIN --dry-run --run-deploy-hooks
```

白名单仅作用于该 HTTPS 虚拟主机，允许 Cloudflare 和本机访问。HTTP 业务请求使用 308 跳转 HTTPS，ACME 校验路径保留。不要用这份配置替换同 VPS 上其他站点。

白名单快照日期为 2026-11-09；部署和维护时对照 [Cloudflare IPv4](https://www.cloudflare.com/ips-v4) 与 [IPv6](https://www.cloudflare.com/ips-v6) 官方列表。来源网段白名单不验证具体 Cloudflare 租户，下载接口仍依赖 Bearer 鉴权。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| AUTH_TOKEN | 无 | 必填；不使用默认或示例令牌 |
| MAX_CONCURRENT_DOWNLOADS | 2 | 单 worker 并发下载数量；应小于线程数 |
| MAX_PDF_BYTES | 67108864 | 最大 PDF 字节数 |
| FETCH_BUDGET_SECONDS | 100 | 上游业务时间预算 |
| HOST / PORT | 127.0.0.1 / 8000 | 仅用于直接运行 server.py；Gunicorn 绑定地址在 unit 中指定 |

修改文件上限、并发或时间预算时，需要同步检查 systemd 内存限制、Gunicorn 线程数、Nginx 超时和 Cloudflare 限制。不要在公网使用未加密 HTTP 发送令牌。

## 运维与更新

```bash
sudo systemctl status patentsdown-server
sudo journalctl -u patentsdown-server -f
sudo nginx -t
sudo systemctl restart patentsdown-server
```

更新前备份线上源码、依赖清单和该服务的配置；测试通过后仅更新该项目文件，并保留环境文件和证书。该仓库不会自动部署到 VPS。

如果现有服务已经有 systemd drop-in，安装完整 unit 前先检查 `systemctl cat patentsdown-server`。旧 drop-in 可覆盖新 unit 的启动命令和参数。迁移配置时保留原令牌，不要直接覆盖运行中的环境文件。

仓库包含服务端源码、部署模板和离线回归测试，不包含真实密码、令牌、源站地址、私钥、日志或 PDF 文件。
