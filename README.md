# Grok视频生成插件

基于Grok API的视频生成插件，支持根据图片和提示词生成视频内容。
Grok2api搭建教程：https://github.com/chenyme/grok2api

## 功能特性

- 🎬 **视频生成**：根据图片和文字提示生成视频
- 🤖 **LLM集成**：支持AI助手调用视频生成功能
- 🛡️ **权限控制**：支持群组白名单/黑名单和速率限制
- 🔄 **自动重试**：API请求失败时自动重试
- 📁 **本地存储**：自动下载并保存生成的视频
- 🌊 **流式解析**：支持 huan-grok-imagine-1.0-video 模型及其 SSE 流式返回
- ✨ **签到系统**：支持「辉宝赐福」签到系统，每日获取生成次数

## 使用方法

### 基础用法
1. 发送一张图片到群聊或私聊
2. 引用该图片发送命令：`/视频 <提示词>`

### 签到与次数管理
- **辉宝赐福**：每日签到获取免费的视频生成次数
- **视频次数**：查询您当前剩余的视频生成次数

### 管理员命令
- `/视频增加次数 @用户 <次数>` - 为指定用户增加生成额度
- `/grok测试` - 测试API连接状态
- `/grok帮助` - 显示详细帮助信息

## 配置说明

### 必需配置
- **server_url**: Grok API服务器地址
- **model_id**: 模型ID（默认：huan-grok-imagine-1.0-video）
- **api_key**: Grok API密钥

### 签到系统配置
- **enable_user_limit**: 是否启用用户次数限制（默认：true）
- **enable_checkin**: 是否开启每日签到功能（默认：true）
- **checkin_fixed_reward**: 固定签到奖励次数（默认：2）
- **enable_random_checkin**: 是否开启随机奖励（默认：false）

### 其他配置
- **enabled**: 启用/禁用功能（默认：true）
- **timeout_seconds**: 请求超时时间（默认：180秒）
- **group_control_mode**: 群组控制模式（off/whitelist/blacklist）
- **rate_limit_enabled**: 启用速率限制（默认：true）
- **save_video_enabled**: 是否保留视频文件（默认：false）

## 技术实现

### API调用
插件已适配 某些渠道流式grok 的 SSE 流式响应，能够自动拼接 Chunk 数据并从中提取视频地址。

### 视频处理
- 自动解析API响应中的视频URL（支持正则提取与结构化解析）
- 下载视频到 AstrBot Data 目录下的 `data/plugins/astrbot_plugin_grok_video/videos/`
- `save_video_enabled=false` 时，发送成功后自动清理本地缓存

## 版本信息

- 版本：1.2.1
- 作者：辉宝
- 仓库：https://github.com/YumenoSayuri/astrbot_plugin_grok_video

Made by Nova for 辉宝主人 ❤
