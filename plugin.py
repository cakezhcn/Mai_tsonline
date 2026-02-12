"""
TeamSpeak 在线人数查询插件
仅保留通过命令触发的查询实现（/ts、/ts status）。
"""
from typing import List, Tuple, Type, Any
import asyncio

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField, BaseAction, ActionActivationType,
)
from src.common.logger import get_logger

logger = get_logger("teamspeak_plugin")

# 可选的内置默认配置（如果你想把凭证写入文件，可在此处填写）
DEFAULT_PLUGIN_CONFIG = {
    "teamspeak": {
        "host": "localhost",
        "port": 10011,
        "server_id": 1,
        "username": "serveradmin",
        "password": "",
        "api_key": "",
        "show_details": True,
        # 支持字符串列表：可以是频道 ID（如"5"或5）或频道名称（精确匹配）
        "exclude_channels": [],
    }
}


def _perform_teamspeak_query(get_config_func, query_type: str = "online_count", show_user_list: bool = False) -> Tuple[bool, Any]:
    """执行 TeamSpeak ServerQuery 查询（同步实现）。
    优先使用 ts3 包；不可用时回退到直接 TCP ServerQuery。
    get_config_func: (key, default) -> value
    返回 (success, result_dict)
    """
    ts_host = get_config_func("teamspeak.host", DEFAULT_PLUGIN_CONFIG["teamspeak"]["host"])
    ts_port = get_config_func("teamspeak.port", DEFAULT_PLUGIN_CONFIG["teamspeak"]["port"])
    ts_server_id = get_config_func("teamspeak.server_id", DEFAULT_PLUGIN_CONFIG["teamspeak"]["server_id"])
    ts_username = get_config_func("teamspeak.username", DEFAULT_PLUGIN_CONFIG["teamspeak"]["username"])
    ts_password = get_config_func("teamspeak.password", DEFAULT_PLUGIN_CONFIG["teamspeak"]["password"])
    ts_api_key = get_config_func("teamspeak.api_key", DEFAULT_PLUGIN_CONFIG["teamspeak"]["api_key"])
    exclude_channels = get_config_func("teamspeak.exclude_channels", DEFAULT_PLUGIN_CONFIG["teamspeak"]["exclude_channels"]) or []
    # 规范化为字符串列表
    try:
        exclude_channels = [str(x) for x in exclude_channels]
    except Exception:
        exclude_channels = []

    if not ts_api_key and not ts_password:
        return False, {"error": "TeamSpeak 凭证未配置（请在插件配置中设置 password 或 api_key）"}

    # 尝试使用 ts3 包
    try:
        import ts3
        ts3_version = getattr(ts3, "__version__", "unknown")
        logger.info(f"检测到 ts3 库版本: {ts3_version}")

        conn = ts3.query.TS3Connection(ts_host, ts_port)
        try:
            login_password = ts_api_key if ts_api_key else ts_password
            conn.login(client_login_name=ts_username, client_login_password=login_password)
            conn.use(sid=ts_server_id)

            server_info = conn.serverinfo()[0]
            server_name = server_info.get("virtualserver_name", "未知服务器")
            max_clients = int(server_info.get("virtualserver_maxclients", "0"))

            clients = conn.clientlist()
            # 获取频道列表，构建 cid->name 映射
            channels = conn.channellist()
            cid_to_name = {ch.get("cid"): (ch.get("channel_name") or ch.get("name") or "") for ch in channels}

            # 计算要排除的频道 ID（字符串形式）
            excluded_cids = set()
            for ex in exclude_channels:
                if ex in cid_to_name.values():
                    # 名称匹配：找到其对应 cid
                    for k, v in cid_to_name.items():
                        if v == ex:
                            excluded_cids.add(str(k))
                else:
                    excluded_cids.add(str(ex))

            online_users = [c for c in clients if c.get("client_type") == "0" and str(c.get("cid")) not in excluded_cids]
            online_count = len(online_users)

            result: dict[str, Any] = {"server_name": server_name, "online_count": online_count, "max_clients": max_clients}

            if query_type == "server_status":
                uptime = int(server_info.get("virtualserver_uptime", 0))
                # 已获取 channels，计算不包含排除频道的频道数
                channel_count = len([ch for ch in channels if str(ch.get("cid")) not in excluded_cids])
                result.update({
                    "uptime_days": uptime // 86400,
                    "uptime_hours": (uptime % 86400) // 3600,
                    "version": server_info.get("virtualserver_version", "未知"),
                    "platform": server_info.get("virtualserver_platform", "未知"),
                    "channel_count": channel_count,
                })

            if show_user_list or get_config_func("teamspeak.show_details", DEFAULT_PLUGIN_CONFIG["teamspeak"]["show_details"]):
                user_names = [u.get("client_nickname", "未知") for u in online_users[:10]]
                result["online_users"] = user_names
                if len(online_users) > 10:
                    result["more_users"] = len(online_users) - 10

            conn.close()
            logger.info(f"查询成功（ts3 库）: {result}")
            return True, result
        finally:
            try:
                conn.close()
            except Exception:
                pass

    except Exception:
        # 回退到纯 TCP ServerQuery 实现
        import socket, time

        def _unescape(val: str) -> str:
            return val.replace("\\s", " ").replace("\\/", "/").replace("\\p", "|").replace("\\\\", "\\")

        def _parse_entry(line: str) -> dict:
            d = {}
            parts = line.strip().split(" ") if line else []
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    d[k] = _unescape(v)
            return d

        def _send_and_recv(sock: socket.socket, cmd: str, timeout: float = 5.0):
            sock.sendall((cmd + "\n").encode())
            data = b""
            sock.settimeout(timeout)
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\nerror id=" in data or b"\nerror id=" in data.replace(b"\r", b""):
                        break
                except socket.timeout:
                    break
            text = data.decode(errors="ignore")
            lines = [l for l in text.splitlines() if l.strip()]
            if not lines:
                return [], {"id": "-1", "msg": "no response"}
            if lines[-1].startswith("error id="):
                err = _parse_entry(lines[-1])
                entries = lines[:-1]
            else:
                err = {"id": "-1", "msg": "no error line"}
                entries = lines
            parsed = []
            for entry in entries:
                for part in entry.split("|"):
                    if part.strip():
                        parsed.append(_parse_entry(part))
            return parsed, err

        try:
            sock = socket.create_connection((ts_host, int(ts_port)), timeout=5)
            time.sleep(0.1)
            try:
                _ = sock.recv(4096).decode(errors="ignore")
            except Exception:
                pass

            login_password = ts_api_key if ts_api_key else ts_password
            parsed, err = _send_and_recv(sock, f"login {ts_username} {login_password}")
            if err.get("id") != "0":
                sock.close()
                return False, {"error": f"ServerQuery 登录失败: {err.get('msg', '')}"}

            parsed, err = _send_and_recv(sock, f"use sid={ts_server_id}")
            if err.get("id") != "0":
                sock.close()
                return False, {"error": f"use sid 失败: {err.get('msg', '')}"}

            parsed, err = _send_and_recv(sock, "serverinfo")
            if err.get("id") != "0":
                sock.close()
                return False, {"error": f"serverinfo 失败: {err.get('msg', '')}"}
            server_info = parsed[0] if parsed else {}
            server_name = server_info.get("virtualserver_name", "未知服务器")
            max_clients = int(server_info.get("virtualserver_maxclients", "0"))

            parsed_clients, err = _send_and_recv(sock, "clientlist")
            parsed_channels, _ = _send_and_recv(sock, "channellist")
            # 构建 cid->name 映射
            cid_to_name = {ch.get("cid"): (ch.get("channel_name") or ch.get("name") or "") for ch in parsed_channels}
            excluded_cids = set()
            for ex in exclude_channels:
                if ex in cid_to_name.values():
                    for k, v in cid_to_name.items():
                        if v == ex:
                            excluded_cids.add(str(k))
                else:
                    excluded_cids.add(str(ex))

            online_users = [c for c in parsed_clients if c.get("client_type") == "0" and str(c.get("cid")) not in excluded_cids]
            online_count = len(online_users)

            result: dict[str, Any] = {"server_name": server_name, "online_count": online_count, "max_clients": max_clients}

            if query_type == "server_status":
                uptime = int(server_info.get("virtualserver_uptime", 0)) if server_info.get("virtualserver_uptime") else 0
                parsed_channels, _ = _send_and_recv(sock, "channellist")
                # 计算不包含排除频道的频道数
                channel_list_filtered = [ch for ch in parsed_channels if str(ch.get("cid")) not in excluded_cids]
                result.update({
                    "uptime_days": uptime // 86400,
                    "uptime_hours": (uptime % 86400) // 3600,
                    "version": server_info.get("virtualserver_version", "未知"),
                    "platform": server_info.get("virtualserver_platform", "未知"),
                    "channel_count": len(channel_list_filtered),
                })

            if show_user_list or get_config_func("teamspeak.show_details", DEFAULT_PLUGIN_CONFIG["teamspeak"]["show_details"]):
                user_names = [u.get("client_nickname", "未知") for u in online_users[:10]]
                result["online_users"] = user_names
                if len(online_users) > 10:
                    result["more_users"] = len(online_users) - 10

            try:
                _send_and_recv(sock, "quit")
            except Exception:
                pass
            sock.close()
            logger.info(f"查询成功（TCP 回退）: {result}")
            return True, result
        except Exception as e:
            return False, {"error": f"TCP 回退查询失败: {e}"}


