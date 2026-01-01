# 群聊定时打卡插件

---

## 功能说明

让麦麦在群聊中到点打卡


## 安装方法

1.下载或克隆本仓库
  ```bash
  git colne https://github.com/WaterInk0101/groupsign.git
  ```
2.将本插件文件放置到对应插件目录 `/plugins` 中

3.在napcat中创建HTTP Server  
 *host:127.0.0.1,端口4430*  
 务必与 `config` 中填写的 `host` `Prot` 一致  
<img width="580" height="746" alt="image" src="https://github.com/user-attachments/assets/46391940-6ae1-4643-93ec-1a23d1d035ba" />

## 命令使用方法

```bash
  /groupsign list_groups       //查看打卡群聊列表  
  /groupsign add_group 群号    //将群聊添加至打卡列表  
  /groupsign remove_group 群号 //将群聊移出打卡列表  
  /groupsign execute 群聊      //立即在群聊中执行打卡操作
```  
<img width="2064" height="273" alt="image" src="https://github.com/user-attachments/assets/eb9b5ba1-6d2d-4dee-a92f-c7e832fa32c6" />

## 配置选项

```bash
[plugin]  
enable = true                 #是否启用插件

[components]  
enable_sign = true            #是否启用打卡功能

[sign]
groups = ["123456", "234567"] #需要打卡的群聊白名单
reminder_time = "00:00"       #执行打卡的时间 24小时制 如"18:00" "6:00"

[permissions]
admin_users = ["12345678"]    #允许使用命令的用户白名单

[api]
host = "127.0.0.1"
port = "4430"
token = ""                    #与napcat中配置的一致
```
> [!NOTE]
> `congig.toml`中的其他参数请勿随意更改，除非你知道你自己在做什么
