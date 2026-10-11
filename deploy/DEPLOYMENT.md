# 语音 Agent Docker 部署手册

本文面向 Linux 服务器部署当前 `sts` 项目。Docker Compose 会构建 React 前端和 FastAPI 后端，并运行一个带持久化卷的 PostgreSQL。Gateway 向量服务、统一身份中心、LLM、ASR、火山 TTS、RTC 均为外部依赖，不会被安装成项目内服务。

## 部署结构

```text
浏览器 --HTTPS--> 服务器反向代理 --HTTP--> web(Nginx 静态站点/代理)
                                             |-- /api/*、/getScenes、/proxy --> api(FastAPI)
                                             |                                  |-- PostgreSQL
                                             |                                  |-- Gateway
                                             |                                  |-- 身份中心 / LLM / ASR / 火山 TTS
                                             +-- React 静态文件
```

前后端通过同一站点来源访问，浏览器 API 地址使用相对路径；API Key 和数据库连接仅注入后端。数据库不映射到公网端口。SDK 在构建镜像时从 GitHub 获取固定提交 `b69f4b04b1874507985d831ce83467f7d0cc0025`，不会依赖开发机上的 `gateway_api` 相对路径。

## 服务器准备

准备一台能访问 Docker Hub、GitHub、Gateway、身份中心、模型服务和火山语音服务的 Linux 主机。安装 Docker Engine 与 Docker Compose v2，并确保服务器时间正确。防火墙仅开放 SSH 和 HTTPS。Compose 默认只把网页入口绑定到服务器回环地址，交由同机 TLS 反向代理访问。

将本仓库放到服务器，例如：

```bash
git clone <当前语音项目仓库地址> sts
cd sts
```

Docker 构建会访问公开的 Gateway SDK 仓库并检出上述固定提交。如果服务器不能访问 GitHub，请在有网络的构建机制作镜像并推送到镜像仓库，再在服务器拉取；或者把该 SDK 固定提交制成可访问的内部 wheel，并相应调整构建文件。不要把 SDK 依赖改为浮动分支。

## 配置密钥、场景与数据库

创建部署环境文件：

```bash
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

编辑 `deploy/.env`，至少填写：

- PostgreSQL：`POSTGRES_PASSWORD` 和 `DATABASE_URL` 中的密码必须一致。密码若包含 `@`、`:`、`/` 等 URL 保留字符，需在 `DATABASE_URL` 中进行 percent-encoding。
- 身份中心：`IDENTITY_CENTER_BASE_URL` 必须是后端容器可访问的服务端 URL，带正确的反向代理前缀。
- LLM：`LLM_API_BASE`、`LLM_API_KEY` 和可用模型名称。
- Gateway：`GATEWAY_BASE_URL`、`GATEWAY_API_KEY`、知识库集合、长期记忆集合和获准的 Embedding 模型。
- ASR/TTS：ASR Gateway WebSocket 地址及火山 TTS Key、Resource ID、已授权音色 ID。
- Cookie：生产 HTTPS 使用 `AUTH_COOKIE_SECURE=true`、`AUTH_COOKIE_SAMESITE=lax`。`CORS_ALLOW_ORIGINS` 填浏览器实际来源，例如 `https://voice.example.com`，不要带路径或尾部斜杠。
- `GATEWAY_SDK_REF` 默认是已核对的固定提交。只有在 SDK 升级并验证兼容后才更新。

不要将 `deploy/.env` 提交到 Git、放进前端构建参数或复制进镜像。

创建 RTC 场景文件：

```bash
cp python_server/scenes/Custom.json.example python_server/scenes/Custom.json
chmod 600 python_server/scenes/Custom.json
```

编辑 `python_server/scenes/Custom.json`，至少提供 RTC `AppId` 与 `AppKey`。文件通过只读卷挂载进 API 容器，不会进入镜像；不要提交真实场景凭据。

## 首次启动

