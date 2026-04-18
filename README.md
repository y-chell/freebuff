# freebuff

基于 `aiohttp` 的单文件 OpenAI 兼容代理，默认监听 `9090` 端口。

## 本地运行

```bash
pip install -r requirements.txt
python freebuff.py
```

如需启用代理访问鉴权：

```bash
API_KEY=your-key python freebuff.py
```

## 账号注入方式

脚本按以下顺序加载账号：

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

如果环境变量和 `credentials.json` 同时存在，脚本会自动去重后一起加入账号池。

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

## Docker Compose

```bash
docker compose up -d --build
```

默认 `docker-compose.yml` 已包含：

- `9090:9090` 端口映射
- `API_KEY`、`PORT` 等环境变量
- `FREEBUFF_AUTH_TOKEN` / `FREEBUFF_AUTH_TOKENS`

服务器部署时，只需要把 token 写进环境变量即可。
