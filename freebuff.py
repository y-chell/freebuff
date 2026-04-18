#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# @Time : 2026/4/17 13:10
#!/usr/bin/env python3
"""Freebuff OpenAI API 反代代理 (Python 版)"""

import asyncio
import argparse
import json
import os
import platform
import random
import re
import signal
import string
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("请先安装 aiohttp: pip install aiohttp")
    sys.exit(1)

API_BASE = os.environ.get("FREEBUFF_API_BASE", "www.codebuff.com")
LOCAL_PORT = int(os.environ.get("PORT", "9090"))
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "5"))
TIMEOUT_S = int(os.environ.get("TIMEOUT_S", "300"))

# 本代理的访问鉴权 Key（空字符串 = 不鉴权）
# 可在环境变量 API_KEY 或 --api-key 命令行参数中设置
PROXY_API_KEY = os.environ.get("API_KEY", "")

MODEL_TO_AGENT = {
    "minimax/minimax-m2.7": "base2-free",
    "z-ai/glm-5.1": "base2-free",
    "google/gemini-2.5-flash-lite": "file-picker",
    "google/gemini-3.1-flash-lite-preview": "file-picker-max",
    "google/gemini-3.1-pro-preview": "thinker-with-files-gemini",
}

default_model = "minimax/minimax-m2.7"

# 多账号 Token 池
# token_pool 每项示例:
# {
#   "id": "user-id",
#   "name": "Alice",
#   "email": "a@example.com",
#   "authToken": "...",
#   "credits": 0,
# }
token_pool = []
next_token_index = 0

# Agent Run 缓存改为按 (token, agent_id) 维度维护
# run_cache: { (token_value, agent_id): run_id }
run_cache = {}

C = {
    "R": "\033[0m", "B": "\033[1m", "G": "\033[32m",
    "Y": "\033[33m", "E": "\033[31m", "C": "\033[36m", "D": "\033[90m",
}


def log(msg, t="info"):
    c = {"success": C["G"], "error": C["E"], "warn": C["Y"]}.get(t, C["C"])
    icon = {"success": "✓", "error": "✗", "warn": "⚠"}.get(t, "ℹ")
    print(f"{c}{icon}{C['R']} {msg}")


def token_fingerprint(auth_token):
    if not auth_token:
        return "none"
    return f"{auth_token[:6]}...{auth_token[-4:]}"


def generate_fingerprint_id():
    chars = string.ascii_lowercase + string.digits
    return f"codebuff-cli-{''.join(random.choices(chars, k=26))}"


def get_config_paths():
    home = Path.home()
    if platform.system() == "Windows":
        config_dir = Path(os.environ.get("APPDATA", str(home))) / "manicode"
    else:
        config_dir = home / ".config" / "manicode"
    return config_dir, config_dir / "credentials.json"


def load_accounts_from_env():
    raw_tokens = []

    single_token = os.environ.get("FREEBUFF_AUTH_TOKEN", "").strip()
    if single_token:
        raw_tokens.append(single_token)

    multi_tokens = os.environ.get("FREEBUFF_AUTH_TOKENS", "")
    if multi_tokens:
        for item in re.split(r"[\r\n,]+", multi_tokens):
            token = item.strip()
            if token:
                raw_tokens.append(token)

    accounts = []
    seen_tokens = set()
    for index, auth_token in enumerate(raw_tokens, start=1):
        if auth_token in seen_tokens:
            continue
        seen_tokens.add(auth_token)
        accounts.append({
            "id": f"env-{index}",
            "name": f"env-{index}",
            "email": "env@local",
            "authToken": auth_token,
            "credits": 0,
        })

    return accounts


