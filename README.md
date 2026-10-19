# 微信小店客服桥接

连接微信小店客服回调、微语（Bytedesk）工作台与兼容 OpenAI 格式的 AI 对话接口。支持多店铺配置、用户与 AI 消息同步、人工接管、图片收发，以及 SQLite 持久化发送任务。

本仓库只发布通用程序与模拟测试。部署者使用自己的平台账号和凭据；仓库不附带店铺配置、客户数据、聊天记录、知识库或模型密钥。

## 工作方式

```text
微信小店 HTTPS 回调
  → FastAPI 验签 / 解密
  → SQLite 任务日志 / 异步 worker
  → 微语工作台 / AI 对话接口
  → 微信小店消息 API
```

Redis 用于令牌缓存、去重及会话映射；微语维护工作台消息。人工回复后暂停该会话的 AI；出现待回复用户消息后，180 秒无人工回复可恢复 AI，也支持 `/交回AI` 指令。

## 本地安装

需要 Python 3.10+、Redis，以及由部署者独立安装并配置的微语服务及 MySQL。微语 API 和表结构可能随版本变化，需要在测试环境核实兼容性。

```bash
git clone https://github.com/NostalgiaU/customer-service-system-public.git
cd customer-service-system-public
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在本机编辑 `.env`，填写各店铺的 `SHOPS_JSON`、Redis 地址与微语数据库连接信息。样例中的字符串均为占位符，不能直接连接真实店铺。AI 提供商的 API Key 在微语服务端独立配置，不写入本仓库。

本地测试 Redis 可绑定回环地址：

```bash
docker run -d --name wx-cs-redis -p 127.0.0.1:6379:6379 redis:7-alpine
uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

通过 HTTPS 反向代理发布 `/wxstore/{slug}/webhook`，在对应店铺配置 Token、EncodingAESKey 和安全模式。`slug` 为 `shop_user_name` 去掉 `gh_` 前缀后转小写的值。反向代理日志应去掉回调查询参数。

微语需提供 `POST /api/v1/chat/completions` 及程序使用的访客、消息和订阅接口。`weiyu_tenant_token` 的登录身份与目标客服身份需要匹配。请先使用测试店完成收发、人工接管、图片和重启恢复验证，并核对平台当前接口权限与接入规则。

## 配置

| 变量 | 用途 |
| --- | --- |
| `SHOPS_JSON` | 每家店铺的凭据、微语地址及客服标识 |
| `REDIS_URL` | 私有 Redis 连接地址 |
| `WEIYU_MYSQL_*` | 微语数据库连接参数 |
| `DELIVERY_DB_PATH` | 任务数据库，默认 `.runtime/delivery.sqlite3` |
| `WX_MEDIA_HOSTS` | 可选，额外允许的微信图片域名，逗号分隔 |
| `WEIYU_MEDIA_ORIGINS` | 可选，允许的微语媒体源，逗号分隔 |
| `IMAGE_MAX_BYTES` | 单张图片上限，最大 30 MiB |
| `LOG_LEVEL` | 日志级别，默认 `INFO` |

仅允许可信媒体域名，不将数据库、Redis 或管理后台直接暴露到公网。

## 测试与发布检查

```bash
python -m unittest discover -s tests -v
python scripts/verify_public_files.py
```

测试使用模拟 HTTP、数据库对象与临时 SQLite 文件，不需要真实店铺或模型 Key。GitHub Actions 执行相同检查。发布前还应使用 Gitleaks 等工具扫描待发布文件和 Git 历史；文件白名单检查不能替代密钥扫描。

## 数据与当前限制

- `.env`、`.runtime/`、Redis/微语数据库、图片和生产日志属于部署私有数据，不应提交、上传为 Issue 附件或打包发布。SQLite 任务内会保存消息和媒体地址。
- 运行日志可能包含店铺、用户或会话标识，异常信息也可能携带请求地址；生产日志应限制访问并脱敏，禁止公开原始日志。
- 当前代码不包含客户资料、订单、物流或售后的完整只读查询集成。
- 微语撤回不等于微信撤回；尚未发送的消息可取消，已提交或结果未知的消息需按实际状态处理。
- 消息发送受平台权限、会话时限和配额约束；本地测试通过不代表实际平台联调通过。

隐私与发布约定见 [SECURITY.md](SECURITY.md)。通用桥接源码按 [MIT License](LICENSE) 发布；微语等外部依赖遵循各自许可和服务条款。
