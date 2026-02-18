import asyncio
import functools
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import At

try:
    from .utils.file_send_server import send_file
except ImportError:
    plugin_dir = Path(__file__).parent
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.append(plugin_dir_str)
    try:
        from utils.file_send_server import send_file  # type: ignore
    except ImportError:
        send_file = None
        logger.warning("NapCat 文件转发模块未找到，将跳过 NapCat 中转功能")


@register("grok-video", "辉宝", "Grok视频生成插件，支持根据图片和提示词生成视频，含次数限制与签到系统", "1.2.0")
class GrokVideoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # API配置
        self.server_url = config.get("server_url", "https://api.x.ai").rstrip('/')
        self.model_id = config.get("model_id", "huan-grok-imagine-1.0-video")
        self.api_key = config.get("api_key", "")
        self.enabled = config.get("enabled", True)
        
        # 请求配置
        self.timeout_seconds = config.get("timeout_seconds", 180)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)
        
        # 群组控制
        self.group_control_mode = config.get("group_control_mode", "off").lower()
        self.group_list = list(config.get("group_list", []))
        
        # 速率限制
        self.rate_limit_enabled = config.get("rate_limit_enabled", True)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 3600)
        self.rate_limit_max_calls = config.get("rate_limit_max_calls", 5)
        self._rate_limit_bucket = {}  # group_id -> {"window_start": float, "count": int}
        self._rate_limit_locks = {}  # group_id -> asyncio.Lock() 用于并发安全
        self._processing_tasks = {}  # user_id -> task_id 防止重复触发
        
        # 群聊队列限制
        self.group_task_limit = 0
        limit_raw = config.get("group_task_limit", 2)
        try:
            self.group_task_limit = max(0, int(limit_raw))
        except (TypeError, ValueError):
            self.group_task_limit = 0
            logger.warning(f"GrokVideo: group_task_limit 配置无效 ({limit_raw})，已按 0 处理")
        self.group_task_counts: Dict[str, int] = {}  # group_id -> 当前任务数
        self.queue_lock = asyncio.Lock()  # 队列操作锁
        
        # 管理员用户（优化为set提高查询效率）
        self.admin_users = set(str(u) for u in config.get("admin_users", []))

        self.nap_server_address = (config.get("nap_server_address") or "").strip()
        nap_port = config.get("nap_server_port")
        try:
            self.nap_server_port = int(nap_port)
        except (TypeError, ValueError):
            self.nap_server_port = 0

        self.save_video_enabled = config.get("save_video_enabled", False)

        # 使用 AstrBot data 目录保存视频和用户数据
        try:
            self.plugin_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_grok_video"))
            self.videos_dir = self.plugin_data_dir / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        except Exception as e:
            # 如果StarTools不可用，使用插件目录下的文件夹
            logger.warning(f"无法使用StarTools数据目录，使用插件目录: {e}")
            self.plugin_data_dir = Path(__file__).parent
            self.videos_dir = self.plugin_data_dir / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        
        # 签到系统配置
        self.enable_user_limit = config.get("enable_user_limit", True)
        self.enable_checkin = config.get("enable_checkin", True)
        self.checkin_fixed_reward = config.get("checkin_fixed_reward", 2)
        self.enable_random_checkin = str(config.get("enable_random_checkin", False)).lower() == 'true'
        self.checkin_random_reward_min = max(1, int(config.get("checkin_random_reward_min", 1)))
        self.checkin_random_reward_max = max(self.checkin_random_reward_min, int(config.get("checkin_random_reward_max", 3)))
        
        # 用户次数和签到数据存储
        self.user_counts_file = self.plugin_data_dir / "user_video_counts.json"
        self.user_checkin_file = self.plugin_data_dir / "user_video_checkin.json"
        self.user_counts: Dict[str, int] = {}
        self.user_checkin_data: Dict[str, str] = {}
        
        # 构建完整的API URL
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")
        
        logger.info(f"Grok视频生成插件已初始化，API地址: {self.api_url}")

    async def initialize(self):
        """插件初始化时加载用户数据"""
        await self._load_user_counts()
        await self._load_user_checkin_data()
        logger.info(f"Grok视频生成插件数据已加载，用户次数记录: {len(self.user_counts)} 条")

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为全局管理员"""
        try:
            admin_ids = self.context.get_config().get("admins_id", [])
            return event.get_sender_id() in admin_ids
        except Exception:
            return False

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return str(event.get_sender_id()) in self.admin_users

    # ==================== 用户次数管理方法 ====================
    
    async def _load_user_counts(self):
        """加载用户次数数据"""
        if not self.user_counts_file.exists():
            self.user_counts = {}
            return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_counts_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict):
                self.user_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户次数文件时发生错误: {e}", exc_info=True)
            self.user_counts = {}

    async def _save_user_counts(self):
        """保存用户次数数据"""
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(
                None,
                functools.partial(json.dumps, self.user_counts, ensure_ascii=False, indent=4)
            )
            await loop.run_in_executor(None, self.user_counts_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户次数文件时发生错误: {e}", exc_info=True)

    def _get_user_count(self, user_id: str) -> int:
        """获取用户剩余次数"""
        return self.user_counts.get(str(user_id), 0)

    async def _decrease_user_count(self, user_id: str):
        """扣除用户次数"""
        user_id_str = str(user_id)
        count = self._get_user_count(user_id_str)
        if count > 0:
            self.user_counts[user_id_str] = count - 1
            await self._save_user_counts()

    async def _increase_user_count(self, user_id: str, amount: int):
        """增加用户次数"""
        user_id_str = str(user_id)
        current_count = self._get_user_count(user_id_str)
        self.user_counts[user_id_str] = current_count + amount
        await self._save_user_counts()

    # ==================== 签到数据管理方法 ====================
    
    async def _load_user_checkin_data(self):
        """加载用户签到数据"""
        if not self.user_checkin_file.exists():
            self.user_checkin_data = {}
            return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_checkin_file.read_text, "utf-8")
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict):
                self.user_checkin_data = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"加载用户签到文件时发生错误: {e}", exc_info=True)
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        """保存用户签到数据"""
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(
                None,
                functools.partial(json.dumps, self.user_checkin_data, ensure_ascii=False, indent=4)
            )
            await loop.run_in_executor(None, self.user_checkin_file.write_text, json_data, "utf-8")
        except Exception as e:
            logger.error(f"保存用户签到文件时发生错误: {e}", exc_info=True)

    def _get_callback_api_base(self) -> Optional[str]:
        """读取 AstrBot 全局 callback_api_base 配置"""
        try:
            config = self.context.get_config()
            if isinstance(config, dict):
                return config.get("callback_api_base")
        except Exception as e:
            logger.debug(f"读取 callback_api_base 失败: {e}")
        return None

    # ==================== 群聊队列限制方法 ====================
    
    async def _acquire_group_slot(self, group_id: Optional[str]) -> bool:
        """尝试获取群聊任务槽位
        
        Args:
            group_id: 群组ID，私聊时为None
            
        Returns:
            True 表示成功获取槽位，False 表示已达上限
        """
        # 私聊或未设置限制时直接通过
        if not group_id or self.group_task_limit <= 0:
            return True
        
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current >= self.group_task_limit:
                return False
            self.group_task_counts[group_id] = current + 1
            logger.debug(f"[GrokVideo] 群 {group_id} 任务占用 {self.group_task_counts[group_id]}/{self.group_task_limit}")
            return True

    async def _release_group_slot(self, group_id: Optional[str]):
        """释放群聊任务槽位
        
        Args:
            group_id: 群组ID，私聊时为None
        """
        if not group_id or self.group_task_limit <= 0:
            return
        
        async with self.queue_lock:
            current = self.group_task_counts.get(group_id, 0)
            if current <= 1:
                self.group_task_counts.pop(group_id, None)
            else:
                self.group_task_counts[group_id] = current - 1
            logger.debug(f"[GrokVideo] 群 {group_id} 任务释放，当前 {self.group_task_counts.get(group_id, 0)}")

    async def _check_group_access(self, event: AstrMessageEvent) -> Optional[str]:
        """检查群组访问权限和速率限制（并发安全）"""
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                group_id = None

            # 群组白名单/黑名单检查
            if group_id:
                if self.group_control_mode == "whitelist" and group_id not in self.group_list:
                    return "当前群组未被授权使用视频生成功能"
                if self.group_control_mode == "blacklist" and group_id in self.group_list:
                    return "当前群组已被限制使用视频生成功能"

                # 速率限制检查（仅对群组）- 使用异步锁确保并发安全
                if self.rate_limit_enabled:
                    # 获取或创建该群组的锁
                    if group_id not in self._rate_limit_locks:
                        self._rate_limit_locks[group_id] = asyncio.Lock()
                    
                    # 正确使用异步锁保护临界区
                    async with self._rate_limit_locks[group_id]:
                        now = time.time()
                        bucket = self._rate_limit_bucket.get(group_id, {"window_start": now, "count": 0})
                        window_start = bucket.get("window_start", now)
                        count = int(bucket.get("count", 0))
                        
                        # 检查是否需要重置窗口
                        if now - window_start >= self.rate_limit_window_seconds:
                            window_start = now
                            count = 0
                        
                        # 检查是否超过限制
                        if count >= self.rate_limit_max_calls:
                            return f"本群调用已达上限（{self.rate_limit_max_calls}次/{self.rate_limit_window_seconds}秒），请稍后再试"
                        
                        # 原子性更新计数器
                        bucket["window_start"], bucket["count"] = window_start, count + 1
                        self._rate_limit_bucket[group_id] = bucket

        except Exception as e:
            logger.error(f"群组访问检查失败: {e}")
            return None
        
        return None

    async def _extract_images_from_message(self, event: AstrMessageEvent) -> List[str]:
        """从消息中提取图片的base64数据"""
        images = []
        
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        if base64_data:
                            # 确保是完整的 data URL 格式
                            if not base64_data.startswith('data:'):
                                base64_data = f"data:image/jpeg;base64,{base64_data}"
                            images.append(base64_data)
                    except Exception as e:
                        logger.warning(f"图片转base64失败: {e}")
                elif isinstance(comp, Reply) and comp.chain:
                    # 检查引用消息中的图片
                    for reply_comp in comp.chain:
                        if isinstance(reply_comp, Image):
                            try:
                                base64_data = await reply_comp.convert_to_base64()
                                if base64_data:
                                    # 确保是完整的 data URL 格式
                                    if not base64_data.startswith('data:'):
                                        base64_data = f"data:image/jpeg;base64,{base64_data}"
                                    images.append(base64_data)
                            except Exception as e:
                                logger.warning(f"引用图片转base64失败: {e}")
        
        return images

    async def _call_grok_api(self, prompt: str, image_base64: str) -> Tuple[Optional[str], Optional[str]]:
        """调用Grok API生成视频"""
        if not self.api_key:
            return None, "未配置API密钥"
        
        # 构建请求数据
        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_base64
                            }
                        }
                    ]
                }
            ]
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        timeout_config = httpx.Timeout(
            connect=10.0,
            read=self.timeout_seconds,
            write=10.0,
            pool=self.timeout_seconds + 10
        )
        
        for attempt in range(self.max_retry_attempts):
            try:
                logger.info(f"调用Grok API (尝试 {attempt + 1}/{self.max_retry_attempts})")
                logger.debug(f"请求URL: {self.api_url}")
                logger.debug(f"请求模型: {self.model_id}")
                
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await client.post(
                        self.api_url,
                        json=payload,
                        headers=headers
                    )
                    
                    logger.info(f"API响应状态码: {response.status_code}")
                    
                    # 记录响应内容用于调试
                    response_text = response.text
                    logger.debug(f"API响应内容: {response_text[:500]}...")
                    
                    if response.status_code == 200:
                        full_content = ""
                        try:
                            # 1. 优先尝试标准 JSON 解析
                            result = response.json()
                            logger.debug(f"解析的标准 JSON 响应: {result}")
                            video_url, parse_error = self._extract_video_url_from_response(result)
                            if video_url:
                                return video_url, None
                        except json.JSONDecodeError:
                            # 2. 尝试解析 SSE 流式响应
                            logger.debug("尝试从 SSE 响应流中拼接内容...")
                            lines = response_text.split('\n')
                            for line in lines:
                                line = line.strip()
                                if line.startswith("data: "):
                                    content_str = line[6:].strip()
                                    if content_str == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(content_str)
                                        if "choices" in chunk and chunk["choices"]:
                                            delta = chunk["choices"][0].get("delta", {})
                                            if "content" in delta:
                                                full_content += delta["content"]
                                    except:
                                        continue
                        
                        # 3. 如果拿到了拼接内容，进行提取
                        if full_content:
                            logger.debug(f"SSE 拼接内容提取结果: {full_content}")
                            video_url = self._try_content_extraction(full_content)
                            if video_url:
                                logger.info(f"成功从 SSE 流中提取到 URL: {video_url}")
                                return video_url, None
                        
                        # 4. 最后一道防线：对整个原始文本进行暴力正则提取
                        video_url = self._extract_direct_url(response_text)
                        if video_url:
                            logger.info(f"通过直接提取 URL 的方式获取成功: {video_url}")
                            return video_url, None
                            
                        return None, "API响应中未包含有效的视频URL"
                    
                    elif response.status_code == 403:
                        return None, "API访问被拒绝，请检查密钥和权限"
                    
                    else:
                        error_msg = f"API请求失败 (状态码: {response.status_code})"
                        try:
                            error_detail = response.json()
                            logger.debug(f"错误详情JSON: {error_detail}")
                            if "error" in error_detail:
                                error_msg += f": {error_detail['error']}"
                            elif "message" in error_detail:
                                error_msg += f": {error_detail['message']}"
                            else:
                                error_msg += f": {error_detail}"
                        except:
                            error_msg += f": {response_text[:200]}"
                        
                        if attempt == self.max_retry_attempts - 1:
                            return None, error_msg
                        
                        logger.warning(f"{error_msg}，等待重试...")
                        await asyncio.sleep(2)  # 增加重试间隔
            
            except httpx.TimeoutException:
                error_msg = f"请求超时 ({self.timeout_seconds}秒)"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
            
            except Exception as e:
                error_msg = f"请求异常: {str(e)}"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
        
        return None, "所有重试均失败"

    def _extract_video_url_from_response(self, response_data: dict) -> Tuple[Optional[str], Optional[str]]:
        """
        从 API 响应中提取视频 URL，采用更健墮的解析策略
        
        返回: (video_url, error_message)
        """
        try:
            # 1. 首先检查响应结构是否符合预期
            if not isinstance(response_data, dict):
                return None, f"无效的响应格式: {type(response_data)}"
            
            if "choices" not in response_data or not response_data["choices"]:
                return None, "API响应中缺少 choices 字段"
            
            # 2. 提取内容
            choice = response_data["choices"][0]
            if not isinstance(choice, dict) or "message" not in choice:
                return None, "choices[0] 缺少 message 字段"
            
            message = choice["message"]
            if not isinstance(message, dict) or "content" not in message:
                return None, "message 缺少 content 字段"
            
            content = message["content"]
            if not isinstance(content, str):
                return None, f"content 不是字符串类型: {type(content)}"
            
            logger.debug(f"API返回内容长度: {len(content)} 字符")
            
            # 3. 优先尝试结构化解析（如果 API 支持）
            video_url = self._try_structured_extraction(response_data)
            if video_url:
                return video_url, None
            
            # 4. 如果结构化解析失败，使用改进的文本解析
            video_url = self._try_content_extraction(content)
            if video_url:
                return video_url, None
            
            # 5. 所有方法都失败
            logger.warning(f"无法从响应中提取视频URL，内容片段: {content[:200]}...")
            return None, f"未能从 API 响应中提取到有效的视频 URL"
            
        except Exception as e:
            logger.error(f"URL 提取过程中发生异常: {e}")
            return None, f"URL 提取失败: {str(e)}"
    
    def _try_structured_extraction(self, response_data: dict) -> Optional[str]:
        """
        尝试从结构化数据中提取 URL（为未来 API 改进做准备）
        """
        try:
            # 检查是否有直接的 video_url 字段
            if "video_url" in response_data:
                url = response_data["video_url"]
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    logger.info("使用结构化 video_url 字段")
                    return url
            
            # 检查 choices[0].message 中是否有结构化数据
            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            # 检查是否有 attachments 或 media 字段
            for field in ["attachments", "media", "files"]:
                if field in message and isinstance(message[field], list):
                    for item in message[field]:
                        if isinstance(item, dict) and "url" in item:
                            url = item["url"]
                            if isinstance(url, str) and url.endswith(".mp4"):
                                logger.info(f"使用结构化 {field} 字段")
                                return url
            
            return None
            
        except Exception as e:
            logger.debug(f"结构化提取失败: {e}")
            return None
    
    def _try_content_extraction(self, content: str) -> Optional[str]:
        """
        从文本内容中提取 URL，使用改进的策略
        """
        try:
            # 策略 1: 查找最常见的 HTML video 标签
            video_url = self._extract_from_html_tag(content)
            if video_url:
                return video_url
            
            # 策略 2: 查找直接的 .mp4 URL
            video_url = self._extract_direct_url(content)
            if video_url:
                return video_url
            
            # 策略 3: 查找 Markdown 格式链接
            video_url = self._extract_from_markdown(content)
            if video_url:
                return video_url
            
            return None
            
        except Exception as e:
            logger.debug(f"内容提取失败: {e}")
            return None
    
    def _extract_from_html_tag(self, content: str) -> Optional[str]:
        """从 HTML video 标签中提取 URL"""
        if "<video" not in content or "src=" not in content:
            return None
        
        # 更宽松的正则，支持多种引号和空格
        patterns = [
            r'<video[^>]*src=["\']([^"\'>]+)["\'][^>]*>',  # 标准 video 标签
            r'src=["\']([^"\'>]+\.mp4[^"\'>]*)["\']',      # 任意 src 属性
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                if self._is_valid_video_url(url):
                    logger.debug(f"从 HTML 标签提取到 URL: {url}")
                    return url
        
        return None
    
    def _extract_direct_url(self, content: str) -> Optional[str]:
        """提取直接的 .mp4 URL"""
        # 更精确的 URL 正则，避免误匹配
        pattern = r'(https?://[^\s<>"\')\]\}]+\.mp4(?:\?[^\s<>"\')\]\}]*)?)'
        
        matches = re.findall(pattern, content, re.IGNORECASE)
        for url in matches:
            if self._is_valid_video_url(url):
                logger.debug(f"提取到直接 URL: {url}")
                return url
        
        return None
    
    def _extract_from_markdown(self, content: str) -> Optional[str]:
        """从 Markdown 链接中提取 URL"""
        # Markdown 格式: [text](url) 或 ![alt](url)
        patterns = [
            r'!?\[[^\]]*\]\(([^\)]+\.mp4[^\)]*)\)',  # Markdown 链接
            r'!?\[[^\]]*\]:\s*([^\s]+\.mp4[^\s]*)',   # Markdown 引用式链接
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                if self._is_valid_video_url(url):
                    logger.debug(f"从 Markdown 提取到 URL: {url}")
                    return url
        
        return None
    
    def _is_valid_video_url(self, url: str) -> bool:
        """验证 URL 是否为有效的视频 URL"""
        if not isinstance(url, str) or len(url) < 10:
            return False
        
        # 检查协议
        if not url.startswith(("http://", "https://")):
            return False
        
        # 检查文件扩展名
        if not url.lower().endswith(".mp4") and ".mp4" not in url.lower():
            return False
        
        # 检查是否包含明显的非法字符
        invalid_chars = ['<', '>', '"', "'", '\n', '\r', '\t']
        if any(char in url for char in invalid_chars):
            return False
        
        return True

    async def _download_video(self, video_url: str) -> Optional[str]:
        """下载视频到本地"""
        try:
            filename = f"grok_video_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.mp4"
            file_path = self.videos_dir / filename
            
            timeout_config = httpx.Timeout(
                connect=10.0,
                read=300.0,  # 视频文件可能较大，给更长的读取时间
                write=10.0,
                pool=300.0
            )
            
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(video_url)
                response.raise_for_status()
                
                # 保存视频文件
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                # 确保返回绝对路径，避免路径问题
                absolute_path = file_path.resolve()
                logger.info(f"视频已保存到: {absolute_path}")
                return str(absolute_path)
        
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return None

    async def _prepare_video_path(self, video_path: str) -> str:
        """Optionally bridge the video file through NapCat so the client can access it."""
        if not video_path:
            return video_path
        if not (self.nap_server_address and self.nap_server_port):
            return video_path
        if send_file is None:
            logger.debug("NapCat 文件转发模块不可用，直接返回本地路径")
            return video_path
        try:
            forwarded_path = await send_file(video_path, self.nap_server_address, self.nap_server_port)
            if forwarded_path:
                logger.info(f"NapCat file server returned video path: {forwarded_path}")
                return forwarded_path
            logger.warning("NapCat file server did not return a valid video path, falling back to local file")
        except Exception as e:
            logger.warning(f"NapCat file server transfer failed, falling back to local file: {e}")
        return video_path

    async def _cleanup_video_file(self, video_path: Optional[str]):
        """删除临时视频缓存（按照配置可选）"""
        if not video_path:
            return
        if self.save_video_enabled:
            return
        try:
            path = Path(video_path)
            if path.exists():
                path.unlink()
                logger.debug(f"已清理本地视频缓存: {path}")
        except Exception as e:
            logger.warning(f"清理视频文件失败: {e}")

    async def _create_video_component(self, video_path: Optional[str], video_url: Optional[str]):
        """根据配置构建最终 Video 组件，优先使用URL发送（适合Docker部署）"""
        from astrbot.api.message_components import Video

        # Docker部署下优先使用远程URL（避免文件系统共享问题）
        if video_url:
            logger.info(f"使用远程视频URL发送: {video_url}")
            return Video.fromURL(video_url)
        
        # 如果没有远程URL，且用户配置了保存，尝试本地文件
        if video_path and self.save_video_enabled:
            logger.warning(f"Docker部署下使用本地文件可能失败: {video_path}")
            return Video.fromFileSystem(path=video_path)

        raise ValueError("缺少可用的视频URL，无法发送")

    async def _generate_video_core(self, event: AstrMessageEvent, prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """核心视频生成逻辑"""
        # 检查功能是否启用
        if not self.enabled:
            return None, None, "视频生成功能已禁用"
        
        # 提取图片
        images = await self._extract_images_from_message(event)
        if not images:
            return None, None, "未找到图片，请在消息中包含图片或引用包含图片的消息"
        
        # 使用第一张图片
        image_base64 = images[0]
        
        # 调用API生成视频
        video_url, error_msg = await self._call_grok_api(prompt, image_base64)
        if error_msg:
            return None, None, error_msg

        if not video_url:
            return None, None, "API未返回视频URL"

        # Docker部署下优先使用URL，不下载本地文件避免文件系统问题
        local_path = None
        if self.save_video_enabled:
            logger.info("用户配置了保存，但Docker部署下建议使用URL发送")
            # 可选下载，但不强制
            try:
                local_path = await self._download_video(video_url)
                if local_path:
                    logger.info(f"视频已下载到: {local_path}")
            except Exception as e:
                logger.warning(f"视频下载失败，将使用URL发送: {e}")

        return video_url, local_path, None

    async def _async_generate_video(self, event: AstrMessageEvent, prompt: str, task_id: str, remaining_count: int = -1, group_id: Optional[str] = None):
        """异步视频生成，避免超时和重复触发
        
        Args:
            event: 消息事件
            prompt: 提示词
            task_id: 任务ID
            remaining_count: 用户剩余次数（-1表示管理员或未开启限制，不显示次数）
            group_id: 群组ID，用于释放队列槽位
        """
        user_id = str(event.get_sender_id())
        
        # 构建剩余次数提示
        def _count_tip() -> str:
            if remaining_count >= 0:
                return f"\n🎬 剩余视频生成次数: {remaining_count}"
            return ""
        
        try:
            logger.info(f"开始处理用户 {user_id} 的视频生成任务: {task_id}")
            
            video_url, video_path, error_msg = await self._generate_video_core(event, prompt)
            
            if error_msg:
                # 失败时也显示剩余次数
                await event.send(event.plain_result(f"❌ {error_msg}{_count_tip()}"))
                return
            
            if video_url or video_path:
                try:
                    # 发送进度更新
                    await event.send(event.plain_result("📤 正在发送视频，请稍候..."))
                    
                    video_component = await self._create_video_component(video_path, video_url)
                    
                    # 使用更长的超时时间，但提供更好的反馈
                    try:
                        await asyncio.wait_for(
                            event.send(event.chain_result([video_component])),
                            timeout=90.0  # 增加到90秒超时
                        )
                        logger.info(f"用户 {user_id} 的视频发送成功")
                        await event.send(event.plain_result(f"✅ 视频发送成功！{_count_tip()}"))
                        
                    except asyncio.TimeoutError:
                        logger.warning(f"用户 {user_id} 的视频发送超时，但可能仍在传输")
                        await event.send(event.plain_result(
                            f"⚠️ 视频发送超时，但可能仍在传输中。\n"
                            f"如果稍后收到视频，说明发送成功。{_count_tip()}"
                        ))
                    
                    # 清理文件（如果配置允许）
                    if video_path:
                        await self._cleanup_video_file(video_path)
                        
                except Exception as e:
                    # 区分WebSocket超时和真正的错误
                    if "WebSocket API call timeout" in str(e):
                        logger.warning(f"用户 {user_id} 的视频发送WebSocket超时: {e}")
                        await event.send(event.plain_result(
                            f"⚠️ 视频发送超时，但可能仍在传输中。\n"
                            f"如果稍后收到视频，说明发送成功。{_count_tip()}"
                        ))
                    else:
                        logger.error(f"用户 {user_id} 的视频发送真正失败: {e}")
                        await event.send(event.plain_result(f"❌ 视频发送失败: {str(e)}{_count_tip()}"))
            else:
                await event.send(event.plain_result(f"❌ 视频生成失败，请稍后再试{_count_tip()}"))
        
        except Exception as e:
            logger.error(f"用户 {user_id} 的异步视频生成异常: {e}")
            await event.send(event.plain_result(f"❌ 视频生成时遇到问题: {str(e)}"))
        
        finally:
            # 释放群聊队列槽位
            await self._release_group_slot(group_id)
            # 清理任务记录
            if user_id in self._processing_tasks and self._processing_tasks[user_id] == task_id:
                del self._processing_tasks[user_id]
                logger.info(f"用户 {user_id} 的任务 {task_id} 已完成")

    @filter.llm_tool(name="generate_video_with_grok")
    async def generate_video_with_grok(self, event: AstrMessageEvent, **kwargs) -> str:
        """🎬 使用 Grok 生成视频
        
        根据用户提供的图片和提示词，使用 Grok AI 生成一段视频。
        需要用户在消息中包含图片或引用包含图片的消息。
        
        【使用场景】
        - 用户说"帮我把这张图变成视频" → 需要用户提供图片
        - 用户说"让图片里的角色动起来" → 需要用户提供图片
        - 用户说"给这张图加上下雨效果" → 需要用户提供图片和效果描述
        
        【注意事项】
        - 必须有图片才能生成视频
        - 视频生成需要较长时间（通常几分钟）
        - 如果用户没有提供图片，请提示用户先发送图片
        
        Args:
            prompt(string): 视频生成的提示词，描述希望视频呈现的效果，如"让角色跳舞"、"添加下雨效果"等
            reply_image_url(string): 用户引用的图片URL（可选，如果消息中已包含图片则不需要）
        
        Returns:
            JSON 格式的执行结果，包含 success 和 message 字段
        """
        import json
        
        prompt = kwargs.get('prompt', '').strip()
        reply_image_url = kwargs.get('reply_image_url', '').strip()
        
        logger.info(f"LLM请求生成视频 | 提示词: {prompt} | 引用图片: {reply_image_url}")
        
        # 检查功能是否启用
        if not self.enabled:
            return json.dumps({
                "success": False,
                "error": "视频生成功能已禁用"
            }, ensure_ascii=False)
        
        # 检查是否有提示词
        if not prompt:
            return json.dumps({
                "success": False,
                "error": "请提供视频生成的提示词，描述你希望视频呈现的效果"
            }, ensure_ascii=False)
        
        # 如果有 reply_image_url，尝试将其添加到消息组件中
        if reply_image_url:
            try:
                # 构建一个包含图片的 Reply 组件
                from astrbot.core.message.components import Reply, Image as ImageComp
                try:
                    img_comp = ImageComp.fromURL(reply_image_url)
                except (AttributeError, TypeError):
                    img_comp = ImageComp(file=reply_image_url)
                
                reply_chain = [img_comp]
                reply_comp = Reply(id=0, sender_id=0, chain=reply_chain)
                
                # 添加到消息对象中
                if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message'):
                    # 在消息开头插入 Reply 组件
                    event.message_obj.message.insert(0, reply_comp)
                    logger.debug(f"已将引用图片添加到消息组件中")
            except Exception as e:
                logger.warning(f"添加引用图片失败: {e}")
        
        # 检查是否有图片
        images = await self._extract_images_from_message(event)
        if not images:
            return json.dumps({
                "success": False,
                "error": "未找到图片，请让用户先发送一张图片，然后再请求生成视频"
            }, ensure_ascii=False)
        
        # 检查群组访问权限
        access_error = await self._check_group_access(event)
        if access_error:
            return json.dumps({
                "success": False,
                "error": access_error
            }, ensure_ascii=False)
        
        # 防止重复触发
        user_id = str(event.get_sender_id())
        if user_id in self._processing_tasks:
            return json.dumps({
                "success": False,
                "error": "该用户已有一个视频生成任务在进行中，请等待完成后再试"
            }, ensure_ascii=False)
        
        # 次数检查（管理员跳过）- LLM调用也需要扣除次数
        is_admin = self._is_global_admin(event) or self._is_admin(event)
        remaining_count = -1  # -1 表示管理员或未开启限制，不显示次数
        
        if not is_admin and self.enable_user_limit:
            user_count = self._get_user_count(user_id)
            if user_count <= 0:
                return json.dumps({
                    "success": False,
                    "error": "用户的视频生成次数已用完，请使用「辉宝赐福」获取免费次数"
                }, ensure_ascii=False)
            # 立即扣除次数（不管成功与否）
            await self._decrease_user_count(user_id)
            remaining_count = self._get_user_count(user_id)
        
        # 获取群组ID用于队列限制
        group_id = None
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        
        # 群聊队列限制检查（LLM调用也需要检查）
        if not await self._acquire_group_slot(group_id):
            # 如果已扣除次数但获取槽位失败，需要退还次数
            if remaining_count >= 0:
                await self._increase_user_count(user_id, 1)
            return json.dumps({
                "success": False,
                "error": f"当前群聊已有 {self.group_task_limit} 个视频生成任务在进行中，请稍后再试"
            }, ensure_ascii=False)
        
        try:
            # 生成任务ID
            task_id = str(uuid.uuid4())[:8]
            self._processing_tasks[user_id] = task_id
            
            # 构建提示信息
            tip_parts = [
                f"🎥 正在使用 Grok 为您生成视频，请稍候（预计需要几分钟）...",
                f"🆔 任务ID: {task_id}",
                f"📝 提示词: {prompt}"
            ]
            if remaining_count >= 0:
                tip_parts.append(f"🎬 剩余视频生成次数: {remaining_count}")
            
            # 发送开始提示
            await event.send(event.plain_result("\n".join(tip_parts)))
            
            # 启动异步任务，传入剩余次数和群组ID
            asyncio.create_task(self._async_generate_video(event, prompt, task_id, remaining_count, group_id))
            
            return json.dumps({
                "success": True,
                "message": f"视频生成任务已启动，任务ID: {task_id}，请等待几分钟",
                "task_id": task_id
            }, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"启动视频生成任务失败: {e}")
            return json.dumps({
                "success": False,
                "error": f"启动视频生成任务失败: {str(e)}"
            }, ensure_ascii=False)

    @filter.command("视频")
    async def cmd_generate_video(self, event: AstrMessageEvent):
        """生成视频：/视频 <提示词>（需要包含图片）"""
        # 完全从原始消息中提取提示词，不依赖框架的参数解析
        # 这样可以兼容 LLM Executor 的调用方式
        raw_text = event.message_str if hasattr(event, 'message_str') else ""
        prompt = ""
        if raw_text:
            # 使用正则去除命令前缀（支持 /视频 或 视频），保留后面的所有内容
            # re.sub(pattern, repl, string, count=1) 只替换第一次出现的命令
            extracted_prompt = re.sub(r'^/?视频\s*', '', raw_text, count=1).strip()
            if extracted_prompt:
                prompt = extracted_prompt
        
        # 群组访问检查
        access_error = await self._check_group_access(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        
        # 获取群组ID用于队列限制
        group_id = None
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        
        # 防止重复触发检查（在获取槽位之前检查，避免占用槽位）
        user_id = str(event.get_sender_id())
        if user_id in self._processing_tasks:
            yield event.plain_result(f"⚠️ 您已有一个视频生成任务在进行中，请等待完成后再试。")
            return
        
        # 检查是否包含图片（在获取槽位之前检查，避免占用槽位）
        images = await self._extract_images_from_message(event)
        if not images:
            yield event.plain_result("❌ 视频生成需要您在消息中包含图片。请上传图片后再试。")
            return
        
        # 次数检查（管理员跳过）- 在获取槽位之前检查
        is_admin = self._is_global_admin(event) or self._is_admin(event)
        remaining_count = -1  # -1 表示管理员或未开启限制，不显示次数
        
        if not is_admin and self.enable_user_limit:
            user_count = self._get_user_count(user_id)
            if user_count <= 0:
                yield event.plain_result(
                    "❌ 您的视频生成次数已用完。\n"
                    "💡 请使用「辉宝赐福」获取免费次数。"
                )
                return
            # 立即扣除次数（不管成功与否）
            await self._decrease_user_count(user_id)
            remaining_count = self._get_user_count(user_id)
        
        # 群聊队列限制检查（所有前置检查通过后再获取槽位）
        if not await self._acquire_group_slot(group_id):
            # 如果已扣除次数但获取槽位失败，需要退还次数
            if remaining_count >= 0:
                await self._increase_user_count(user_id, 1)
            yield event.plain_result(
                f"⚠️ 当前群聊已有 {self.group_task_limit} 个视频生成任务在进行中，请稍后再试。"
            )
            return
        
        try:
            # 生成任务ID并记录
            import uuid
            task_id = str(uuid.uuid4())[:8]
            self._processing_tasks[user_id] = task_id
            
            # 构建提示信息
            tip_parts = [
                f"🎥 正在使用Grok为您生成视频，请稍候（预计需要几分钟）...",
                f"🆔 任务ID: {task_id}"
            ]
            if remaining_count >= 0:
                tip_parts.append(f"🎬 剩余视频生成次数: {remaining_count}")
            tip_parts.append("📝 提示：如果显示超时但稍后收到视频，说明发送成功。")
            
            yield event.plain_result("\n".join(tip_parts))
            
            # 启动异步任务避免超时，传入剩余次数和群组ID
            asyncio.create_task(self._async_generate_video(event, prompt, task_id, remaining_count, group_id))
        
        except Exception as e:
            logger.error(f"视频生成命令异常: {e}")
            yield event.plain_result(f"❌ 生成视频时遇到问题: {str(e)}")

    @filter.command("grok测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试Grok API连接（管理员专用）"""
        if not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return
        
        try:
            test_results = [Plain("🔍 Grok视频生成插件测试结果\n" + "="*30 + "\n\n")]
            
            # 检查配置
            if not self.api_key:
                test_results.append(Plain("❌ API密钥未配置\n"))
            else:
                test_results.append(Plain("✅ API密钥已配置\n"))
            
            test_results.append(Plain(f"📡 API地址: {self.api_url}\n"))
            test_results.append(Plain(f"🤖 模型ID: {self.model_id}\n"))
            test_results.append(Plain(f"⏱️ 超时时间: {self.timeout_seconds}秒\n"))
            test_results.append(Plain(f"🔄 最大重试: {self.max_retry_attempts}次\n"))
            test_results.append(Plain(f"📁 视频存储目录: {self.videos_dir}\n"))
            
            if self.enabled:
                test_results.append(Plain("✅ 功能已启用\n"))
            else:
                test_results.append(Plain("❌ 功能已禁用\n"))
            
            yield event.chain_result(test_results)
        
        except Exception as e:
            logger.error(f"测试命令异常: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @filter.command("grok帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助信息"""
        help_text = (
            "🎬 Grok视频生成插件帮助\n\n"
            "使用方法：\n"
            "1. 发送一张图片\n"
            "2. 引用该图片发送：/视频 <提示词>\n\n"
            "示例：\n"
            "• /视频 让太阳升起来\n"
            "• /视频 添加下雨效果\n"
            "• /视频 让角色跳舞\n\n"
            "LLM函数调用：\n"
            "• generate_video_with_grok - AI可调用的视频生成工具\n\n"
            "管理员命令：\n"
            "• /grok测试 - 测试API连接\n"
            "• /grok帮助 - 显示此帮助信息\n\n"
            "签到系统：\n"
            "• 辉宝赐福 - 每日签到获取视频生成次数\n"
            "• 视频次数 - 查询剩余次数\n\n"
            "管理员命令：\n"
            "• /视频增加次数 @用户 <次数> - 增加用户次数\n\n"
            "注意：视频生成需要较长时间，请耐心等待"
        )
        yield event.plain_result(help_text)

    # ==================== 签到系统命令 ====================

    @filter.regex(r"^[#/!！]?辉宝赐福\s*$")
    async def on_video_checkin(self, event: AstrMessageEvent):
        """每日签到获取视频生成次数（辉宝赐福）"""
        if not self.enable_checkin:
            yield event.plain_result("📅 本机器人未开启视频签到功能。")
            return
        
        user_id = str(event.get_sender_id())
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 检查是否已签到
        if self.user_checkin_data.get(user_id) == today_str:
            remaining = self._get_user_count(user_id)
            yield event.plain_result(f"您今天已经签到过了。\n🎬 剩余视频生成次数: {remaining}")
            return
        
        # 计算奖励
        reward = 0
        if self.enable_random_checkin:
            reward = random.randint(self.checkin_random_reward_min, self.checkin_random_reward_max)
        else:
            reward = self.checkin_fixed_reward
        
        # 增加次数
        await self._increase_user_count(user_id, reward)
        
        # 记录签到
        self.user_checkin_data[user_id] = today_str
        await self._save_user_checkin_data()
        
        new_count = self._get_user_count(user_id)
        yield event.plain_result(f"🎉 签到成功！获得视频生成 {reward} 次\n🎬 当前剩余次数: {new_count}")

    @filter.regex(r"^[#/!！]?视频次数\s*$")
    async def on_query_video_counts(self, event: AstrMessageEvent):
        """查询视频生成剩余次数"""
        user_id = str(event.get_sender_id())
        
        # 管理员可以查询其他用户
        if self._is_global_admin(event) or self._is_admin(event):
            # 检查是否有@用户
            at_seg = None
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message'):
                for seg in event.message_obj.message:
                    if isinstance(seg, At):
                        at_seg = seg
                        break
            
            if at_seg:
                user_id = str(at_seg.qq)
        
        count = self._get_user_count(user_id)
        
        if user_id == str(event.get_sender_id()):
            yield event.plain_result(f"🎬 您的视频生成剩余次数: {count}")
        else:
            yield event.plain_result(f"🎬 用户 {user_id} 的视频生成剩余次数: {count}")

    @filter.command("视频增加次数", prefix_optional=True)
    async def on_add_video_counts(self, event: AstrMessageEvent):
        """管理员增加用户视频生成次数"""
        if not self._is_global_admin(event) and not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return
        
        cmd_text = event.message_str.strip() if hasattr(event, 'message_str') else ""
        
        # 检查是否有@用户
        at_seg = None
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message'):
            for seg in event.message_obj.message:
                if isinstance(seg, At):
                    at_seg = seg
                    break
        
        target_qq = None
        count = 0
        
        if at_seg:
            target_qq = str(at_seg.qq)
            # 从命令文本中提取次数
            match = re.search(r"(\d+)\s*$", cmd_text)
            if match:
                count = int(match.group(1))
        else:
            # 尝试从文本中提取 QQ号 和 次数
            match = re.search(r"(\d+)\s+(\d+)", cmd_text)
            if match:
                target_qq = match.group(1)
                count = int(match.group(2))
        
        if not target_qq or count <= 0:
            yield event.plain_result(
                "格式错误:\n"
                "#视频增加次数 @用户 <次数>\n"
                "或 #视频增加次数 <QQ号> <次数>"
            )
            return
        
        await self._increase_user_count(target_qq, count)
        new_count = self._get_user_count(target_qq)
        yield event.plain_result(f"✅ 已为用户 {target_qq} 增加 {count} 次视频生成次数\n🎬 该用户当前剩余: {new_count}")

    async def terminate(self):
        """插件卸载时调用"""
        # 清理速率限制锁
        self._rate_limit_locks.clear()
        logger.info("Grok视频生成插件已卸载")
