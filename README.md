# freebuff

`freebuff` 是一个基于 `aiohttp` 的单文件代理服务，用来把 Freebuff / Codebuff 账号能力包装成通用 HTTP 接口，默认监听 `9090` 端口。

它不只是简单的 OpenAI Chat Completions 兼容层，目前还同时提供：

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/models`
- `/v1/reset-run`
- `/health`
- `/`

## 特性

- 支持多账号 token 池轮询
- 支持 `Chat Completions` 与 `Responses API`
- 支持流式与非流式请求
- 支持代理访问鉴权 `API_KEY`
- 支持环境变量注入 token，适合无浏览器服务器部署
- 支持从本地 `credentials.json` 加载账号
- 支持长流式输出 keep-alive，降低客户端因长时间无数据而误判断流的概率

## 默认监听地址

服务默认监听：

```text
http://0.0.0.0:9090
```

常用接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `GET /v1/models`
- `POST /v1/reset-run`
- `GET /health`

## 本地运行

```bash
pip install -r requirements.txt
python freebuff.py
```

如需启用代理访问鉴权：

```bash
API_KEY=your-key python freebuff.py
```

## 账号加载顺序

脚本按以下顺序加载账号，并自动按 `authToken` 去重：

1. 环境变量 `FREEBUFF_AUTH_TOKEN`
2. 环境变量 `FREEBUFF_AUTH_TOKENS`
3. 本地 `credentials.json`

其中：

- `FREEBUFF_AUTH_TOKEN`：单个 token
- `FREEBUFF_AUTH_TOKENS`：多个 token，支持逗号或换行分隔

示例：

```bash
FREEBUFF_AUTH_TOKEN=token1 python freebuff.py
```

```bash
FREEBUFF_AUTH_TOKENS="token1,token2,token3" python freebuff.py
```

如果你仍想交互登录，可以使用：

```bash
python freebuff.py --manage-accounts
```

但服务器部署通常不需要浏览器登录，直接用环境变量注入 token 更方便。

## 环境变量

常用环境变量：

- `PORT`：监听端口，默认 `9090`
- `API_KEY`：代理本身的访问鉴权 key
- `FREEBUFF_API_BASE`：上游域名，默认 `www.codebuff.com`
- `FREEBUFF_AUTH_TOKEN`：单个上游账号 token
- `FREEBUFF_AUTH_TOKENS`：多个上游账号 token
- `POLL_INTERVAL_S`：登录轮询间隔
- `TIMEOUT_S`：登录超时
- `UPSTREAM_CONNECT_TIMEOUT_S`：上游连接超时，默认 `30`
- `UPSTREAM_STREAM_READ_TIMEOUT_S`：上游流式读取超时，默认 `600`

如果长文本流式输出较慢，可适当增大 `UPSTREAM_STREAM_READ_TIMEOUT_S`。

## Docker 构建

```bash
docker build -t freebuff .
```

## Docker 运行

推荐直接用环境变量注入 token，这样服务器不需要浏览器，也不需要上传 `credentials.json`：

```bash
docker run -d \
  --name freebuff \
  -p 9090:9090 \
  -e API_KEY=your-key \
  -e FREEBUFF_AUTH_TOKEN=your-auth-token \
  freebuff
```

多账号：

```bash
docker run -d \
  --name freebuff \
  -p 9090:9090 \
  -e API_KEY=your-key \
  -e FREEBUFF_AUTH_TOKENS="token1,token2,token3" \
  freebuff
```

如果你仍想沿用本地凭据文件，也可以挂载：

```bash
docker run -d \
  --name freebuff \
  -p 9090:9090 \
  -e API_KEY=your-key \
  -v "$APPDATA/manicode:/root/.config/manicode:ro" \
  freebuff
```

Linux 服务器上则把挂载源目录改成实际路径，例如：

```bash
docker run -d \
  --name freebuff \
  -p 9090:9090 \
  -e API_KEY=your-key \
  -v "$HOME/.config/manicode:/root/.config/manicode:ro" \
  freebuff
```

## Docker Compose

首次使用 `docker compose` 持久化部署时，建议在项目目录下新建 `.env` 文件，而不是每次手动 `export`。

目录示例：

```text
/www/server/panel/data/compose/freebuff/.env
```

推荐模板：

```env
FREEBUFF_AUTH_TOKEN=your-auth-token
API_KEY=
FREEBUFF_AUTH_TOKENS=
```

如果你想启用代理访问鉴权，可以写成：

```env
FREEBUFF_AUTH_TOKEN=your-auth-token
API_KEY=your-proxy-api-key
FREEBUFF_AUTH_TOKENS=
```

如果需要更长的流式超时，也可以把可选参数一起写进 `.env`：

```env
FREEBUFF_AUTH_TOKEN=your-auth-token
API_KEY=
FREEBUFF_AUTH_TOKENS=
UPSTREAM_CONNECT_TIMEOUT_S=30
UPSTREAM_STREAM_READ_TIMEOUT_S=600
```

当前 `docker-compose.yml` 会自动读取同目录下的 `.env`：

```yaml
API_KEY: "${API_KEY:-}"
FREEBUFF_AUTH_TOKEN: "${FREEBUFF_AUTH_TOKEN:-}"
FREEBUFF_AUTH_TOKENS: "${FREEBUFF_AUTH_TOKENS:-}"
```

启动命令：

```bash
docker compose up -d --build
```

默认 `docker-compose.yml` 已包含：

- `9090:9090` 端口映射
- `API_KEY`、`PORT` 等环境变量
- `FREEBUFF_AUTH_TOKEN` / `FREEBUFF_AUTH_TOKENS`
- `UPSTREAM_CONNECT_TIMEOUT_S` / `UPSTREAM_STREAM_READ_TIMEOUT_S`

服务器部署时，通常只需要把 token 写进 `.env` 即可。

## 接口说明

### `POST /v1/chat/completions`

OpenAI Chat Completions 风格接口，支持：

- `model`
- `messages`
- `stream`

### `POST /v1/responses`

OpenAI Responses API 风格接口，支持：

- `input`
- `instructions`
- `stream`

### `GET /v1/models`

返回当前脚本映射出的模型列表。

### `POST /v1/reset-run`

清空当前缓存的 Agent Run。

### `GET /health`

返回服务状态、账号池数量、下一个轮询账号位置等信息。

## 当前模型映射

当前代码内置以下映射：

- `minimax/minimax-m2.7` → `base2-free`
- `z-ai/glm-5.1` → `base2-free`
- `google/gemini-2.5-flash-lite` → `file-picker`
- `google/gemini-3.1-flash-lite-preview` → `file-picker-max`
- `google/gemini-3.1-pro-preview` → `thinker-with-files-gemini`

如果后续要扩模型，直接修改 `freebuff.py` 中的 `MODEL_TO_AGENT` 即可。
