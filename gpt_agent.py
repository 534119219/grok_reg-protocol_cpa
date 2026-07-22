"""GPT 注册的后续产物：Codex Agent 身份注册 + sub2api 推送。

- create_agent_identity：/api/auth/session 拿到 accessToken 后，
  生成 Ed25519 密钥对并在 auth.openai.com 注册 agent，
  产出 Codex CLI 的 auth.json（auth_mode=agent_identity），落盘 gpt_agents/。
- push_to_sub2api：通过管理密钥（x-api-key）把账号推到 sub2api，
  支持指定分组（group id 或分组名）。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
AGENT_REGISTER_URL = "https://auth.openai.com/api/accounts/v1/agent/register"
GPT_AGENTS_DIR = "gpt_agents"

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:
    print(msg, flush=True)


def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    parts = jwt_token.split(".")
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def session_info_from_token(access_token: str) -> dict[str, Any]:
    """从 accessToken JWT 解码账号信息。"""
    claims = decode_jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {}) or {}
    profile = claims.get("https://api.openai.com/profile", {}) or {}
    return {
        "account_id": auth_info.get("chatgpt_account_id", ""),
        "user_id": auth_info.get("chatgpt_user_id", ""),
        "email": profile.get("email", ""),
        "plan_type": auth_info.get("chatgpt_plan_type", "free"),
        "exp": claims.get("exp"),
    }


def _gen_ed25519_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    pkcs8_der = private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    private_key_b64 = base64.b64encode(pkcs8_der).decode()

    pub_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    header = b"ssh-ed25519"
    blob = (
        len(header).to_bytes(4, "big") + header + len(pub_bytes).to_bytes(4, "big") + pub_bytes
    )
    public_key_ssh = f"ssh-ed25519 {base64.b64encode(blob).decode()}"
    return private_key_b64, public_key_ssh


def create_agent_identity(
    access_token: str,
    *,
    email: str = "",
    proxy: str | None = None,
    verify_task: bool = False,
    impersonate: str = "chrome",
    out_dir: Path | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """注册 Codex agent 身份，返回 auth.json dict（并落盘 gpt_agents/agent-<email>.json）。"""
    from curl_cffi import requests as creq

    log = log or _noop
    info = session_info_from_token(access_token)
    account_id = info["account_id"]
    user_id = info["user_id"]
    email = email or info["email"]
    plan_type = info["plan_type"]
    if not account_id or not user_id:
        raise RuntimeError("accessToken JWT 缺少 chatgpt_account_id/chatgpt_user_id")

    private_key_b64, public_key_ssh = _gen_ed25519_keypair()

    kwargs: dict[str, Any] = {"impersonate": impersonate, "timeout": 20}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    r = creq.post(
        AGENT_REGISTER_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "abom": {
                "agent_version": AGENT_VERSION,
                "agent_harness_id": AGENT_HARNESS_ID,
                "running_location": "local",
            },
            "agent_public_key": public_key_ssh,
        },
        **kwargs,
    )
    if r.status_code != 200:
        raise RuntimeError(f"agent register HTTP {r.status_code}: {r.text[:200]}")
    agent_runtime_id = r.json().get("agent_runtime_id")
    if not agent_runtime_id:
        raise RuntimeError(f"agent register 未返回 agent_runtime_id: {r.text[:200]}")
    log(f"[*] agent 已注册: {agent_runtime_id}")

    if verify_task:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            pkcs8_der = base64.b64decode(private_key_b64)
            pem = (
                b"-----BEGIN PRIVATE KEY-----\n"
                + base64.encodebytes(pkcs8_der)
                + b"-----END PRIVATE KEY-----\n"
            )
            private_key = load_pem_private_key(pem, password=None)
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            signature_b64 = base64.b64encode(
                private_key.sign(f"{agent_runtime_id}:{timestamp}".encode())
            ).decode()
            rt = creq.post(
                f"https://auth.openai.com/api/accounts/v1/agent/{agent_runtime_id}/task/register",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                json={"timestamp": timestamp, "signature": signature_b64},
                **kwargs,
            )
            log(f"[*] task 验证: HTTP {rt.status_code}")
        except Exception as exc:
            log(f"[Debug] task 验证失败（不影响 auth.json）: {exc}")

    auth_json = {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": private_key_b64,
            "account_id": account_id,
            "chatgpt_user_id": user_id,
            "email": email,
            "plan_type": plan_type,
            "chatgpt_account_is_fedramp": False,
        },
    }

    root = out_dir or (Path(__file__).resolve().parent / GPT_AGENTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in email or agent_runtime_id)
    (root / f"agent-{safe}.json").write_text(
        json.dumps(auth_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"[*] agent auth.json 已写出: {GPT_AGENTS_DIR}/agent-{safe}.json")
    return auth_json


# ── sub2api 推送 ──


def _resolve_group_id(base: str, api_key: str, group: str, proxy: str | None, log: LogFn) -> int | None:
    """group 为数字直接用；为名称时查 /api/v1/admin/groups/all 解析。"""
    group = (group or "").strip()
    if not group:
        return None
    if group.isdigit():
        return int(group)
    from curl_cffi import requests as creq

    kwargs: dict[str, Any] = {"impersonate": "chrome", "timeout": 15}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    r = creq.get(f"{base}/api/v1/admin/groups/all", headers={"x-api-key": api_key}, **kwargs)
    if r.status_code != 200:
        raise RuntimeError(f"查询分组失败 HTTP {r.status_code}")
    data = r.json()
    groups = data.get("data") if isinstance(data, dict) else data
    if isinstance(groups, dict):
        groups = groups.get("items") or groups.get("groups") or []
    for g in groups or []:
        if str(g.get("name", "")).strip() == group:
            return int(g["id"])
    raise RuntimeError(f"sub2api 分组不存在: {group}")


def push_to_sub2api(
    *,
    sess_data: dict[str, Any],
    email: str,
    access_token: str,
    cfg: dict[str, Any],
    proxy: str | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """把 GPT 账号推送到 sub2api（x-api-key 认证，支持指定分组）。"""
    from curl_cffi import requests as creq

    log = log or _noop
    base = str(cfg.get("sub2api_base") or "").strip().rstrip("/")
    api_key = str(cfg.get("sub2api_api_key") or "").strip()
    if not base or not api_key:
        raise RuntimeError("sub2api_base / sub2api_api_key 未配置")

    group_id = _resolve_group_id(base, api_key, str(cfg.get("sub2api_group_id") or ""), proxy, log)

    account = sess_data.get("account") or {}
    info = session_info_from_token(access_token)
    expires_at = ""
    exp = info.get("exp")
    if exp:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
    credentials: dict[str, Any] = {
        "access_token": access_token,
        "expires_at": expires_at or sess_data.get("expires", ""),
        "email": email,
        "plan_type": account.get("planType", "free"),
    }
    session_token = sess_data.get("sessionToken", "")
    if session_token:
        credentials["refresh_token"] = session_token
    if account.get("id"):
        credentials["chatgpt_account_id"] = account["id"]
    if info.get("user_id"):
        credentials["chatgpt_user_id"] = info["user_id"]

    body = {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"email": email, "source": "gpt_register"},
        "concurrency": int(cfg.get("sub2api_concurrency", 10) or 10),
        "priority": int(cfg.get("sub2api_priority", 1) or 1),
        "group_ids": [group_id] if group_id else [],
    }
    kwargs: dict[str, Any] = {"impersonate": "chrome", "timeout": 20}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    r = creq.post(
        f"{base}/api/v1/admin/accounts",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        **kwargs,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"sub2api 推送失败 HTTP {r.status_code}: {r.text[:200]}")
    log(f"[*] sub2api 推送成功: {email}（group={group_id or '默认'}）")
    return r.json()


__all__ = [
    "create_agent_identity",
    "push_to_sub2api",
    "session_info_from_token",
    "decode_jwt_claims",
    "GPT_AGENTS_DIR",
]