def normalize_accounts(creds):
    """兼容旧格式(default)和新格式(accounts)。"""
    if not isinstance(creds, dict):
        return []

    accounts = []

    raw_accounts = creds.get("accounts")
    if isinstance(raw_accounts, list):
        for entry in raw_accounts:
            if isinstance(entry, dict) and entry.get("authToken"):
                accounts.append({
                    "id": entry.get("id"),
                    "name": entry.get("name") or "unknown",
                    "email": entry.get("email") or "unknown",
                    "authToken": entry.get("authToken"),
                    "credits": entry.get("credits", 0),
                })

    default_entry = creds.get("default")
    if isinstance(default_entry, dict) and default_entry.get("authToken"):
        default_token = default_entry.get("authToken")
        exists = any(acc.get("authToken") == default_token for acc in accounts)
        if not exists:
            accounts.insert(0, {
                "id": default_entry.get("id"),
                "name": default_entry.get("name") or "default",
                "email": default_entry.get("email") or "unknown",
                "authToken": default_token,
                "credits": default_entry.get("credits", 0),
            })

    return accounts


def load_accounts():
    accounts = load_accounts_from_env()
    seen_tokens = {acc.get("authToken") for acc in accounts}

    _, creds_path = get_config_paths()
    if not creds_path.exists():
        return accounts

    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
    except Exception:
        return accounts

    for account in normalize_accounts(creds):
        auth_token = account.get("authToken")
        if auth_token and auth_token not in seen_tokens:
            accounts.append(account)
            seen_tokens.add(auth_token)

    return accounts


def save_accounts(accounts):
    config_dir, creds_path = get_config_paths()
    config_dir.mkdir(parents=True, exist_ok=True)

    if not accounts:
        return

    # default 指向当前首个账号，兼容旧逻辑
    data = {
        "default": {
            "id": accounts[0].get("id"),
            "name": accounts[0].get("name"),
            "email": accounts[0].get("email"),
            "authToken": accounts[0].get("authToken"),
            "credits": accounts[0].get("credits", 0),
        },
        "accounts": accounts,
    }
    creds_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_account(user_obj):
    """把新登录账号追加到 token 池并持久化（按 authToken 去重）。"""
    global token_pool

    new_acc = {
        "id": user_obj.get("id"),
        "name": user_obj.get("name") or "unknown",
        "email": user_obj.get("email") or "unknown",
        "authToken": user_obj.get("authToken") or user_obj.get("auth_token"),
        "credits": user_obj.get("credits", 0),
    }

    if not new_acc["authToken"]:
        raise RuntimeError("登录返回中缺少 authToken")

    existing = {acc.get("authToken") for acc in token_pool}
    if new_acc["authToken"] not in existing:
        token_pool.append(new_acc)

    save_accounts(token_pool)


def pick_next_account():
    """轮询选择下一个账号。"""
    global next_token_index

    if not token_pool:
        raise RuntimeError("没有可用 token，请先登录至少一个账号")

    idx = next_token_index % len(token_pool)
    next_token_index = (next_token_index + 1) % len(token_pool)
    return token_pool[idx], idx


async def prompt_add_accounts_on_startup(session):
    """启动时交互式决定是否新增账号，可连续添加多个。"""
    while True:
        ans = input("启动时是否新增一个账号登录到账号池？(y/N): ").strip().lower()
        if ans not in ("y", "yes"):
            break

        before = len(token_pool)
        await do_login(session)
        after = len(token_pool)
        if after > before:
            log(f"账号池新增成功，当前共 {after} 个账号", "success")
        else:
            log(f"账号未新增（可能是重复 token），当前共 {after} 个账号", "warn")


def parse_args():
    parser = argparse.ArgumentParser(description="Freebuff OpenAI Proxy (multi-token)")
    parser.add_argument(
        "--manage-accounts",
        action="store_true",
        help="启动时进入账号管理（可交互新增账号）",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="设置代理访问鉴权 Key（也可通过环境变量 API_KEY 设置）",
    )
    return parser.parse_args()


def get_run_cache_key(auth_token, agent_id):
    return (auth_token, agent_id)


# ============ HTTP 请求 ============