class TeamSpeakQueryCommand(BaseCommand):
    """TeamSpeak 命令查询（/ts）"""

    command_name = "ts_online"
    command_description = "查询 TeamSpeak 服务器在线人数"
    command_pattern = r"^/(ts|teamspeak)(\s+online)?$"

    async def execute(self) -> Tuple[bool, str, bool]:
        logger.info("通过命令触发 TS 查询")

        success, result = await asyncio.get_running_loop().run_in_executor(
            None, _perform_teamspeak_query, self.get_config, "online_count", True
        )

        if not success:
            error_msg = result.get("error", "查询失败")
            await self.send_text(f"❌ {error_msg}")
            return False, error_msg, False

        server_name = result.get("server_name", "未知")
        online_count = result.get("online_count", 0)
        max_clients = result.get("max_clients", 0)

        message = f"🎮 TeamSpeak 服务器状态\n"
        message += f"📡 服务器: {server_name}\n"
        message += f"👥 在线: {online_count}/{max_clients}\n"

        if "online_users" in result:
            message += f"\n在线用户:\n"
            for i, user in enumerate(result["online_users"], 1):
                message += f"{i}. {user}\n"
            if "more_users" in result:
                message += f"... 还有 {result['more_users']} 人\n"

        await self.send_text(message)
        return True, "查询成功", True


