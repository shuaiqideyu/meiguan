# meikai

meikai 在 Telegram 里代为领取红包。一台主体跑机器人和管理页，三台节点按数据中心登录账号。私聊机器人即可使用，群里不回复。

作者 [@yuuu](https://t.me/yuuu)

官方频道 [@Meiguan](https://t.me/Meiguan)

## 机器

程序固定安装在 `/opt/meikai`，用 root 运行。四台机器克隆同一份代码，各自写自己的 `.env`。

| 角色 | SSH 主机名 | `NODE_ID` | 数据中心 |
| --- | --- | --- | --- |
| 主体 |  | 不填 | 机器人、管理页、Redis |
| 迈阿密 | `meikai-mia` | `mia` | DC1、DC3 |
| 阿姆斯特丹 | `meikai-ams` | `ams` | DC2、DC4 |
| 新加坡 | `meikai-sgp` | `sgp` | DC5 |

主体通过 SSH 调用节点，主机名必须是上表这三个。节点建议放在靠近对应数据中心的地区。

系统用 Ubuntu 24.04，时区设为 `Asia/Shanghai`。运作时间、签到和记录都按北京时间。

管理页只监听 `127.0.0.1:8780`，经 Cloudflare Tunnel 转到你自己的 HTTPS 域名。Redis 只监听 `127.0.0.1:6379`。防火墙只放行 SSH。

## 事先准备

1. 一个域名，DNS 放在 Cloudflare。
2. 在 [@BotFather](https://t.me/BotFather) 创建机器人，拿到 token。这是主体的 `BOT_TOKEN`。
3. 在 [my.telegram.org](https://my.telegram.org) 申请 `API_ID` 和 `API_HASH`。三台节点可以共用一对，也可以各用一对。
4. 查出作者账号的数字 id，填到主体的 `AUTHOR_ID`。给 [@userinfobot](https://t.me/userinfobot) 发一条消息就能看到。只有这个号能审核投稿，以及查看、移出系统屏蔽。
5. 主体的 root 能免密登录三台节点。

## 每台机器

```bash
apt update
apt install -y git python3 python3-venv python3-pip ca-certificates curl
timedatectl set-timezone Asia/Shanghai
git clone https://github.com/shuaiqideyu/meikai-public.git /opt/meikai
cd /opt/meikai
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

用 `pip install -e .`。管理页从 `/opt/meikai/web/dist` 读取，可编辑安装才对得上这个路径。

## 节点

三台都做。下面以迈阿密为例。阿姆斯特丹把 `NODE_ID` 写成 `ams`，新加坡写成 `sgp`。

安装浏览器：

```bash
cd /opt/meikai
.venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
.venv/bin/patchright install chromium
```

写入 `/opt/meikai/.env`：

```
NODE_ID=mia
API_ID=
API_HASH=
```

`API_ID` 和 `API_HASH` 用于验证码登录和扫码登录。zip 导入会用压缩包里原来的 app id。

可选变量：

- `HEADLESS=false`：浏览器默认无界面。某类网页验证打不开时再关掉。
- `VISION_API_KEY`：表情算式和网页识图。默认走阿里云百炼，模型见 `.env.example`。不填则跳过这类。
- `CAPMONSTER_KEY`：作者账号使用的 Cloudflare 打码。
- `TWOCAPTCHA_KEY`：作者账号使用的 hCaptcha 和滑块打码。

其他人的打码 token 在管理页填写。

```bash
chmod 600 /opt/meikai/.env
cp /opt/meikai/deploy/meikai-node.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now meikai-node
```

节点不填 `BOT_TOKEN`。

## 主体

安装 Redis 和构建页面用的 Node.js：

```bash
apt install -y redis-server nodejs npm
systemctl enable --now redis-server
ss -ltnp | grep 6379
```

最后一行应显示 `127.0.0.1:6379`。

构建管理页：

```bash
cd /opt/meikai/web
npm ci
npm run build
```

写入 `/opt/meikai/.env`：

```
BOT_TOKEN=
WEB_ORIGIN=https://hb.example.com
AUTHOR_ID=
REDIS_URL=redis://127.0.0.1:6379/0
WEB_PORT=8780
```

把 `WEB_ORIGIN` 换成你的地址，不要末尾斜杠。它必须和隧道里的主机名一致。`AUTHOR_ID` 填作者的数字 id。

```bash
chmod 600 /opt/meikai/.env
cp /opt/meikai/deploy/meikai.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now meikai
```

服务通过 systemd 的 `EnvironmentFile` 读取 `/opt/meikai/.env`。文件里写 `KEY=value`，不要加 `export`，也不要加引号。

### 登录节点

在主体上准备一把只给三台节点用的密钥，把公钥放进节点的 `/root/.ssh/authorized_keys`。

`/root/.ssh/config`：

```
Host meikai-mia
  HostName 迈阿密节点的地址
  User root
  IdentityFile /root/.ssh/meikai_nodes

Host meikai-ams
  HostName 阿姆斯特丹节点的地址
  User root
  IdentityFile /root/.ssh/meikai_nodes

Host meikai-sgp
  HostName 新加坡节点的地址
  User root
  IdentityFile /root/.ssh/meikai_nodes
```

```bash
chmod 600 /root/.ssh/config /root/.ssh/meikai_nodes
ssh meikai-mia hostname
ssh meikai-ams hostname
ssh meikai-sgp hostname
```

三条都通再重启主体。主体在节点上执行的是 `/opt/meikai/.venv/bin/python -m meikai.rpc_client`。

### 隧道

amd64 安装 cloudflared：

```bash
curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
dpkg -i /tmp/cloudflared.deb
```

ARM 改用 `cloudflared-linux-arm64.deb`。

```bash
cloudflared tunnel login
cloudflared tunnel create meikai
cloudflared tunnel route dns meikai hb.example.com
```

`hb.example.com` 换成你的主机名。`tunnel login` 会给出一个地址，在浏览器里打开，选中你的域名。`tunnel create` 会打印凭据文件，一般在 `/root/.cloudflared/<隧道 id>.json`。

```bash
cp /opt/meikai/deploy/cloudflared.yml.example /opt/meikai/cloudflared.yml
```

编辑 `/opt/meikai/cloudflared.yml`，填上隧道 id、凭据路径和主机名。主机名等于 `WEB_ORIGIN` 去掉 `https://` 的部分。这个文件已在 `.gitignore` 中。

```bash
chmod 600 /opt/meikai/cloudflared.yml
cp /opt/meikai/deploy/meikai-tunnel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now meikai-tunnel
```

## 使用

私聊机器人，发送 `/start`，点「打开」。左下角菜单「管理」进入同一页。主体启动时会把这个菜单指到 `WEB_ORIGIN`。用浏览器直接打开域名，页面会提示从 Telegram 进入。

右下角添加账号，可以用验证码、二维码，或每个号一份 json 的 zip。账号按数据中心落到对应节点。

红包配置有四类开关：口令红包、算式计算、网页验证码、按钮关键词。同一页可以设运作时间、延迟、名单、领取阈值和炒群。

领到后如果要私信通知，再用 @BotFather 另建一个机器人，把那个 token 填进页面。它和主体的 `BOT_TOKEN` 是两个机器人。

投稿由 `AUTHOR_ID` 对应的账号审核。

## 更新

四台都执行：

```bash
cd /opt/meikai
git pull --ff-only
.venv/bin/pip install -e .
```

主体的页面有改动时：

```bash
cd /opt/meikai/web
npm ci
npm run build
systemctl restart meikai
```

节点：

```bash
systemctl restart meikai-node
```

改了 `deploy/` 里的单元文件后，重新复制到 `/etc/systemd/system/`，再执行 `systemctl daemon-reload`。

## 日志

```bash
systemctl status meikai
journalctl -u meikai -n 80 --no-pager
journalctl -u meikai-node -n 80 --no-pager
journalctl -u meikai-tunnel -n 80 --no-pager
```

日志里出现 `缺少 BOT_TOKEN`、`缺少 WEB_ORIGIN` 或 `缺少 AUTHOR_ID`，说明主体没读到 `.env`。

页面显示「页面还没构建」时，在主体的 `web/` 里重新 `npm run build`。

页面上节点一直离线时，先在主体执行 `ssh meikai-mia`、`ssh meikai-ams`、`ssh meikai-sgp`。SSH 通了再看对应节点的日志。

## 不要放进仓库

`.env`、`cloudflared.yml`、`sessions/`、`data/`、`*.session`。会话只留在登录该账号的那台节点上。

## 许可

[MIT](LICENSE)
