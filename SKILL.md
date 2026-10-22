---
name: agent-wakeup
description: >
  仅在用户或上下文明确要求使用本 skill 时调用。
  注册后台条件检查，在条件满足后向指定 agent 会话投递消息。
---

# Agent Wakeup

使用本目录的 `agent-wakeup` 入口管理监控。命令及参数见 `agent-wakeup --help` 和各子命令的 `--help`。

## 编写 probe

<u>Probe</u> 是一次执行只检查一次条件的脚本。返回格式见 [JSON Schema](src/agent_wakeup/schema.json)，将结果位置、必要的背景和后续指令写入 `summary`。

正常返回使用退出码 `0`，stdout 输出一个 JSON 对象，诊断写 stderr。较长日志可在 prompt 中引用文件路径。

## 注册与继续

用 `register` 指定 probe 和监控 key，按任务需要选择间隔、期限、解释器及工作目录。首版通过 Codex 投递：目标默认取当前 `CODEX_THREAD_ID`，也可指定会话 UUID。

注册成功后报告监控 ID，结束当前轮次。后台运行器执行后续检查，触发时发送 prompt。收到消息后，根据其中的结果和指令继续工作。
