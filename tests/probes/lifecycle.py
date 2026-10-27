"""A file-controlled probe shared by service integration tests and manual acceptance."""

import argparse
import json
from pathlib import Path


def main() -> None:
    """Read marker files from a dedicated acceptance directory and emit one probe result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="本次验收使用的独立目录")
    directory = parser.parse_args().directory.resolve()
    if (directory / "wake").exists():
        result = {
            "state": "wake",
            "summary": (
                f"agent-wakeup 验收：{directory.name} 的最终唤醒。"
                + "请检查这项监控的投递结果及服务退出情况，然后报告验收结果。"
            ),
        }
    elif (directory / "notify").exists():
        (directory / "notify").unlink()
        result = {
            "state": "notify",
            "summary": (
                f"agent-wakeup 验收：{directory.name} 的中间通知。"
                + "请检查这项监控仍在运行，报告结果后结束本轮，等待后续验收消息。"
            ),
        }
    else:
        result = {"state": "wait", "summary": "等待验收触发。"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
