import asyncio
import toml
import tomlkit
import os
import re
import aiohttp
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, List, Type

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)
from src.common.logger import get_logger
from src.plugin_system.apis import send_api

logger = get_logger("group_sign")

# 全局变量，用于存储插件实例
_group_sign_plugin_instance = None


# ===== 打卡定时任务管理类 =====
class SignTaskManager:
    """打卡定时任务管理器，负责处理定时打卡逻辑"""
    
    def __init__(self, plugin):
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.last_check_time = 0
        
    async def start(self):
        """启动打卡任务"""
        if self.is_running:
            logger.info("[SignTaskManager] 打卡任务已在运行中")
            return
            
        self.is_running = True
        self.task = asyncio.create_task(self._task_loop())
        logger.info("[SignTaskManager] 打卡任务已启动")
        
    async def stop(self):
        """停止打卡任务"""
        if not self.is_running:
            logger.info("[SignTaskManager] 打卡任务未在运行")
            return
            
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        logger.info("[SignTaskManager] 打卡任务已停止")
        
    async def _task_loop(self):
        """打卡任务主循环"""
        while self.is_running:
            try:
                # 获取配置
                check_interval = self.plugin.get_config("sign.check_interval", 3600)
                reminder_time = self.plugin.get_config("sign.reminder_time", "09:00")
                
                # 执行打卡检查
                await self._check_and_execute_sign()
                
                # 计算下次检查的等待时间
                sleep_time = self._calculate_sleep_time(check_interval, reminder_time)
                logger.debug(f"[SignTaskManager] 下次检查将在 {sleep_time} 秒后进行")
                await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SignTaskManager] 任务循环出错: {str(e)}", exc_info=True)
                # 出错后等待一段时间再重试
                await asyncio.sleep(60)
                
    async def _check_and_execute_sign(self):
        """检查是否到达打卡时间并执行打卡"""
        config = self.plugin.load_config()
        sign_groups = config["sign"].get("groups", [])
        reminder_time = config["sign"]["reminder_time"]
        
        if not sign_groups:
            logger.debug("[SignTaskManager] 打卡群列表为空，不执行打卡")
            return
            
        # 检查是否到达提醒时间
        now = datetime.now().strftime("%H:%M")
        today = datetime.now().date()
        
        # 记录最后检查时间（仅当天有效）
        if getattr(self, 'last_check_date', None) != today:
            self.last_check_date = today
            self.last_checked = False
            
        # 如果到达提醒时间且今天尚未检查过
        if now == reminder_time and not self.last_checked:
            logger.info(f"[SignTaskManager] 到达打卡提醒时间 {reminder_time}，开始执行打卡")
            self.last_checked = True
            
            # 对每个群执行打卡
            for group_id in sign_groups:
                try:
                    success, response = await self.plugin.send_group_sign_request(group_id)
                    if success:
                        logger.info(f"[SignTaskManager] 群{group_id}定时打卡成功")
                    else:
                        logger.error(f"[SignTaskManager] 群{group_id}定时打卡失败: {response}")
                except Exception as e:
                    logger.error(f"[SignTaskManager] 群{group_id}打卡过程出错: {str(e)}")
                
                # 避免请求过于频繁
                await asyncio.sleep(2)
                
    def _calculate_sleep_time(self, check_interval: int, reminder_time: str) -> int:
        """计算到下次检查需要等待的时间"""
        now = datetime.now()
        target_time = datetime.strptime(reminder_time, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        
        # 如果目标时间已过，则设为明天
        if now > target_time:
            target_time += timedelta(days=1)
            
        # 计算到目标时间的秒数
        time_diff = (target_time - now).total_seconds()
        
        # 取检查间隔和到目标时间的较小值
        return int(min(time_diff, check_interval))


# ===== 命令处理类 =====
class GroupSignCommand(BaseCommand):
    """群聊打卡管理命令处理类"""
    
    command_name = "groupsign"
    command_description = "群聊打卡管理命令"
    command_pattern = r"^/groupsign\s+(?P<operation_type>\w+)(?:\s+(?P<value>.+))?$"
    command_help = "使用方法: \n/groupsign add_group <群号> - 添加群聊到打卡列表\n/groupsign remove_group <群号> - 从打卡列表移除群聊\n/groupsign list_groups - 查看打卡群聊列表\n/groupsign execute <群号> - 立即执行群打卡"
    command_examples = [
        "/groupsign add_group 1046062330",
        "/groupsign remove_group 1046062330",
        "/groupsign list_groups",
        "/groupsign execute 1046062330"
    ]
    enable_command = True

    def __init__(self, message, plugin_config: dict = None):
        super().__init__(message, plugin_config)
        self.log_prefix = f"[Command:{self.command_name}]"
        self.send_api = send_api
        self.plugin_config = plugin_config or {}
        
        # 初始化 stream_id（兼容两种获取方式）
        self.stream_id = self._get_stream_id()

    def _get_stream_id(self) -> Optional[str]:
        """获取 stream_id（兼容多种路径）"""
        try:
            # 方式1：从 chat_stream 获取（优先）
            if hasattr(self.message, "chat_stream") and self.message.chat_stream:
                return self.message.chat_stream.stream_id
            # 方式2：从 message_info 获取
            elif hasattr(self.message, "message_info"):
                message_info = self.message.message_info
                return getattr(message_info, "stream_id", None)
            return None
        except Exception as e:
            logger.warning(f"{self.log_prefix} 获取 stream_id 失败: {e}")
            return None

    def _get_plugin_instance(self):
        """获取关联的插件实例"""
        global _group_sign_plugin_instance
        return _group_sign_plugin_instance

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        try:
            # 1. 获取发送者ID（用于权限检查）
            sender_id = self._get_sender_id()
            if not sender_id:
                logger.error(f"{self.log_prefix} 无法获取发送者ID，拒绝执行命令")
                await self.send_message("系统错误：无法识别发送者信息")
                return False, "无法识别发送者信息", False

            # 2. 权限检查
            if not self._check_person_permission(sender_id):
                await self.send_message("权限不足，你无权使用此命令")
                return False, "", False

            # 3. 获取操作参数
            operation_type = self.matched_groups.get("operation_type")
            value = self.matched_groups.get("value")
            
            # 4. 私聊限制检查（仅群聊可执行添加/移除/执行操作）
            isgroup = self._is_group_chat()
            if not isgroup and operation_type in ["add_group", "remove_group", "execute"]:
                await self.send_message("抱歉，添加/移除打卡群聊及执行打卡需在群聊中操作")
                return False, "", False
        
            # 5. 执行对应操作
            operation_map = {
                "add_group": lambda: self._handle_group_add(value),
                "remove_group": lambda: self._handle_group_remove(value),
                "list_groups": lambda: self._handle_group_list(),
                "execute": lambda: self._handle_execute_sign(value),
                "start_task": lambda: self._handle_start_task(),
                "stop_task": lambda: self._handle_stop_task(),
                "status": lambda: self._handle_task_status()
            }
            
            if operation_type in operation_map:
                await operation_map[operation_type]()
                return True, "", False
            else:
                await self.send_message(
                    "无效的操作参数，可用参数: 'add_group', 'remove_group', 'list_groups', 'execute', 'start_task', 'stop_task', 'status'"
                )
                logger.error(f"{self.log_prefix} 参数错误: {operation_type}")
                return False, "", False
            
        except Exception as e:
            logger.error(f"{self.log_prefix} 执行错误: {e}", exc_info=True)
            await self.send_message(f"执行失败: {str(e)}")
            return False, f"执行失败: {str(e)}", False

    # ------------------------------
    # 核心：简化消息发送（无需 reply_to，直接发送）
    # ------------------------------
    async def send_message(self, content: str):
        """直接发送文本消息，无需回复原始消息"""
        try:
            # 检查 stream_id 是否有效
            if not self.stream_id:
                logger.error(f"{self.log_prefix} stream_id 为空，无法发送消息")
                return False
            
            # 调用正确的发送方法（去掉 reply_to 参数）
            success = await self.send_api.text_to_stream(
                text=content,
                stream_id=self.stream_id,
                typing=True,  # 可选：显示输入状态
                storage_message=True  # 可选：存储消息
            )
            
            if not success:
                logger.warning(f"{self.log_prefix} 消息发送失败，内容: {content}")
            return success
        except Exception as e:
            logger.error(f"{self.log_prefix} 发送消息出错: {e}", exc_info=True)
            return False

    # ------------------------------
    # 辅助方法（封装重复逻辑）
    # ------------------------------
    def _get_sender_id(self) -> Optional[str]:
        """获取发送者ID（兼容多种路径）"""
        try:
            if not hasattr(self.message, "message_info"):
                return None
            message_info = self.message.message_info
            
            # 从 user_info 中获取用户ID
            if hasattr(message_info, "user_info"):
                user_info = message_info.user_info
                return getattr(user_info, "user_id", getattr(user_info, "id", None))
            return None
        except Exception as e:
            logger.warning(f"{self.log_prefix} 获取发送者ID失败: {e}")
            return None

    def _is_group_chat(self) -> bool:
        """判断是否为群聊"""
        try:
            if not hasattr(self.message, "message_info"):
                return False
            message_info = self.message.message_info
            
            # 检查 group_info 是否存在且有效
            if hasattr(message_info, "group_info") and message_info.group_info:
                group_info = message_info.group_info
                return bool(getattr(group_info, "group_id", getattr(group_info, "id", None)))
            return False
        except Exception as e:
            logger.warning(f"{self.log_prefix} 判断群聊失败: {e}")
            return False

    # ------------------------------
    # 操作处理方法
    # ------------------------------
    async def _handle_start_task(self) -> bool:
        plugin = self._get_plugin_instance()
        if plugin and plugin.sign_task_manager:
            await plugin.sign_task_manager.start()
            return await self.send_message("已尝试启动打卡定时任务")
        return await self.send_message("获取插件实例失败，无法启动任务")

    async def _handle_stop_task(self) -> bool:
        plugin = self._get_plugin_instance()
        if plugin and plugin.sign_task_manager:
            await plugin.sign_task_manager.stop()
            return await self.send_message("已尝试停止打卡定时任务")
        return await self.send_message("获取插件实例失败，无法停止任务")

    async def _handle_task_status(self) -> bool:
        plugin = self._get_plugin_instance()
        if plugin and plugin.sign_task_manager:
            status = "运行中" if plugin.sign_task_manager.is_running else "已停止"
            return await self.send_message(f"打卡定时任务状态: {status}")
        return await self.send_message("打卡定时任务未初始化")

    async def _handle_execute_sign(self, value: str) -> bool:
        """立即执行群打卡操作"""
        if not value:
            return await self.send_message("请指定要执行打卡的群号，格式: /groupsign execute <群号>")
        
        if not re.match(r"^[1-9]\d{4,10}$", value):
            return await self.send_message(f"{value}不是有效的群号格式")
        
        # 检查是否在打卡群列表中
        config = self._load_config()
        sign_groups = config["sign"].get("groups", [])
        if value not in sign_groups:
            return await self.send_message(f"群聊{value}不在打卡列表中，无法执行打卡")
        
        # 调用API执行打卡
        plugin = self._get_plugin_instance()
        success, response = await plugin.send_group_sign_request(value)
        
        if success:
            await self.send_message(f"已在群{value}执行打卡操作")
            logger.info(f"{self.log_prefix} 已在群{value}执行打卡操作")
            return True
        else:
            error_msg = f"打卡操作失败: {response}" if response else "打卡操作失败，未知错误"
            await self.send_message(error_msg)
            logger.error(f"{self.log_prefix} 群{value}打卡失败: {response}")
            return False

    async def _handle_group_add(self, value: str) -> bool:
        """添加群组到打卡列表"""
        if not value:
            return await self.send_message("请指定要添加的群号，格式: /groupsign add_group <群号>")
        
        if not re.match(r"^[1-9]\d{4,10}$", value):
            return await self.send_message(f"{value}不是有效的群号格式")
        
        # 执行添加
        await self.update_group_config("add", value)
        return True

    async def _handle_group_remove(self, value: str) -> bool:
        """从打卡列表移除群组"""
        if not value:
            return await self.send_message("请指定要移除的群号，格式: /groupsign remove_group <群号>")
        
        if not re.match(r"^[1-9]\d{4,10}$", value):
            return await self.send_message(f"{value}不是有效的群号格式")
        
        # 执行移除
        await self.update_group_config("remove", value)
        return True

    async def _handle_group_list(self) -> bool:
        """列出所有打卡群聊"""
        config = self._load_config()
        sign_groups = config["sign"].get("groups", [])
        
        if not sign_groups:
            return await self.send_message("打卡列表为空")
            
        result = "打卡群聊列表：\n" + "\n".join(sign_groups)
        return await self.send_message(result)

    async def update_group_config(self, action: str, group_id: str):
        """更新打卡群聊配置"""
        try:
            config_path = self._get_config_path()
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = tomlkit.load(f)
            
            # 确保配置结构存在
            if "sign" not in config_data:
                config_data["sign"] = tomlkit.table()
            if "groups" not in config_data["sign"]:
                config_data["sign"]["groups"] = tomlkit.array()
            
            groups_list = config_data["sign"]["groups"]
            
            if action == "add":
                if group_id not in groups_list:
                    groups_list.append(group_id)
                    await self.send_message(f"已将群聊{group_id}添加到打卡列表")
                    logger.info(f"{self.log_prefix} 已添加打卡群聊: {group_id}")
                else:
                    await self.send_message(f"群聊{group_id}已在打卡列表中")
                    return
                    
            elif action == "remove":
                if group_id in groups_list:
                    groups_list.remove(group_id)
                    await self.send_message(f"已将群聊{group_id}从打卡列表中移除")
                    logger.info(f"{self.log_prefix} 已移除打卡群聊: {group_id}")
                else:
                    await self.send_message(f"群聊{group_id}不在打卡列表中")
                    return
            
            # 写入更新后的配置
            with open(config_path, 'w', encoding='utf-8') as f:
                tomlkit.dump(config_data, f)
                
        except Exception as e:
            logger.error(f"{self.log_prefix} 更新配置失败: {e}", exc_info=True)
            await self.send_message(f"操作失败: {str(e)}")
            raise

    def _check_person_permission(self, user_id: str) -> bool:
        """检查用户权限"""
        config = self._load_config()
        admin_users = config["permissions"].get("admin_users", [])
        return user_id in admin_users
    
    def _get_config_path(self) -> str:
        """获取配置文件路径"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(script_dir, "config.toml")
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            config_path = self._get_config_path()
            
            if not os.path.exists(config_path):
                logger.warning(f"{self.log_prefix} 配置文件不存在: {config_path}，使用默认配置")
                return self._get_default_config()
                
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            return {
                "sign": {
                    "groups": config_data.get("sign", {}).get("groups", []),
                    "check_interval": config_data.get("sign", {}).get("check_interval", 3600),
                    "reminder_time": config_data.get("sign", {}).get("reminder_time", "09:00")
                },
                "messages": {
                    "sign_reminder": config_data.get("messages", {}).get("sign_reminder", "请大家记得打卡哦~"),
                    "sign_summary": config_data.get("messages", {}).get("sign_summary", "今日打卡情况：")
                },
                "permissions": {
                    "admin_users": config_data.get("permissions", {}).get("admin_users", [])
                },
                "api": {
                    "host": config_data.get("api", {}).get("host", "127.0.0.1"),
                    "port": config_data.get("api", {}).get("port", "4999"),
                    "token": config_data.get("api", {}).get("token", ""),
                    "timeout": config_data.get("api", {}).get("timeout", 10)
                }
            }
        except Exception as e:
            logger.error(f"{self.log_prefix} 加载配置失败: {e}", exc_info=True)
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "sign": {
                "groups": [],
                "check_interval": 3600,
                "reminder_time": "09:00"
            },
            "messages": {
                "sign_reminder": "请大家记得打卡哦~",
                "sign_summary": "今日打卡情况："
            },
            "permissions": {
                "admin_users": []
            },
            "api": {
                "host": "127.0.0.1",
                "port": "4999",
                "token": "",
                "timeout": 10
            }
        }


# ===== 插件主类 =====
@register_plugin
class GroupSignPlugin(BasePlugin):
    """群聊打卡插件
    - 采用Maizone风格的定时任务机制
    - 支持定时打卡和手动控制
    """

    plugin_name = "group_sign_plugin"
    enable_plugin = True
    config_file_name = "config.toml"

    def __init__(self, plugin_dir=None, *args, **kwargs):
        try:
            super().__init__(plugin_dir=plugin_dir, *args, **kwargs)
            self.sign_task_manager = None
            self.plugin_config = {}
            self.startup_delay = 10  # 启动延迟时间(秒)
            
            # 设置全局插件实例
            global _group_sign_plugin_instance
            _group_sign_plugin_instance = self
            
            # 加载配置
            self.load_config()
            logger.info("[Plugin:GroupSign] 插件初始化完成")
            
            # 根据配置启用插件和任务
            if self.get_config("plugin.enable", True):
                self.enable_plugin = True
                asyncio.create_task(self._start_task_after_delay())
            else:
                logger.info("[Plugin:GroupSign] 插件已禁用，不启动打卡任务")
                
        except Exception as e:
            logger.error("[Plugin:GroupSign] 插件初始化失败", exc_info=True)
            raise

    config_section_descriptions = {
        "plugin": "插件基本配置",
        "components": "组件启用控制",
        "sign": "打卡功能配置",
        "messages": "打卡相关消息配置",
        "permissions": "管理员权限配置（仅用户）",
        "api": "打卡API配置",
        "logging": "日志配置",
    }

    config_schema = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.5.0", description="配置文件版本号"),
            "enable": ConfigField(type=bool, default=True, description="是否启用插件"),
            "startup_delay": ConfigField(type=int, default=10, description="插件加载后启动定时任务的延迟时间(秒)"),
        },
        "components": {
            "enable_sign": ConfigField(type=bool, default=True, description="是否启用打卡功能"),
        },
        "sign": {
            "groups": ConfigField(type=List, default=[], description="需要进行打卡的群聊列表"),
            "check_interval": ConfigField(type=int, default=3600, description="打卡检查间隔(秒)"),
            "reminder_time": ConfigField(type=str, default="09:00", description="每日打卡提醒时间"),
        },
        "messages": {
            "sign_reminder": ConfigField(type=str, default="请大家记得打卡哦~", description="打卡提醒消息"),
            "sign_summary": ConfigField(type=str, default="今日打卡情况：", description="打卡汇总消息"),
        },
        "permissions": {
            "admin_users": ConfigField(type=List, default=[], description="允许管理打卡配置的管理员QQ号（英文逗号分隔）"),
        },
        "api": {
            "host": ConfigField(type=str, default="127.0.0.1", description="打卡API的主机地址"),
            "port": ConfigField(type=str, default="4999", description="打卡API的端口号"),
            "token": ConfigField(type=str, default="", description="API鉴权token，可选"),
            "timeout": ConfigField(type=int, default=10, description="API请求超时时间(秒)")
        },
        "logging": {
            "level": ConfigField(
                type=str, default="INFO", description="日志级别", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
            ),
            "prefix": ConfigField(type=str, default="[GroupSign]", description="日志前缀"),
        },
    }

    @property
    def dependencies(self) -> List[str]:
        """声明依赖的其他插件（插件名称列表）"""
        return []

    @property
    def python_dependencies(self) -> List[str]:
        """声明依赖的Python包（包名称列表）"""
        return []

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""
        try:
            components = []
            if self.get_config("components.enable_sign", True):
                components.append((GroupSignCommand.get_command_info(), GroupSignCommand))
                logger.debug("[Plugin:GroupSign] 已添加GroupSignCommand组件")
            else:
                logger.info("[Plugin:GroupSign] 打卡功能已禁用，未添加命令组件")
            return components
        except Exception as e:
            logger.error("[Plugin:GroupSign] 获取插件组件失败", exc_info=True)
            return []

    def _get_config_path(self) -> str:
        """获取配置文件路径"""
        return os.path.join(Path(__file__).parent, self.config_file_name)
    
    def load_config(self):
        """加载配置文件"""
        try:
            config_path = self._get_config_path()
            logger.info(f"[Plugin:GroupSign] 尝试加载配置文件: {config_path}")
            
            if not os.path.exists(config_path):
                logger.warning(f"[Plugin:GroupSign] 配置文件不存在: {config_path}，使用默认配置")
                self.plugin_config = self._get_default_config()  # 使用默认配置
                return self.plugin_config  # 返回默认配置
                
            with open(config_path, 'r', encoding='utf-8') as f:
                self.plugin_config = toml.load(f)
            
            # 从配置获取启动延迟时间
            self.startup_delay = self.plugin_config.get("plugin", {}).get("startup_delay", 10)
            logger.info(f"[Plugin:GroupSign] 配置文件加载成功，启动延迟时间: {self.startup_delay}秒")
            return self.plugin_config
        except Exception as e:
            logger.error(f"[Plugin:GroupSign] 读取配置文件失败: {e}", exc_info=True)
            self.plugin_config = self._get_default_config()
            return self.plugin_config

    def reload_config(self):
        """重新加载配置文件"""
        try:
            config_path = self._get_config_path()
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.plugin_config = toml.load(f)
                # 更新启动延迟时间
                self.startup_delay = self.plugin_config.get("plugin", {}).get("startup_delay", 10)
                logger.info(f"[Plugin:GroupSign] 配置文件重新加载成功，启动延迟时间: {self.startup_delay}秒")
        except Exception as e:
            logger.warning(f"[Plugin:GroupSign] 重新加载配置失败: {str(e)}")

    def get_config(self, key: str, default=None):
        """获取配置值"""
        keys = key.split('.')
        current = self.plugin_config
        
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
                
        return current

    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "sign": {
                "groups": [],
                "check_interval": 3600,
                "reminder_time": "09:00"
            },
            "messages": {
                "sign_reminder": "请大家记得打卡哦~",
                "sign_summary": "今日打卡情况："
            },
            "permissions": {
                "admin_users": []
            },
            "api": {
                "host": "127.0.0.1",
                "port": "4999",
                "token": "",
                "timeout": 10
            }
        }

    async def send_group_sign_request(self, group_id: str) -> Tuple[bool, Optional[str]]:
        """调用群打卡API执行打卡操作"""
        self.reload_config()
        
        try:
            api_config = self.plugin_config.get("api", {})
            api_host = api_config.get("host", "127.0.0.1")
            api_port = api_config.get("port", "3430")
            api_token = api_config.get("token", "")
            timeout = api_config.get("timeout", 10)
            
            base_url = f"http://{api_host}:{api_port}"
            params = {}
            if api_token:
                params["access_token"] = api_token
            
            payload = {
                "group_id": group_id
            }
            
            logger.info(f"[Plugin:GroupSign] 发送打卡请求到 {base_url}/set_group_sign, 群号: {group_id}")
            
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(
                        f"{base_url}/set_group_sign",
                        json=payload,
                        params=params,
                        headers={'Content-Type': 'application/json'},
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as response:
                        status_code = response.status
                        logger.debug(f"[Plugin:GroupSign] 打卡API响应状态码: {status_code}")
                        
                        response_text = await response.text()
                        logger.debug(f"[Plugin:GroupSign] 打卡API响应内容: {response_text}")
                        
                        if status_code != 200:
                            return False, f"HTTP请求失败，状态码: {status_code}"
                            
                        try:
                            result = json.loads(response_text)
                            if result.get("status") == "ok" and result.get("retcode") == 0:
                                return True, "打卡成功"
                            else:
                                error_msg = result.get("wording", result.get("message", f"未知错误（响应: {response_text}）"))
                                return False, error_msg
                        except json.JSONDecodeError:
                            return False, f"响应格式错误，非JSON: {response_text}"
                            
                except asyncio.TimeoutError:
                    return False, f"API请求超时（超过{timeout}秒）"
                except aiohttp.ClientError as e:
                    return False, f"HTTP客户端错误: {str(e)}"
                    
        except Exception as e:
            error_msg = f"API请求异常: {str(e)}"
            logger.error(f"[Plugin:GroupSign] {error_msg}", exc_info=True)
            return False, error_msg

    async def _start_task_after_delay(self):
        """延迟启动打卡任务，模仿Maizone的延迟启动机制"""
        try:
            # 检查打卡功能是否启用
            if not self.get_config("components.enable_sign", True):
                logger.info("[Plugin:GroupSign] 打卡功能已禁用，不启动打卡任务")
                return
                
            logger.info(f"[Plugin:GroupSign] 插件将在{self.startup_delay}秒后启动打卡任务...")
            await asyncio.sleep(self.startup_delay)
            
            # 初始化并启动打卡任务管理器
            self.sign_task_manager = SignTaskManager(self)
            await self.sign_task_manager.start()
            
            # 验证任务是否真的启动
            if self.sign_task_manager and self.sign_task_manager.is_running:
                logger.info("[Plugin:GroupSign] 打卡任务启动验证成功")
            else:
                logger.error("[Plugin:GroupSign] 打卡任务启动验证失败 - 任务未正常运行")
                # 尝试重新启动
                await asyncio.sleep(2)
                logger.info("[Plugin:GroupSign] 尝试重新启动打卡任务")
                await self.sign_task_manager.start()
                
        except Exception as e:
            logger.error(f"[Plugin:GroupSign] 延迟启动过程中发生错误", exc_info=True)
            # 出错时直接启动任务，不等待
            logger.info("[Plugin:GroupSign] 尝试立即启动打卡任务")
            self.sign_task_manager = SignTaskManager(self)
            await self.sign_task_manager.start()

    async def on_unload(self):
        """插件卸载时停止任务"""
        try:
            if self.sign_task_manager:
                await self.sign_task_manager.stop()
                
            global _group_sign_plugin_instance
            _group_sign_plugin_instance = None
            
            logger.info("[Plugin:GroupSign] 插件已卸载")
        except Exception as e:
            logger.error("[Plugin:GroupSign] 卸载插件时发生错误", exc_info=True)