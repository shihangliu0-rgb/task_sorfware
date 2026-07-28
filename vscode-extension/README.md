# DevLog VSCode 插件

在编辑器里直接记任务、存报错、分析日志，数据写入本地 DevLog 桌面应用。

## 安装

1. 先启动 DevLog 桌面应用（保持后台运行）
2. 把 `vscode-extension` 整个文件夹复制到：
   - Windows：`%USERPROFILE%\.vscode\extensions\devlog-companion`
   - macOS / Linux：`~/.vscode/extensions/devlog-companion`
3. 重启 VSCode

## 命令（Ctrl+Shift+P 里搜 DevLog）

| 命令 | 快捷键 | 说明 |
|---|---|---|
| 新建任务 | — | 自动带上当前 Git 分支和工作区名 |
| 把选中内容存为问题 | `Ctrl+Alt+I` | 自动附上文件名和行号 |
| 分析选中的日志 | 右键菜单 | 侧栏打开分析结果 |
| 快速记一笔 | `Ctrl+Alt+N` | 写入时间线 |
| 同步当前仓库提交 | — | 把 commit 关联到任务 |
| 查看未完成任务 | 点状态栏 | 可切换当前任务 / 标记完成 |

状态栏右下角实时显示待办数和未解决问题数。