async def api_request(session, hostname, path, body=None, auth_token=None, method="POST"):
    url = f"https://{hostname}{path}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "freebuff-proxy/1.0",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=30)}
    if body and method == "POST":
        kwargs["json"] = body

    async with session.request(method, url, **kwargs) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = await resp.text()
        return {"status": resp.status, "data": data}


# ============ 登录流程 ============

async def do_login(session):
    log("需要登录 Freebuff...")
    fp_id = generate_fingerprint_id()
    log(f"指纹: {fp_id[:30]}...")

    res = await api_request(session, "freebuff.com", "/api/auth/cli/code", {"fingerprintId": fp_id})
    if res["status"] != 200 or "loginUrl" not in res["data"]:
        raise RuntimeError("获取登录 URL 失败")

    d = res["data"]
    login_url, fp_hash, expires = d["loginUrl"], d["fingerprintHash"], d["expiresAt"]

    print(f"\n{C['Y']}请在浏览器中打开:{C['R']}\n{C['C']}{login_url}{C['R']}\n")

    if platform.system() == "Darwin":
        subprocess.Popen(["open", login_url])
    elif platform.system() == "Windows":
        subprocess.Popen(["start", "", login_url], shell=True)

    input(f"{C['Y']}完成登录后按回车继续...{C['R']}")
    log("等待登录完成...")

    start = time.time()
    while time.time() - start < TIMEOUT_S:
        print(f"\r{C['D']}轮询中...{C['R']}", end="", flush=True)
        try:
            path = (
                f"/api/auth/cli/status?fingerprintId={quote(str(fp_id))}"
                f"&fingerprintHash={quote(str(fp_hash))}&expiresAt={quote(str(expires))}"
            )
            sr = await api_request(session, "freebuff.com", path, method="GET")
            if sr["status"] == 200 and "user" in sr["data"]:
                print()
                user = sr["data"]["user"]
                append_account(user)
                log("登录成功并已加入账号池！", "success")
                print(f"  用户: {user.get('name')} ({user.get('email')})")
                return
        except Exception as e:
            log(f"轮询出错: {e}", "error")
        await asyncio.sleep(POLL_INTERVAL_S)

    raise RuntimeError("登录超时")


# ============ Freebuff API ============

async def create_agent_run(session, auth_token, agent_id):
    t = time.time()
    res = await api_request(session, API_BASE, "/api/v1/agent-runs",
                            {"action": "START", "agentId": agent_id}, auth_token)
    ms = int((time.time() - t) * 1000)
    if res["status"] != 200 or "runId" not in res["data"]:
        raise RuntimeError(f"创建 Agent Run 失败: {json.dumps(res['data'])}")
    log(f"创建新 Agent Run: {res['data']['runId']} (耗时 {ms}ms, token={token_fingerprint(auth_token)})")
    return res["data"]["runId"]


async def get_or_create_agent_run(session, auth_token, agent_id):
    key = get_run_cache_key(auth_token, agent_id)
    run_id = run_cache.get(key)
    if run_id:
        return run_id

    run_id = await create_agent_run(session, auth_token, agent_id)
    run_cache[key] = run_id
    return run_id


async def finish_agent_run(session, auth_token, run_id):
    await api_request(session, API_BASE, "/api/v1/agent-runs", {
        "action": "FINISH", "runId": run_id, "status": "completed",
        "totalSteps": 1, "directCredits": 0, "totalCredits": 0,
    }, auth_token)


def make_freebuff_body(openai_body, run_id):
    body = dict(openai_body)
    body["codebuff_metadata"] = {
        "run_id": run_id,
        "client_id": f"freebuff-proxy-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}",
        "cost_mode": "free",
    }
    return body


def build_openai_response(run_id, model, choice_data, usage_data=None):
    choice = choice_data or {}
    message = choice.get("message", {})
    resp = {
        "id": f"freebuff-{run_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": message.get("content", ""),
            },
            "finish_reason": choice.get("finish_reason", "stop"),
        }],
        "usage": {
            "prompt_tokens": (usage_data or {}).get("prompt_tokens", 0),
            "completion_tokens": (usage_data or {}).get("completion_tokens", 0),
            "total_tokens": (usage_data or {}).get("total_tokens", 0),
        },
    }
    if message.get("tool_calls"):
        resp["choices"][0]["message"]["tool_calls"] = message["tool_calls"]
    return resp


