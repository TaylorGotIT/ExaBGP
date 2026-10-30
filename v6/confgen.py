#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExaBGP v6 conf 生成器：按 runtime_config.json 中的分组配置渲染 exabgp.conf

用法:
    python confgen.py [config_file] [conf_file]

位置参数（均可省略，省略时用环境变量或默认值）:
    config_file   运行时配置 JSON（源/组/路由组），默认 /exabgp/data/runtime_config.json
    conf_file     输出 exabgp.conf，默认 /exabgp/data/exabgp.conf

环境变量:
    DATA_URL / TARGET_NEIGHBOR / NEXT_HOP / ROUTE_COMMUNITY / REFRESH_INTERVAL   首次引导默认源/组
    LOCAL_AS(默认65000) / PEER_AS(默认65001) / LOCAL_ADDRESS(默认NEXT_HOP) / ROUTER_ID(默认NEXT_HOP)  邻居声明默认值
    EXABGP_PROCESS_CMD(默认 /usr/local/bin/python /exabgp/http_api.py)          process 段拉起引擎的命令
    CONFIG_FILE / CONF_FILE                                                     同位置参数的别名

分组配置支持逐组覆盖字段（写入 runtime_config.json 的 groups[i]）:
    local_as / peer_as / local_address / router_id    缺省时使用环境变量默认值
"""
import json
import os
import sys
import time

PROCESS_CMD_DEFAULT = "/usr/local/bin/python /exabgp/http_api.py"


def default_paths():
    config_file = os.environ.get("CONFIG_FILE", "/exabgp/data/runtime_config.json")
    conf_file = os.environ.get("CONF_FILE", "/exabgp/data/exabgp.conf")
    return config_file, conf_file


def bootstrap_defaults(config_file):
    """首次运行（无配置文件）时，从环境变量建立默认源 + 默认组（与 http_api 引导逻辑一致）"""
    try:
        interval = int(os.environ.get("REFRESH_INTERVAL", "86400"))
    except ValueError:
        interval = 86400
    data = {
        "sources": [{
            "id": "s1",
            "name": "默认源",
            "url": os.environ.get("DATA_URL", "https://gaoyifan.github.io/china-operator-ip/china.txt"),
            "interval": max(10, interval),
            "enabled": True,
        }],
        "groups": [{
            "id": "g1",
            "name": "默认组",
            "neighbors": [os.environ.get("TARGET_NEIGHBOR", "192.168.75.241")],
            "next_hop": os.environ.get("NEXT_HOP", "192.168.75.244"),
            "community": os.environ.get("ROUTE_COMMUNITY", "65000:100"),
            "enabled": True,
        }],
        "routegroups": [],
    }
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


def load_config(config_file):
    if not os.path.exists(config_file):
        return bootstrap_defaults(config_file)
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)


def runtime_defaults():
    nh = os.environ.get("NEXT_HOP", "192.168.75.244")
    return {
        "local_as": os.environ.get("LOCAL_AS", "65000"),
        "peer_as": os.environ.get("PEER_AS", "65001"),
        "router_id": os.environ.get("ROUTER_ID", nh),
        "local_address": os.environ.get("LOCAL_ADDRESS", nh),
        "process_cmd": os.environ.get("EXABGP_PROCESS_CMD", PROCESS_CMD_DEFAULT),
    }


def neighbor_block(ip, g, runtime):
    """单个邻居声明块：优先取分组覆盖字段，缺省用环境变量默认值"""
    las = str(g.get("local_as") or runtime["local_as"])
    pas = str(g.get("peer_as") or runtime["peer_as"])
    rid = str(g.get("router_id") or runtime["router_id"])
    la = str(g.get("local_address") or runtime["local_address"])
    name = g.get("name", g["id"])
    return (
        f"neighbor {ip} {{\n"
        f"    description {name};\n"
        f"    router-id {rid};\n"
        f"    local-address {la};\n"
        f"    local-as {las};\n"
        f"    peer-as {pas};\n"
        "\n"
        "    api {\n"
        "        processes [ announce-routes ];\n"
        "        neighbor-changes;\n"
        "    }\n"
        "\n"
        "    family {\n"
        "        ipv4 unicast;\n"
        "    }\n"
        "\n"
        "    static {\n"
        "\n"
        "    }\n"
        "}"
    )


def render_conf_text(config, runtime):
    parts = []
    parts.append("# ===========================================================")
    parts.append("# ExaBGP v6 自动生成配置 —— 由 confgen.py 按分组模板渲染")
    parts.append(f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    parts.append("# 请勿手工修改：控制台/API 变更分组后会自动重生成并热重载")
    parts.append("# ===========================================================")
    parts.append("")
    parts.append(f"process announce-routes {{\n    run {runtime['process_cmd']};\n    encoder text;\n}}")
    parts.append("")
    enabled = [g for g in config.get("groups", []) if g.get("enabled", True)]
    if not enabled:
        parts.append("# ⚠️ 当前没有任何启用的分组，未生成任何邻居声明")
    for g in enabled:
        for ip in g.get("neighbors", []):
            parts.append(neighbor_block(ip, g, runtime))
    return "\n".join(parts) + "\n"


def _valid_ipv4(s):
    try:
        return __import__("ipaddress").ip_address(str(s).strip()).version == 4
    except (ValueError, TypeError):
        return False


def render_from_file(config_file=None, conf_file=None, write=True):
    """主入口：读配置 → 校验邻居/覆盖地址 → 渲染文本 →（可选）原子写盘 → 返回文本"""
    cfg_file, cnf_file = default_paths()
    config_file = config_file or cfg_file
    conf_file = conf_file or cnf_file
    config = load_config(config_file)
    # 防御：邻居 IP / 覆盖地址非法时拒绝生成，避免把坏 conf 交给 exabgp
    for g in config.get("groups", []):
        for ip in g.get("neighbors", []):
            if not _valid_ipv4(ip):
                raise ValueError(f"分组 [{g.get('name', g['id'])}] 邻居 IP 非法: {ip}（须为合法 IPv4 地址）")
        for k, label in (("next_hop", "下一跳"), ("local_address", "本地地址"), ("router_id", "Router-ID")):
            v = str(g.get(k) or "").strip()
            if v and not _valid_ipv4(v):
                raise ValueError(f"分组 [{g.get('name', g['id'])}] {label} 非法: {v}")
    text = render_conf_text(config, runtime_defaults())
    if write and conf_file:
        os.makedirs(os.path.dirname(conf_file), exist_ok=True)
        tmp = conf_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, conf_file)
    return text


def count_neighbors(config_file=None):
    cfg_file, _ = default_paths()
    config_file = config_file or cfg_file
    try:
        config = load_config(config_file)
        return sum(
            len(g.get("neighbors", []))
            for g in config.get("groups", [])
            if g.get("enabled", True)
        )
    except Exception:
        return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    c_file = args[0] if len(args) > 0 else None
    o_file = args[1] if len(args) > 1 else None
    text = render_from_file(c_file, o_file, write=True)
    out_path = o_file or default_paths()[1]
    print(f"✅ exabgp.conf 已生成: {out_path}（邻居 {count_neighbors(c_file)} 个）")
    print(text)
