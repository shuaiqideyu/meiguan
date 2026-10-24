# meiguan

meiguan 是一套自己部署的 Telegram 红包助手。私聊机器人打开管理页，用自己的账号领取红包、屏蔽窗口、查看记录，也可以在指定的群里炒群和签到。

作者 [@yuuu](https://t.me/yuuu)

官方频道 [@Meiguan](https://t.me/Meiguan)

## 产品介绍

每个人的账号、名单、偏好和余额分开保存。管理都在私聊和管理页里完成，群里不发回复。管理页要从 Telegram 里打开，用浏览器直接访问会提示回到 Telegram。

### 进入

私聊机器人，发送 `/start`，点「打开」。左下角菜单「管理」进入同一页。

### 账号

右下角添加账号，可以用验证码、二维码，或每个号一份 json 的压缩包。列表按这个号最近一次抢包、炒群或加群的时间排序，能看到头像和在线状态。每个号有抢包、加群两个开关。关掉抢包就不再领。加群打开后，看到红包才会用公开或私密链接申请加入；公开群和频道不加入也能领。

账号详情里有手机号、简介、生日、语言，以及这个号所在的节点。登录失效或账号已注销会写在状态上，并私聊通知一次。

### 红包

红包配置有四页：红包模块、窗口屏蔽、领取阈值、自动炒群。

红包模块按类开关：

- 口令红包：把口令发到群里，可以打开消息自毁。
- 算式计算：只点按钮，不往群里发字。
- 网页验证码：打开网页或小程序再领。hCaptcha 和 Cloudflare 的打码 token 在这里填写。
- 按钮关键词：按按钮上的文字点，例如「领取」「会员红包」。

每一类可以设延迟，看到之后先等再动手。运作时间按北京时间。开始和结束相同就是全天，也可以跨过零点。这段时间以外不领。

### 窗口和金额

窗口屏蔽可以关闭、只用白名单，或使用黑名单。白名单只抢名单里的窗口。黑名单里的用户、群组和频道，这个对话里的红包都不抢。黑名单里的机器人，它在任何群发出的都不抢。可以按名称、@用户名、链接或数字 id 搜索，归档里的窗口和同名窗口也会列出。

关键词命中后，可以把这个窗口屏蔽一段时间。关键词和时长都能改，重复命中只刷新计时。

领取阈值按币种设置个数和人均下限。个数太少，或人均低于下限时不抢。看不出个数或币种时可以不拦。个数大于金额的整数部分时也不抢。

### 炒群

自动炒群按窗口打开。已连接的账号在发言时段里轮流发言，时间和句子错开。还不在群里的号会先加入。文案可以开歇后语、脑筋急转弯、谐音梗、段子、土味情话、打招呼，也可以自己写，一行一句。发出的话可以自毁。

每个窗口还能单独打开签到。打开后，已连接的账号在北京时间 01:00 到 06:00 之间各发一次「签到」。过了 06:00 不补发。

### 记录、通知和投稿

领取记录按天分组。领到的写出金额，没领到的写出原因。同一个窗口里几个号领同一条红包，合成一条。点这一行可以打开原来的消息。当天按币种合计。

领到以后可以由另一个通知机器人私信。在页面里填上那个机器人的 token，再勾选哪些号只发通知、自己不点。

看到不该抢的用户、机器人、群组或频道，可以在私聊里投稿。作者确认后进入系统屏蔽：群和频道被屏蔽后不抢、不炒群、不加入。已经投过的不能再投。作者可以在私聊里查看和移出。

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
git clone https://github.com/shuaiqideyu/meiguan.git /opt/meikai
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

部署完成后，私聊机器人发送 `/start`。主体启动时会把左下角「管理」指到 `WEB_ORIGIN`。账号、红包、记录和通知都在管理页里，见上面的产品介绍。

通知机器人在页面里另填一个 token，和主体的 `BOT_TOKEN` 是两个机器人。投稿由 `AUTHOR_ID` 对应的账号审核。

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
