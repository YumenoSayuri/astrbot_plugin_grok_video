# Grok视频生成插件

基于Grok API的视频生成插件，支持根据图片和提示词生成视频内容。
Grok2api搭建教程：https://github.com/chenyme/grok2api

## 功能特性

- 🎬 **视频生成**：根据图片和文字提示生成视频
- 🤖 **LLM集成**：支持AI助手调用视频生成功能
- 🛡️ **权限控制**：支持群组白名单/黑名单和速率限制
- 🔄 **自动重试**：API请求失败时自动重试
- 📁 **本地存储**：自动下载并保存生成的视频
- 🌊 **流式解析**：支持 第三方渠道的 grok 模型及其 SSE 流式返回

## 使用方法

### 基础用法
1. 发送一张图片到群聊或私聊
2. 引用该图片发送命令：`/视频 <提示词>`

### 示例
```
/视频 让太阳升起来
/视频 添加下雨效果
/视频 让角色跳舞
```

### LLM函数调用
插件提供 `generate_video_with_grok` 工具，AI助手可以自动调用生成视频。

## 配置说明

### 必需配置
- **server_url**: Grok API服务器地址
- **model_id**: 模型ID
- **api_key**: Grok API密钥

### 可选配置
- **enabled**: 启用/禁用功能（默认：true）
- **timeout_seconds**: 请求超时时间（默认：180秒）
- **max_retry_attempts**: 最大重试次数（默认：3次）
- **group_control_mode**: 群组控制模式（off/whitelist/blacklist）
- **rate_limit_enabled**: 启用速率限制（默认：true）
- **rate_limit_max_calls**: 速率限制次数（默认：5次/小时）
- **nap_server_address**: NapCat 文件服务器地址，用于跨容器/跨主机发送视频（可选）
- **nap_server_port**: NapCat 文件服务器端口，需与 NapCat 文件中转服务保持一致（可选）
- **save_video_enabled**: 是否保留生成的视频文件（默认：false，发送成功后自动清理缓存）

## 管理员命令

- `/grok测试` - 测试API连接状态
- `/grok帮助` - 显示帮助信息

## 技术实现

### API调用
插件已适配 `huan-grok-imagine-1.0-video` 的 SSE 流式响应，能够自动拼接 Chunk 数据并从中提取视频地址。

### 视频处理
- 自动解析API响应中的视频URL（支持正则提取与结构化解析）
- 下载视频到 AstrBot Data 目录下的 `data/plugins/astrbot_plugin_grok_video/videos/`
- `save_video_enabled=false` 时，发送成功后自动清理本地缓存

## 版本信息

- 版本：1.2.0
- 作者：辉宝
- 仓库：https://github.com/YumenoSayuri/astrbot_plugin_grok_video
- 兼容：AstrBot插件系统