# ============ 流式转发 ============

async def stream_to_openai_format(session, freebuff_body, auth_token, response, model):
    url = f"https://{API_BASE}/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "Accept": "text/event-stream",
        "User-Agent": "freebuff-proxy/1.0",
    }
    response_id = f"freebuff-{int(time.time() * 1000)}"
    finish_reason = "stop"

    timeout = aiohttp.ClientTimeout(total=120)
    async with session.post(url, json=freebuff_body, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {err}")

        buffer = ""
        async for chunk in resp.content.iter_any():
            buffer += chunk.decode("utf-8", errors="replace")
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                trimmed = line.strip()
                if not trimmed or not trimmed.startswith("data: "):
                    continue
                json_str = trimmed[6:].strip()
                if json_str == "[DONE]":
                    await response.write(b"data: [DONE]\n\n")
                    continue
                try:
                    parsed = json.loads(json_str)
                    delta = (parsed.get("choices") or [{}])[0].get("delta", {})
                    cfr = (parsed.get("choices") or [{}])[0].get("finish_reason")
                    if cfr:
                        finish_reason = cfr

                    delta_obj = {}
                    if delta.get("content"):
                        delta_obj["content"] = delta["content"]
                    if delta.get("tool_calls"):
                        delta_obj["tool_calls"] = delta["tool_calls"]
                    if delta.get("role"):
                        delta_obj["role"] = delta["role"]

                    if delta_obj:
                        openai_chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                        }
                        await response.write(f"data: {json.dumps(openai_chunk)}\n\n".encode())
                except Exception:
                    pass

        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        await response.write(f"data: {json.dumps(final_chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()


# ============ 鉴权中间件 ============

@web.middleware
async def auth_middleware(request, handler):
    """当 PROXY_API_KEY 非空时，对 /v1/* 路由强制验证 Bearer token。"""
    if PROXY_API_KEY and request.path.startswith("/v1/"):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if token != PROXY_API_KEY:
            log(f"鉴权失败: path={request.path}, ip={request.remote}", "warn")
            return web.json_response(
                {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}},
                status=401,
            )
    return await handler(request)


# ============ Responses API 辅助函数 ============

def _responses_parse_input(data: dict) -> list:
    """将 Responses API 的 input 字段转换为 OpenAI messages 列表。
    支持: 字符串 input、{role, content} 数组、typed items。
    """
    raw_input = data.get("input", "")
    instructions = data.get("instructions", "")
    messages = []

    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                item_type = item.get("type", "")
                if item_type == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    })
                elif role in ("user", "assistant", "system", "developer"):
                    if role == "developer":
                        role = "system"
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") in ("input_text", "text"):
                                    text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "".join(text_parts)
                    messages.append({"role": role, "content": content})
    return messages


def _responses_make_base(resp_id: str, model: str, created: float,
                          instructions=None, status="completed") -> dict:
    """构造 Responses API 的基础响应对象。"""
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": None,
        "truncation": "disabled",
        "instructions": instructions,
        "metadata": {},
        "incomplete_details": None,
        "error": None,
        "usage": None,
    }


# ============ 路由处理 ============

