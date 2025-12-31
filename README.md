#功能说明

让麦麦在指定群聊中定时打卡（默认0:00）

#安装方法

1.下载或克隆本仓库
  ```bash
  git colne https://github.com/WaterInk0101/groupsign.git
  ```
2.将本插件文件放置到对应插件目录`/plugins`中

3.在napcat中创建HTTP Server：host:127.0.0.1,端口4430，务必与`config`中填写的`host` `Prot`一致

#命令使用方法

  /groupsign list_groups       //查看打卡群聊列表
  
  /groupsign add_group 群号    //将群聊添加至打卡列表
  
  /groupsign remove_group 群号 //将群聊移出打卡列表
  
  /groupsign execute 群聊      //立即在群聊中执行打卡操作

<img width="2064" height="273" alt="image" src="https://github.com/user-attachments/assets/eb9b5ba1-6d2d-4dee-a92f-c7e832fa32c6" />

> [!NOTE]
> `congig.toml`中的其他参数请勿随意更改，除非你知道你自己在做什么
