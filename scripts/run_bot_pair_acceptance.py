"""手动真实双机器人验收；导入不读取配置、不连接服务、不启动任务。"""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp


@dataclass(frozen=True)
class Instance:
    root: Path
    url: str
    token: str = field(repr=False)
    uid: str


def load_instances(roots: tuple[Path, Path]) -> tuple[tuple[Instance, Instance], str, str]:
    """配置只在显式手动执行时读取，验证后保留已确定的测试条件。"""
    configs = [runpy.run_path(str(root / "config.py")) for root in roots]
    uids = [config["OOPZ_CONFIG"]["person_uid"] for config in configs]
    if roots[0] == roots[1] or not all(uids) or uids[0] == uids[1]:
        raise ValueError("需要独立目录和两个不同账号")
    targets = []
    instances = []
    for index, (root, config) in enumerate(zip(roots, configs, strict=True)):
        voice = config["VOICE_AGENT_CONFIG"]
        if (voice.get("cascade_turn_limit") != 3
                or voice.get("listen_only_uids") != [uids[1-index]]
                or voice.get("backend") != "aliyun_deepseek"
                or voice.get("reply_probability_percent") != 100
                or voice.get("barge_in") is not False
                or voice.get("voice_leave_enabled") is not False
                or voice.get("voice_volume_enabled") is not False):
            raise ValueError("请按验收指南设置级联后端、对端白名单、三次限额及关闭语音控制/抢话")
        if any(row.get("enabled") for row in config.get("VOICE_AUTO_VISIT_CONFIG", {}).get("areas", {}).values()):
            raise ValueError("验收期间关闭自动串门")
        web = config["WEBUI_CONFIG"]
        instances.append(Instance(root, f"http://127.0.0.1:{web['port']}", web["token"], uids[index]))
        oopz = config["OOPZ_CONFIG"]
        targets.append((oopz["default_area"], oopz["default_channel"]))
    if targets[0] != targets[1] or not all(targets[0]) or instances[0].url == instances[1].url:
        raise ValueError("需要同一非空房间和不同 WebUI 端口")
    return (instances[0], instances[1]), *targets[0]


def revision(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=5).strip()
    return {"commit": git("rev-parse", "HEAD"), "tracked_changes": git("diff", "--name-only", "HEAD")}


async def run_acceptance(
    instances: tuple[Instance, Instance], *, area: str, channel: str,
    timeout: float = 150, poll_interval: float = 1,
    on_report: Callable[[dict], None] | None = None,
) -> dict:
    """控制两端 HTTP 与实际渠道终态诊断；不替代第三账号人工旁听。

    整场有时限；所有退出路径均独立尝试两端退房及确认。
    成功必须是本次进房后的三次实际播放完成，旧诊断和日志不能算新证据。
    """
    started = time.monotonic()
    report = {"outcome": "cancelled", "events": [], "completed_replies": [0, 0]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as client:
        async def call(index, path, body=None):
            instance = instances[index]
            async with client.request(
                "GET" if body is None else "POST", instance.url + path,
                json=body, headers={"Authorization": "Bearer " + instance.token},
            ) as response:
                payload = await response.json()
                if response.status >= 400 or payload.get("ok") is not True:
                    # 外部正文可能包含模型载荷或身份信息，不输出它。
                    raise RuntimeError(f"instance {index+1}: {path}: HTTP {response.status}")
                return payload

        async def both(path, body=None):
            results = await asyncio.gather(*(call(index, path, body) for index in range(2)), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return results

        async def test():
            await both("/voice/leave", {})
            await both("/voice/join", {"area": area, "channel": channel})
            states = await both("/voice/status")
            baseline_turns = [state["status"]["turns"] for state in states]
            def completed_ids(state):
                return {row["trigger_id"] for row in state.get("voice_observation", {}).get("recent_decisions", [])
                        if isinstance(row, dict) and isinstance(row.get("trigger_id"), str)
                        and row.get("reason") == "reply" and row.get("outcome") == "completed"}
            baseline_completed = [completed_ids(state["status"]) for state in states]
            if any(state["status"]["cascade_turn_limit"] != 3 for state in states):
                raise RuntimeError("实例未加载三次自动接话限额")
            while True:
                members = await both("/voice/members")
                if all(any(row["uid"] == instances[1-index].uid and row.get("live") is True
                           for row in payload["members"]) for index, payload in enumerate(members)):
                    break
                await asyncio.sleep(poll_interval)
            await call(0, "/voice/speak", {"text": "你好小蓝，我是小橙。周末我想去公园散步，你想做什么？"})
            previous = None
            while True:
                states = [payload["status"] for payload in await both("/voice/status")]
                snapshot = [{key: state.get(key) for key in (
                    "joined", "speaking", "cascade_attempts", "turns", "last_user_text", "last_reply",
                )} for state in states]
                if snapshot != previous:
                    report["events"].append({"elapsed": round(time.monotonic()-started, 2), "instances": snapshot})
                    previous = snapshot
                if not all(state["joined"] for state in states):
                    raise RuntimeError("实例提前退房")
                if any(state["cascade_attempts"] > 3 for state in states):
                    raise RuntimeError("自动接话超过三次")
                evidence = [sorted(completed_ids(state)-previous) for state, previous in
                            zip(states, baseline_completed, strict=True)]
                completed = [len(ids) for ids in evidence]
                report["completion_evidence"] = evidence
                report["completed_replies"] = completed
                if (completed == [3, 3] and all(state["cascade_attempts"] == 3 and not state["speaking"] for state in states)
                        and all(state["turns"]-base >= expected for state, base, expected in zip(states, baseline_turns, (4, 3), strict=True))):
                    report["outcome"] = "completed"
                    return
                await asyncio.sleep(poll_interval)

        try:
            await asyncio.wait_for(test(), timeout)
        except Exception as exc:
            report["outcome"] = type(exc).__name__
        finally:
            # 单端失败不能取消另一端的清理；清理使用独立预算。
            async def bounded(index, path, body=None):
                return await asyncio.wait_for(call(index, path, body), 8)

            async def cleanup():
                left = await asyncio.gather(*(bounded(index, "/voice/leave", {}) for index in range(2)), return_exceptions=True)
                final = await asyncio.gather(*(bounded(index, "/voice/status") for index in range(2)), return_exceptions=True)
                return left, final

            cleanup_task = asyncio.create_task(cleanup(), name="bot-pair-cleanup")
            # 重复取消也不能截断两端独立清理；取消在收尾后继续传播。
            cancelled = False
            while True:
                try:
                    left, final = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            report["left_confirmed"] = [
                isinstance(result, dict) and result.get("ok") is True
                and isinstance(state, dict) and isinstance(state.get("status"), dict)
                and state["status"].get("joined") is False
                for result, state in zip(left, final, strict=True)
            ]
            report["elapsed"] = round(time.monotonic()-started, 2)
            if not all(report["left_confirmed"]):
                report["outcome"] = "cleanup_failed"
            if on_report is not None:
                on_report(report)
            if cancelled:
                raise asyncio.CancelledError
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--report", type=Path, required=True, help="保存到忽略的 data/，含测试对话")
    args = parser.parse_args()
    roots = (args.first.resolve(), args.second.resolve())
    instances, area, channel = load_instances(roots)
    versions = [revision(root) for root in roots]
    def save(report):
        report["versions"] = versions
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    report = asyncio.run(run_acceptance(instances, area=area, channel=channel, on_report=save))
    print(json.dumps({key: report[key] for key in ("outcome", "completed_replies", "left_confirmed", "elapsed")}, ensure_ascii=False))
    return 0 if report["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