在仓库根目录执行：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml config
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f api web
```

API 容器启动时先执行 `alembic upgrade head`，成功后以单进程启动 Uvicorn。服务器本机可用 `curl http://127.0.0.1:8080` 检查网页响应；如需在配置 TLS 前通过浏览器临时查看，可建立 SSH 隧道 `ssh -L 8080:127.0.0.1:8080 user@服务器地址`，再访问 `http://localhost:8080`。登录、向量检索、ASR 和 TTS 还需要相应外部服务可用。生产环境应通过已有 Nginx、Caddy 或负载均衡器配置域名和 TLS，再将流量转发到本机 `127.0.0.1:8080`。将 `.env` 中的 `CORS_ALLOW_ORIGINS` 更新为最终 HTTPS Origin。

外部 Nginx 转发示例：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name voice.example.com;
    # ssl_certificate / ssl_certificate_key 按服务器证书管理方式配置

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

语音 ASR WebSocket 与回答 SSE 都经过反向代理；外层代理必须保留 WebSocket Upgrade 并关闭响应缓冲，且设置较长读写超时。

## 更新版本

先备份数据库，再拉取目标代码版本并重建：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T db \
  pg_dump -U rtc_agent -d rtc_agent > backup-$(date +%F-%H%M%S).sql
git pull --ff-only
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail=200 api
```

每次部署启动都会先执行向前数据库迁移。若版本升级包含破坏性迁移，应先阅读对应 migration 并备份，再升级；镜像回滚不一定能逆转数据库结构，恢复需结合兼容版本和备份方案处理。

## 备份与恢复

数据库卷 `postgres_data` 保存本项目的用户项目目录、文档/切片映射和会话关联等业务元数据；知识正文与向量由 Gateway 保存。定期备份数据库 dump，并单独备份 `deploy/.env` 和 `python_server/scenes/Custom.json` 到受控密钥存储。备份文件本身可能含敏感业务信息，应加密并限制访问。

恢复到已启动的同版本 PostgreSQL：

```bash
cat backup-YYYY-MM-DD-HHMMSS.sql | docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T db \
  psql -U rtc_agent -d rtc_agent
```

不要使用 `docker compose down -v`，它会删除 PostgreSQL 持久化卷。

## 运维与限制

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f api
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f web
docker compose --env-file deploy/.env -f deploy/compose.yaml restart api
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

- Agent 会话运行态仍保存在单个 API 进程内存中；容器重启或发布会中断活跃会话，多副本/多 worker 不受支持。数据库中的持久化业务元数据及 Gateway 数据不会因此自动清空。
- API 容器不对宿主机发布端口；外部只通过 web 容器入口访问。PostgreSQL 只在 Docker 内部网络可见。
- 身份中心 opaque Cookie 在当前项目域名下由后端代理设置并验证。确保 HTTPS、Cookie `Secure` 属性和反向代理来源配置匹配；不要依赖浏览器跨域直连身份中心。
- `docker compose config` 只检查 Compose 展开结果，不会验证外部服务凭据、Gateway 模型权限或 RTC 场景的有效性。

## 常见问题

| 现象 | 检查方向 |
| --- | --- |
| 前端打不开 | `web` 容器状态、`HTTP_PORT` 占用、防火墙和外部 TLS 代理目标端口 |
| 登录提示身份中心不可用 | 在 API 容器所在网络检查 `IDENTITY_CENTER_BASE_URL` DNS、端口、路径前缀和服务可用性 |
| 启动报 `DATABASE_URL_REQUIRED` 或数据库连不上 | `.env` 已加载，URL 主机名使用 Compose 服务名 `db`，密码与 `POSTGRES_PASSWORD` 一致 |
| 场景列表为空或缺少 RTC 配置 | `Custom.json` 存在且 JSON 有效，`AppId`/`AppKey` 已填；检查 `api` 容器挂载 |
| WebSocket 403/502 或流式响应卡住 | ASR Gateway 地址/鉴权；外层 Nginx 是否转发 Upgrade、禁用 buffering 并延长超时 |
| 检索失败 | 检查 Gateway URL 前缀、Key、集合、Embedding 模型兼容性和网络；向量库是外部 Gateway，不是 Compose 内部服务 |
| TTS 失败 | 检查火山 TTS API Key、Resource ID、音色授权和服务器出网 |

排查时勿把完整 `.env`、Cookie、API Key 或用户内容贴到公开工单和日志中。
