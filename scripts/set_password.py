#!/usr/bin/env python3
"""Generate salted scrypt password hash for Kiki target-machine `.env` login config.

Usage:
    python3 scripts/set_password.py
    python3 scripts/set_password.py --username admin
    python3 scripts/set_password.py --username admin --write-env
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.auth import hash_password  # noqa: E402


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        matched_key = None
        for key in list(remaining.keys()):
            if re.match(rf"^\s*#?\s*{re.escape(key)}\s*=", line):
                # Only replace active (uncommented) assignment; keep comments untouched
                if not line.lstrip().startswith("#"):
                    matched_key = key
                    break
        if matched_key is not None:
            new_lines.append(f"{matched_key}={remaining.pop(matched_key)}")
        else:
            new_lines.append(line)

    if remaining:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        for key, value in remaining.items():
            new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为 Kiki 目标机生成加密密码哈希（scrypt），可选直接写入 .env 文件。"
    )
    parser.add_argument(
        "-u",
        "--username",
        default="",
        help="登录用户名（不传则交互输入，默认 admin）",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="直接写入项目根目录的 .env 文件（不存在时自动创建）",
    )
    parser.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="指定 .env 文件路径（配合 --write-env 使用）",
    )
    args = parser.parse_args()

    try:
        username = args.username.strip()
        if not username:
            raw_u = input("登录用户名 [admin]: ").strip()
            username = raw_u or "admin"
        pwd = getpass.getpass("输入登录密码: ")
        if not pwd:
            print("错误：密码不能为空。", file=sys.stderr)
            return 1
        confirm = getpass.getpass("再次确认密码: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 1

    if pwd != confirm:
        print("错误：两次输入的密码不一致。", file=sys.stderr)
        return 1

    hashed = hash_password(pwd)

    if args.write_env:
        env_path = Path(args.env_file).expanduser().resolve()
        _upsert_env_file(
            env_path,
            {
                "KIKI_AUTH_USERNAME": username,
                "KIKI_AUTH_PASSWORD_HASH": hashed,
            },
        )
        print(f"\n已写入加密登录凭据到 {env_path}")
        print(f"用户名: {username}")
        print("重启后端服务后生效。")
    else:
        print("\n请将以下配置添加到目标机的 .env 文件中：\n")
        print(f"KIKI_AUTH_USERNAME={username}")
        print(f"KIKI_AUTH_PASSWORD_HASH={hashed}")
        print("\n提示：也可使用 `--write-env` 参数自动写入 .env。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
