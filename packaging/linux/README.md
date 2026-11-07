# 原生安装（不使用 Docker）

适用：Ubuntu 20.04+ / Debian 11+，不需要 Docker，直接装成系统服务。

## 一键安装

```bash
# 1) 解压
tar xzf exabgp-injector-*-linux.tar.gz
cd exabgp-injector-*-linux

# 2) 安装（root）
sudo bash packaging/linux/install.sh
```

安装脚本会：

| 步骤 | 内容 |
|---|---|
| 1 | `apt` 装 `python3 / python3-venv / python3-pip`（可用 `SKIP_DEPS=1` 跳过） |
| 2 | 程序文件放到 `/opt/exabgp-injector` |
| 3 | 建虚拟环境并 `pip install exabgp==4.2.11 tornado`（可用 `PYPI_INDEX` 指定镜像） |
| 4 | 生成配置 `/etc/exabgp-injector/env`（首次；之后不覆盖） |
| 5 | 安装 systemd 服务 `exabgp-injector` 并启动（179 被占用时只安装不启动） |

装完打开 `http://<服务器IP>:9550/` 即是控制台。

## 目录布局

```
/opt/exabgp-injector/          程序 + venv
/var/lib/exabgp-injector/      持久化（runtime_config.json、exabgp.conf、sources/、logs/、audit/）
/run/exabgp-injector/          FIFO 与运行时通道
/etc/exabgp-injector/env       环境变量（BGP 参数、数据源、令牌等）
```

## 常用命令

```bash
systemctl status exabgp-injector      # 状态
journalctl -u exabgp-injector -f      # 实时日志
systemctl restart exabgp-injector     # 重启（会重建 BGP 会话）
sudo nano /etc/exabgp-injector/env    # 改配置后需 restart
```

## 升级

```bash
tar xzf exabgp-injector-<新版本>-linux.tar.gz && cd exabgp-injector-<新版本>-linux
sudo bash packaging/linux/install.sh      # 覆盖程序文件、保留配置与数据
```

## 卸载

```bash
sudo bash packaging/linux/uninstall.sh          # 保留配置与数据
sudo bash packaging/linux/uninstall.sh --purge  # 全部删除
```

## 与 Docker 版的区别

| | Docker | 原生 |
|---|---|---|
| 依赖 | 只需 Docker | 系统 python3 + venv |
| 网络 | `--network host` | 直接使用宿主网络 |
| 端口 | 179 / 9550 | 同 |
| 数据目录 | `/exabgp/data`（挂载卷） | `/var/lib/exabgp-injector` |
| 时区 | `-e TZ` | 读系统时区，可在 env 里设 `TZ` |

> 两者**不能同时跑**（都占 179）。切换前先 `docker stop exabgp-v6`。

## 自定义路径

```bash
sudo PREFIX=/opt/exabgp DATA_DIR=/srv/exabgp-data RUN_DIR=/run/exabgp \
     bash packaging/linux/install.sh
```

## 打包给别人的产物

```bash
bash packaging/linux/build.sh v6.3.0
# → dist/exabgp-injector-v6.3.0-linux.tar.gz
```