async def handle_chat_completion(request):
    start = time.time()
    session = request.app["client_session"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

    model = body.get("model", default_model)
    agent_id = MODEL_TO_AGENT.get(model, "base2-free")

    account, account_idx = pick_next_account()
    auth_token = account["authToken"]

    log(
        "收到请求: "
        f"model={model}, messages={len(body.get('messages', []))}, stream={body.get('stream', False)}, "
        f"account_index={account_idx}, user={account.get('name')}, token={token_fingerprint(auth_token)}"
    )

    try:
        run_id = await get_or_create_agent_run(session, auth_token, agent_id)
    except Exception as e:
        return web.json_response({"error": {"message": str(e)}}, status=500)

    fb_body = make_freebuff_body(body, run_id)

    try:
        if body.get("stream"):
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
            await response.prepare(request)
            await stream_to_openai_format(session, fb_body, auth_token, response, model)
            log(f"请求完成，总耗时 {int((time.time() - start) * 1000)}ms", "success")
            return response
        else:
            res = await api_request(session, API_BASE, "/api/v1/chat/completions", fb_body, auth_token)
            if res["status"] == 200:
                choice = (res["data"].get("choices") or [{}])[0]
                resp = build_openai_response(run_id, model, choice, res["data"].get("usage"))
                log(f"请求完成，总耗时 {int((time.time() - start) * 1000)}ms", "success")
                return web.json_response(resp)
            elif res["status"] in (400, 404):
                log("Agent Run 失效，重新创建...", "warn")
                run_cache.pop(get_run_cache_key(auth_token, agent_id), None)
                run_id = await get_or_create_agent_run(session, auth_token, agent_id)
                fb_body["codebuff_metadata"]["run_id"] = run_id
                retry = await api_request(session, API_BASE, "/api/v1/chat/completions", fb_body, auth_token)
                if retry["status"] == 200:
                    choice = (retry["data"].get("choices") or [{}])[0]
                    resp = build_openai_response(run_id, model, choice, retry["data"].get("usage"))
                    log(f"重试成功，总耗时 {int((time.time() - start) * 1000)}ms", "success")
                    return web.json_response(resp)
                return web.json_response({"error": {"message": retry["data"]}}, status=retry["status"])
            else:
                return web.json_response({"error": {"message": res["data"]}}, status=res["status"])
    except Exception as e:
        log(f"请求失败: {e}", "error")
        return web.json_response({"error": {"message": str(e)}}, status=500)


async def handle_responses(request):
    """OpenAI Responses API 兼容端点 /v1/responses。
    支持 input (str/array)、instructions、stream。
    """
    start = time.time()
    session = request.app["client_session"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

    model = data.get("model", default_model)
    stream = data.get("stream", False)
    instructions = data.get("instructions")
    agent_id = MODEL_TO_AGENT.get(model, "base2-free")

    messages = _responses_parse_input(data)
    if not messages:
        return web.json_response({"error": {"message": "input is required", "type": "invalid_request_error"}}, status=400)

    account, account_idx = pick_next_account()
    auth_token = account["authToken"]

    log(
        f"收到 Responses API 请求: model={model}, msgs={len(messages)}, stream={stream}, "
        f"account_index={account_idx}, user={account.get('name')}, token={token_fingerprint(auth_token)}"
    )

    try:
        run_id = await get_or_create_agent_run(session, auth_token, agent_id)
    except Exception as e:
        return web.json_response({"error": {"message": str(e)}}, status=500)

    fb_body = make_freebuff_body({"model": model, "messages": messages, "stream": True}, run_id)

    resp_id = f"resp_{int(time.time() * 1000)}"
    msg_id = f"msg_{int(time.time() * 1000)}"
    created = time.time()
    msg_chars = sum(len(str(m.get("content", ""))) for m in messages)

    url = f"https://{API_BASE}/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "Accept": "text/event-stream",
        "User-Agent": "freebuff-proxy/1.0",
    }

    try:
        if stream:
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
            await response.prepare(request)

            timeout = aiohttp.ClientTimeout(total=120)
            async with session.post(url, json=fb_body, headers=headers, timeout=timeout) as upstream_resp:
                if upstream_resp.status != 200:
                    err = await upstream_resp.text()
                    raise RuntimeError(f"HTTP {upstream_resp.status}: {err}")

                seq = 0
                full_text_parts = []
                base = _responses_make_base(resp_id, model, created, instructions, "in_progress")

                async def emit(event_type, payload):
                    nonlocal seq
                    data_str = json.dumps({"type": event_type, "sequence_number": seq, **payload}, ensure_ascii=False)
                    await response.write(f"event: {event_type}\ndata: {data_str}\n\n".encode())
                    seq += 1

                await response.write(
                    f"event: response.created\ndata: {json.dumps({'type':'response.created','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n".encode()
                )
                seq += 1
                await response.write(
                    f"event: response.in_progress\ndata: {json.dumps({'type':'response.in_progress','sequence_number':seq,'response':base}, ensure_ascii=False)}\n\n".encode()
                )
                seq += 1

                item_skeleton = {"id": msg_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                await response.write(
                    f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','sequence_number':seq,'output_index':0,'item':item_skeleton}, ensure_ascii=False)}\n\n".encode()
                )
                seq += 1

                part_skeleton = {"type": "output_text", "text": "", "annotations": []}
                await response.write(
                    f"event: response.content_part.added\ndata: {json.dumps({'type':'response.content_part.added','sequence_number':seq,'item_id':msg_id,'output_index':0,'content_index':0,'part':part_skeleton}, ensure_ascii=False)}\n\n".encode()
                )
                seq += 1

                # 读取上游 SSE 并转发文本 delta
                buffer = ""
                async for chunk in upstream_resp.content.iter_any():
                    buffer += chunk.decode("utf-8", errors="replace")
                    lines = buffer.split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        trimmed = line.strip()
                        if not trimmed or not trimmed.startswith("data: "):
                            continue
                        json_str = trimmed[6:].strip()
                        if json_str == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(json_str)
                            delta_content = (parsed.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                            if delta_content:
                                full_text_parts.append(delta_content)
                                delta_evt = json.dumps({
                                    "type": "response.output_text.delta",
                                    "sequence_number": seq,
                                    "item_id": msg_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": delta_content,
                                }, ensure_ascii=False)
                                await response.write(f"event: response.output_text.delta\ndata: {delta_evt}\n\n".encode())
                                seq += 1
                        except Exception:
                            pass

                full_text = "".join(full_text_parts)

                # output_text.done
                done_text_evt = json.dumps({"type": "response.output_text.done", "sequence_number": seq, "item_id": msg_id, "output_index": 0, "content_index": 0, "text": full_text}, ensure_ascii=False)
                await response.write(f"event: response.output_text.done\ndata: {done_text_evt}\n\n".encode())
                seq += 1

                done_part = {"type": "output_text", "text": full_text, "annotations": []}
                done_part_evt = json.dumps({"type": "response.content_part.done", "sequence_number": seq, "item_id": msg_id, "output_index": 0, "content_index": 0, "part": done_part}, ensure_ascii=False)
                await response.write(f"event: response.content_part.done\ndata: {done_part_evt}\n\n".encode())
                seq += 1

                done_item = {"id": msg_id, "type": "message", "role": "assistant", "status": "completed", "content": [done_part]}
                done_item_evt = json.dumps({"type": "response.output_item.done", "sequence_number": seq, "output_index": 0, "item": done_item}, ensure_ascii=False)
                await response.write(f"event: response.output_item.done\ndata: {done_item_evt}\n\n".encode())
                seq += 1

                usage = {
                    "input_tokens": msg_chars // 4,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": len(full_text) // 4,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": (msg_chars + len(full_text)) // 4,
                }
                final = _responses_make_base(resp_id, model, created, instructions, "completed")
                final["output"] = [done_item]
                final["usage"] = usage
                completed_evt = json.dumps({"type": "response.completed", "sequence_number": seq, "response": final}, ensure_ascii=False)
                await response.write(f"event: response.completed\ndata: {completed_evt}\n\n".encode())

            await response.write_eof()
            log(f"Responses API 请求完成，总耗时 {int((time.time() - start) * 1000)}ms", "success")
            return response

        else:
            # 非流式：收集全部内容后构造 Responses API 响应
            timeout = aiohttp.ClientTimeout(total=120)
            content_parts = []
            async with session.post(url, json=fb_body, headers=headers, timeout=timeout) as upstream_resp:
                if upstream_resp.status != 200:
                    err = await upstream_resp.text()
                    # 尝试重建 run
                    if upstream_resp.status in (400, 404):
                        log("Responses API: Agent Run 失效，重新创建...", "warn")
                        run_cache.pop(get_run_cache_key(auth_token, agent_id), None)
                        run_id = await get_or_create_agent_run(session, auth_token, agent_id)
                        fb_body["codebuff_metadata"]["run_id"] = run_id
                        async with session.post(url, json=fb_body, headers=headers, timeout=timeout) as retry_resp:
                            if retry_resp.status != 200:
                                raise RuntimeError(f"HTTP {retry_resp.status}: {await retry_resp.text()}")
                            upstream_resp = retry_resp
                            buffer = ""
                            async for chunk in upstream_resp.content.iter_any():
                                buffer += chunk.decode("utf-8", errors="replace")
                            for line in buffer.split("\n"):
                                trimmed = line.strip()
                                if not trimmed or not trimmed.startswith("data: "):
                                    continue
                                json_str = trimmed[6:].strip()
                                if json_str == "[DONE]":
                                    continue
                                try:
                                    parsed = json.loads(json_str)
                                    c = (parsed.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                                    if c:
                                        content_parts.append(c)
                                except Exception:
                                    pass
                    else:
                        raise RuntimeError(f"HTTP {upstream_resp.status}: {err}")
                else:
                    buffer = ""
                    async for chunk in upstream_resp.content.iter_any():
                        buffer += chunk.decode("utf-8", errors="replace")
                    for line in buffer.split("\n"):
                        trimmed = line.strip()
                        if not trimmed or not trimmed.startswith("data: "):
                            continue
                        json_str = trimmed[6:].strip()
                        if json_str == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(json_str)
                            c = (parsed.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                            if c:
                                content_parts.append(c)
                        except Exception:
                            pass

            full_text = "".join(content_parts)
            output_item = {
                "id": msg_id, "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": full_text, "annotations": []}],
            }
            usage = {
                "input_tokens": msg_chars // 4,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": len(full_text) // 4,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": (msg_chars + len(full_text)) // 4,
            }
            result = _responses_make_base(resp_id, model, created, instructions, "completed")
            result["output"] = [output_item]
            result["usage"] = usage
            log(f"Responses API 请求完成，总耗时 {int((time.time() - start) * 1000)}ms", "success")
            return web.json_response(result)

    except Exception as e:
        log(f"Responses API 请求失败: {e}", "error")
        return web.json_response({"error": {"message": str(e)}}, status=500)


async def handle_models(request):
    models = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "freebuff"} for m in MODEL_TO_AGENT]
    return web.json_response({"object": "list", "data": models})


async def handle_reset_run(request):
    run_cache.clear()
    log("Agent Run 缓存已清除")
    return web.json_response({"status": "cleared"})


async def handle_health(request):
    account_brief = [
        {
            "index": i,
            "name": acc.get("name"),
            "email": acc.get("email"),
            "token": token_fingerprint(acc.get("authToken")),
        }
        for i, acc in enumerate(token_pool)
    ]
    return web.json_response({
        "status": "ok",
        "model": default_model,
        "accounts": account_brief,
        "accountCount": len(token_pool),
        "nextAccountIndex": next_token_index,
        "cachedRunCount": len(run_cache),
    })


# ============ 主入口 ============

async def main(manage_accounts=False, api_key=""):
    global token_pool, PROXY_API_KEY

    # 命令行 --api-key 优先级高于环境变量
    if api_key:
        PROXY_API_KEY = api_key

    session = aiohttp.ClientSession(trust_env=True)

    try:
        token_pool = load_accounts()

        if token_pool:
            log(f"已加载账号池: {len(token_pool)} 个")
            for i, acc in enumerate(token_pool):
                log(
                    f"  [{i}] {acc.get('name')} <{acc.get('email')}> token={token_fingerprint(acc.get('authToken'))}"
                )

        if manage_accounts:
            if not token_pool:
                log("本地未检测到可用 token，先登录第 1 个账号...")
                await do_login(session)
            await prompt_add_accounts_on_startup(session)
        elif not token_pool:
            raise RuntimeError("没有可用账号。默认模式不管理账号，请先配置 credentials.json、设置 FREEBUFF_AUTH_TOKEN(S) 或使用 --manage-accounts")

        if not token_pool:
            raise RuntimeError("没有可用账号，无法启动代理")

        # 预热：为池中每个账号的默认 agent 创建 run
        log("预热：为账号池创建默认 Agent Run...")
        default_agent = MODEL_TO_AGENT.get(default_model, "base2-free")
        warmed = 0
        for acc in token_pool:
            auth_token = acc.get("authToken")
            if not auth_token:
                continue
            try:
                run_id = await create_agent_run(session, auth_token, default_agent)
                run_cache[get_run_cache_key(auth_token, default_agent)] = run_id
                warmed += 1
            except Exception as e:
                log(
                    f"预热失败: user={acc.get('name')}, token={token_fingerprint(auth_token)}, err={e}",
                    "warn"
                )

        if warmed == 0:
            raise RuntimeError("账号池预热失败：没有可用 Agent Run")

        log(f"预热完成，已缓存 {warmed} 个默认 Agent Run", "success")

        app = web.Application(middlewares=[auth_middleware])
        app["client_session"] = session
        app.router.add_post("/v1/chat/completions", handle_chat_completion)
        app.router.add_post("/v1/responses", handle_responses)
        app.router.add_get("/v1/models", handle_models)
        app.router.add_post("/v1/reset-run", handle_reset_run)
        app.router.add_get("/health", handle_health)
        app.router.add_get("/", handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", LOCAL_PORT)
        await site.start()

        print(f"""
{C['B']}{C['C']}
╔══════════════════════════════════════════════════════════════╗
║          Freebuff OpenAI Proxy (Python / Multi-Token)       ║
║             本地端口: {LOCAL_PORT}                          ║
║               账号池轮询已启用                               ║
╚══════════════════════════════════════════════════════════════╝
{C['R']}""")
        log(f"代理地址: http://localhost:{LOCAL_PORT}/v1/chat/completions")
        log(f"Responses: http://localhost:{LOCAL_PORT}/v1/responses")
        log(f"模型列表: http://localhost:{LOCAL_PORT}/v1/models")
        log(f"重置缓存: http://localhost:{LOCAL_PORT}/v1/reset-run (POST)")
        log(f"健康检查: http://localhost:{LOCAL_PORT}/health")
        if PROXY_API_KEY:
            log(f"API 鉴权: 已启用 (Bearer {token_fingerprint(PROXY_API_KEY)})", "success")
        else:
            log("API 鉴权: 未启用（所有请求均可访问）", "warn")
        print(f"\n{C['Y']}可用模型:{C['R']}")
        for m, a in MODEL_TO_AGENT.items():
            print(f"  {C['C']}{m}{C['R']} → {a}")
        print(f"\n{C['Y']}账号池:{C['R']}")
        for i, acc in enumerate(token_pool):
            print(
                f"  [{i}] {C['C']}{acc.get('name')}{C['R']} <{acc.get('email')}> "
                f"token={token_fingerprint(acc.get('authToken'))}"
            )
        print(f"\n{C['G']}等待请求... (Ctrl+C 关闭){C['R']}\n")

        stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

        if run_cache:
            log("关闭代理，结束全部缓存 Agent Run...")
            for (auth_token, _agent_id), run_id in list(run_cache.items()):
                try:
                    await finish_agent_run(session, auth_token, run_id)
                except Exception:
                    pass
            log("Agent Run 清理完成", "success")

        await runner.cleanup()
    finally:
        await session.close()


if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(main(manage_accounts=args.manage_accounts, api_key=args.api_key))
    except KeyboardInterrupt:
        pass