class TeamSpeakStatusCommand(BaseCommand):
    """TeamSpeak 状态命令（/ts status）"""

    command_name = "ts_status"
    command_description = "查询 TeamSpeak 服务器详细状态"
    command_pattern = r"^/(ts|teamspeak)\s+status$"

    async def execute(self) -> Tuple[bool, str, bool]:
        logger.info("通过命令触发 TS 状态查询")

        success, result = await asyncio.get_running_loop().run_in_executor(
            None, _perform_teamspeak_query, self.get_config, "server_status", False
        )

        if not success:
            error_msg = result.get("error", "查询失败")
            await self.send_text(f"❌ {error_msg}")
            return False, error_msg, False

        message = f"🎮 TeamSpeak 服务器详细状态\n\n"
        message += f"📡 服务器: {result.get('server_name', '未知')}\n"
        message += f"⏱️ 运行时间: {result.get('uptime_days', 0)}天 {result.get('uptime_hours', 0)}小时\n"
        message += f"📁 频道: {result.get('channel_count', 0)}个\n"
        message += f"👥 在线: {result.get('online_count', 0)}/{result.get('max_clients', 0)}\n"

        await self.send_text(message)
        return True, "查询成功", True

class TeamSpeakAction(BaseAction):
    """TeamSpeak 查询动作"""
    action_name = "teamspeakaction"
    action_description = "TeamSpeak 查询动作"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ['ts','teamspeak']
    keyword_case_sensitive = False
    """决策"""
    action_require = [
        "多次询问（5次以上）不予回复",
        "增加聊天趣味性",
        "不要连续发送多次"
    ]

    async def execute(self) -> Tuple[bool, str, bool]:
        logger.info("通过动作触发 TS 查询")

        success, result = await asyncio.get_running_loop().run_in_executor(
            None, _perform_teamspeak_query, self.get_config, "online_count", True
        )

        if not success:
            error_msg = result.get("error", "查询失败")
            await self.send_text(f"❌ {error_msg}")
            return False, error_msg, False

        server_name = result.get("server_name", "未知")
        online_count = result.get("online_count", 0)
        max_clients = result.get("max_clients", 0)

        message = f"🎮 TeamSpeak 服务器状态\n"
        message += f"📡 服务器: {server_name}\n"
        message += f"👥 在线: {online_count}/{max_clients}\n"

        if "online_users" in result:
            message += f"\n在线用户:\n"
            for i, user in enumerate(result["online_users"], 1):
                message += f"{i}. {user}\n"
            if "more_users" in result:
                message += f"... 还有 {result['more_users']} 人\n"

        await self.send_text(message)
        return True, "查询成功", True


@register_plugin
class TeamSpeakPlugin(BasePlugin):
    """TeamSpeak 插件 - 仅命令查询实现"""

    plugin_name: str = "teamspeak_plugin"
    enable_plugin: bool = False
    dependencies: List[str] = []
    python_dependencies: List[str] = ["ts3"]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本配置",
        "teamspeak": "TeamSpeak 服务器配置",
        "components": "组件启用控制",
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        },
        "teamspeak": {
            "host": ConfigField(type=str, default="localhost", description="TeamSpeak 服务器地址"),
            "port": ConfigField(type=int, default=10011, description="TeamSpeak 管理端口"),
            "server_id": ConfigField(type=int, default=1, description="虚拟服务器 ID"),
            "username": ConfigField(type=str, default="serveradmin", description="登录用户名"),
            "password": ConfigField(type=str, default="", description="登录密码"),
            "exclude_channels": ConfigField(type=list, default=[], description="要在查询结果中排除的频道列表（ID 或 名称）"),
            "show_details": ConfigField(type=bool, default=False, description="是否默认显示用户列表"),
        },
        "components": {
            "enable_tool": ConfigField(type=bool, default=True, description="是否启用查询工具"),
            "enable_commands": ConfigField(type=bool, default=True, description="是否启用命令"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """仅返回命令组件"""
        return [
            (TeamSpeakQueryCommand.get_command_info(), TeamSpeakQueryCommand),
            (TeamSpeakStatusCommand.get_command_info(), TeamSpeakStatusCommand),
            (TeamSpeakAction.get_action_info(), TeamSpeakAction),
        ]
