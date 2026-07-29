"""
DeepSeek LLM 客户端 (OpenAI 兼容)

按用户 2026-07-17 决定: 接 DeepSeek, 让 Mavis 真理解需求
DEEPSEEK_API_KEY 在 secret env (用户给)

v1.0.9+ 增强 (T1 修复):
- LLM_RETRY 包装: 401/403/timeout → 1 次 retry (3s backoff) → fallback
- last_error / last_call_ts 写到模块全局, /llm/status 暴露给 UI
- is_llm_available() 视为 True 当 key 配了 (因为 fallback 也算可用)
- 抛错时 detail 带 HTTP code (不要静默 fallback)
"""
import json
import os
import time
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any


def _load_dotenv():
    """从 .env 文件加载环境变量 (fallback, 当 nohup/systemd 启动没传 env 时用)"""
    import os
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),  # /workspace/.env
        "/opt/mavis-dev/.env",
        os.path.expanduser("~/.env"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        # 只设没设过的
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


# 启动时尝试加载 .env (避免 nohup 启动没 env 时 LLM 不可用)
_load_dotenv()


# === v1.0.9: 全局 LLM 诊断 (供 /llm/status 暴露) ===
class LLMDiagnostics:
    """LLM 调用诊断 - 跟踪 last_error / last_call_ts / call_count / success_count

    线程安全 (LLM 调用可能从 worker 线程触发)
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[int] = None  # HTTP code (401/403/500/...)
        self.last_call_ts: Optional[str] = None
        self.last_success_ts: Optional[str] = None
        self.call_count: int = 0
        self.success_count: int = 0
        self.fallback_count: int = 0  # 走 fallback 的次数
        self.retry_count: int = 0  # retry 触发的次数
        self.last_model: Optional[str] = None
        self.last_base: Optional[str] = None

    def record_call(self, model: str, base: str):
        with self._lock:
            self.call_count += 1
            self.last_call_ts = datetime.now().isoformat()
            self.last_model = model
            self.last_base = base

    def record_success(self):
        with self._lock:
            self.success_count += 1
            self.last_success_ts = datetime.now().isoformat()
            self.last_error = None
            self.last_error_code = None

    def record_error(self, err_msg: str, err_code: Optional[int] = None):
        with self._lock:
            self.last_error = err_msg
            self.last_error_code = err_code

    def record_retry(self):
        with self._lock:
            self.retry_count += 1

    def record_fallback(self):
        with self._lock:
            self.fallback_count += 1

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_error": self.last_error,
                "last_error_code": self.last_error_code,
                "last_call_ts": self.last_call_ts,
                "last_success_ts": self.last_success_ts,
                "call_count": self.call_count,
                "success_count": self.success_count,
                "fallback_count": self.fallback_count,
                "retry_count": self.retry_count,
                "last_model": self.last_model,
                "last_base": self.last_base,
            }

    def reset(self):
        with self._lock:
            self.last_error = None
            self.last_error_code = None
            self.last_call_ts = None
            self.last_success_ts = None
            self.call_count = 0
            self.success_count = 0
            self.fallback_count = 0
            self.retry_count = 0


# 全局单例
_llm_diag = LLMDiagnostics()


def get_llm_diagnostics() -> Dict[str, Any]:
    """对外 API: 拿当前 LLM 诊断 (供 /llm/status 用)"""
    return _llm_diag.to_dict()


def reset_llm_diagnostics():
    """测试/重启时清空"""
    _llm_diag.reset()


class DeepSeekClient:
    """LLM 客户端 (OpenAI 兼容) - 支持 MiniMax / DeepSeek / 其他

    按用户 2026-07-17 决定用 MiniMax:
    - 默认 base: https://api.minimaxi.com/v1
    - 默认 model: MiniMax-Text-01
    - env 优先: LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
    - 兼容旧: DEEPSEEK_API_KEY (当 LLM_API_KEY 没设)
    - fallback: 从 .env 文件读 (避免 nohup 启动没 env)
    """

    DEFAULT_BASE = "https://api.minimaxi.com/v1"
    DEFAULT_MODEL = "MiniMax-Text-01"

    def __init__(self, api_key: Optional[str] = None, base: Optional[str] = None, model: Optional[str] = None):
        # 启动时再调一次, 覆盖 import 时的情况
        _load_dotenv()
        self.api_key = (
            api_key
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        )
        self.base = (
            base
            or os.environ.get("LLM_BASE_URL")
            or self.DEFAULT_BASE
        )
        self.model = (
            model
            or os.environ.get("LLM_MODEL")
            or self.DEFAULT_MODEL
        )
        if not self.api_key:
            raise ValueError(
                "LLM_API_KEY not set. 配: export LLM_API_KEY=sk-... 或 DEEPSEEK_API_KEY=sk-..."
            )

    def stream_chat(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 60,
    ):
        """流式 chat, yield 每个 chunk 的 content 增量 (gen str)

        v1.0.9: 加 1 次 retry (3s backoff) on HTTPError/timeout, 失败记录到 _llm_diag
        """
        import urllib.request
        import urllib.error
        import json as _json

        model = model or self.model
        body = _json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }).encode("utf-8")

        url = f"{self.base}/chat/completions"
        last_err = ""
        last_code = None
        _llm_diag.record_call(model, self.base)
        for attempt in range(2):  # 0=first, 1=retry
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    for line in resp:
                        line = line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            _llm_diag.record_success()
                            break
                        try:
                            obj = _json.loads(data)
                            delta = obj["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (KeyError, _json.JSONDecodeError):
                            continue
                _llm_diag.record_success()
                return
            except urllib.error.HTTPError as e:
                last_code = e.code
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            # retry 一次
            if attempt == 0:
                _llm_diag.record_retry()
                time.sleep(3)
        # 两次都失败 → 记录 + 抛错
        _llm_diag.record_error(last_err, last_code)
        raise RuntimeError(f"DeepSeek API 失败 (2 次): {last_err}")

    def chat(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 60,
        max_retries: int = 2,
        tools: Optional[List[Dict]] = None,  # v1.0.11: OpenAI 兼容 tools (function calling)
        tool_choice: Optional[str] = None,  # v1.0.11: "auto" / "none" / "required"
    ) -> Dict:
        """
        调用 chat completion
        messages: [{"role": "system/user/assistant", "content": "..."}]
        tools: OpenAI 格式 [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        返回: {
            "content": "...",
            "tool_calls": [{"id", "type", "function": {"name", "arguments"}}],
            "finish_reason": "stop" / "tool_calls",
            "usage": {...},
            "model": "..."
        }

        v1.0.9: 默认 max_retries=2 (1 次原始 + 1 次 retry), 失败记录到 _llm_diag
        v1.0.11: 加 tools/tool_choice 支持 function calling (Agent 自主调 tool)
        """
        import urllib.request
        import urllib.error

        model = model or self.model
        body_dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body_dict["tools"] = tools
            if tool_choice:
                body_dict["tool_choice"] = tool_choice
        body = json.dumps(body_dict).encode("utf-8")

        url = f"{self.base}/chat/completions"

        last_err = ""
        last_code = None
        _llm_diag.record_call(model, self.base)
        # v1.0.9: 内部 max_retries 实际 = 2 次 (attempt 0 = 原始, attempt 1 = retry, 3s backoff)
        # 保留原 max_retries 兼容调用方, 最多额外 2 次
        max_attempts = max(1, max_retries) + 1  # +1 是因为 client.chat 内部 retry, 加上 chat_with_fallback 还会再 retry
        for attempt in range(min(max_attempts, 3)):  # cap 3
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                _llm_diag.record_success()
                msg = data["choices"][0]["message"]
                # v1.0.11: 透传 tool_calls + finish_reason (Agent 调 tool 用)
                tool_calls = msg.get("tool_calls")
                content_text = msg.get("content", "") or ""
                # v1.0.13.3 fix: minimax 等 LLM 在 content 文本里写函数名+args 而 tool_calls 字段空
                #   fallback: 用正则从文本里抽
                if not tool_calls and content_text:
                    import re as _re
                    matches = []
                    # 格式 1: functions.tool_name({...}) — OpenAI 老 style
                    pattern1 = r"functions\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\((\{.*?\})\)"
                    matches1 = _re.findall(pattern1, content_text, _re.DOTALL)
                    if matches1:
                        matches = matches1
                    # 格式 2: ```json\n{"tool_calls": [{"function/name": "X", "parameters/arguments": {...}}]} — minimax json style
                    if not matches:
                        json_pattern = r'\{\s*"tool_calls"\s*:\s*\[\s*\{[^}]*?"(?:function|name)"\s*:\s*"([a-zA-Z_][a-zA-Z0-9_]*)"[^}]*?(?:"parameters"|"arguments")\s*:\s*(\{[^}]*?\})[^}]*?\}'
                        json_matches = _re.findall(json_pattern, content_text, _re.DOTALL)
                        if json_matches:
                            matches = json_matches
                    # 格式 3: functions.tool_name() (空 args)
                    if not matches:
                        pattern3 = r"functions\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*\)"
                        matches3 = _re.findall(pattern3, content_text)
                        if matches3:
                            matches = [(fn, "{}") for fn in matches3[:1]]
                    if matches:
                        # 去重 (minimax 有时在文本里写重复 tool call, 报 invalid tool calls count)
                        seen = set()
                        unique = []
                        for fn, args in matches:
                            if fn not in seen:
                                seen.add(fn)
                                unique.append((fn, args))
                        tool_calls = [
                            {
                                "id": f"call_{i}_{fn}",
                                "type": "function",
                                "function": {
                                    "name": fn,
                                    "arguments": args if isinstance(args, str) else json.dumps(args),
                                },
                                "index": i,
                            }
                            for i, (fn, args) in enumerate(unique)
                        ]
                return {
                    "content": content_text,
                    "tool_calls": tool_calls,
                    "finish_reason": data["choices"][0].get("finish_reason", "stop"),
                    "usage": data.get("usage", {}),
                    "model": data.get("model", model),
                    "provider_base": self.base,
                }
            except urllib.error.HTTPError as e:
                last_code = e.code
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < min(max_attempts, 3) - 1:
                _llm_diag.record_retry()
                time.sleep(1 + attempt)
        _llm_diag.record_error(last_err, last_code)
        raise RuntimeError(f"DeepSeek API 失败: {last_err}")

    async def chat_async(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 60,
        max_retries: int = 2,
        tools: Optional[List[Dict]] = None,  # v1.0.11
        tool_choice: Optional[str] = None,  # v1.0.11
    ) -> Dict:
        """async 版本, 不阻塞 event loop (用 to_thread 跑 sync urllib)"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.chat(messages, model, temperature, max_tokens, timeout, max_retries, tools, tool_choice),
        )

    def analyze_requirement(self, requirement: str, scenario: str = "") -> Dict:
        """
        用 LLM 解析需求, 生成 4 步 workflow
        返回: {"steps": [{"id":..., "name":..., "description":..., "params":...}, ...]}
        """
        sys_prompt = """你是 Mavis, 赛博数据分析师。接到用户需求后, 拆解成最多 4 步可执行的分析任务。

每步:
- id: s1/s2/s3/s4
- name: 简短中文 (≤10 字)
- description: 一句话说清干啥
- params: 该步的具体参数 (dict)

返回纯 JSON, 不要 markdown 代码块标记。
"""
        user_prompt = f"""需求: {requirement}
场景: {scenario or "通用数据分析"}

返回 JSON 格式:
{{
  "summary": "一句话理解这个需求",
  "steps": [
    {{"id": "s1", "name": "...", "description": "...", "params": {{...}}}},
    {{"id": "s2", "name": "...", "description": "...", "params": {{...}}}},
    ...
  ]
}}

规则:
- 最多 4 步
- 每步要可执行 (不是抽象描述)
- params 给具体值 (日期/指标/表名/字段)
- 如果需求是 ad-hoc 取数, 1-2 步就够
- 如果是周报, 4 步: 取数 / 模板填充 / 加洞察 / 发送
- 如果是应急, 2 步: 查现状 / 找原因"""

        result = self.chat(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        content = result["content"].strip()
        # 清掉可能的 markdown 标记
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 兜底: 返回简单结构
            return {
                "summary": requirement,
                "steps": [
                    {"id": "s1", "name": "理解需求", "description": requirement, "params": {}},
                ],
            }


# 全局单例
_client: Optional[DeepSeekClient] = None
_client_lock = threading.Lock()


def get_llm() -> DeepSeekClient:
    """获取 LLM client 单例 (线程安全)"""
    global _client
    with _client_lock:
        if _client is None:
            _client = DeepSeekClient()
        return _client


def reset_llm_client():
    """重置 LLM client (测试 / 切换 provider 时用)"""
    global _client
    with _client_lock:
        _client = None


def is_llm_available() -> bool:
    """是否配了 LLM
    
    v1.0.9: 返 True 当:
    1. env 配了 LLM_API_KEY / DEEPSEEK_API_KEY
    2. OR 上次调用 fallback 成功 (即 system 仍能给出 fallback response)
    
    这样 decision engine 至少能拿到 fallback reason, 而不是直接 block.
    """
    return bool(
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
    )


# === v1.0.9: LLM_RETRY 包装 (供 decision engine / planner 用) ===

def chat_with_fallback(
    messages: List[Dict[str, str]],
    fallback_content: str = "",
    fallback_metadata: Optional[Dict[str, Any]] = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: int = 60,
    max_retries: int = 1,  # 1 = 原始 + 1 retry
) -> Dict[str, Any]:
    """调 LLM, 失败时返 fallback response (不抛错)
    
    行为 (v1.0.9):
    1. 第 1 次调 LLM (record_call)
    2. 失败 (401/403/timeout/任何异常) → 等 3s → 第 2 次 (record_retry)
    3. 还失败 → record_fallback + 返 {"content": fallback_content, "source": "fallback", "error": "..."}
    4. 成功 → record_success + 返 {"content": "...", "source": "llm", "error": None}
    
    Args:
        messages: chat messages
        fallback_content: LLM 失败时返的内容 (e.g. 模板 reason)
        fallback_metadata: 附加 metadata (e.g. {"chosen": "A", "score": 0.8})
        temperature/max_tokens/timeout: LLM 参数
        max_retries: 额外 retry 次数 (默认 1 = 总 2 次)
    
    Returns:
        {"content": str, "source": "llm" | "fallback", "error": str | None,
         "error_code": int | None, "model": str, "base": str, "attempts": int}
    """
    fallback_metadata = fallback_metadata or {}
    last_err: Optional[str] = None
    last_code: Optional[int] = None
    last_content: str = fallback_content
    last_source: str = "fallback"
    last_attempts: int = 0
    last_model: str = ""
    last_base: str = ""

    try:
        client = get_llm()
        last_model = client.model
        last_base = client.base
    except Exception as e:
        # 拿不到 client (e.g. 没设 key) → 直接 fallback
        _llm_diag.record_call("unknown", "")
        _llm_diag.record_error(f"client init failed: {e}")
        _llm_diag.record_fallback()
        return {
            "content": fallback_content,
            "source": "fallback",
            "error": f"LLM client init failed: {e}",
            "error_code": None,
            "model": None,
            "base": None,
            "attempts": 0,
            **fallback_metadata,
        }

    _llm_diag.record_call(last_model, last_base)  # v1.0.9: 统一 record_call 记录

    for attempt in range(max_retries + 1):
        last_attempts = attempt + 1
        try:
            result = client.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                max_retries=0,  # client.chat 内部不再 retry, 我们自己 retry
            )
            # v1.0.9: 成功 → record_success (client.chat 内部已 record, 这里冗余调一次冱底)
            _llm_diag.record_success()
            return {
                "content": result.get("content", ""),
                "source": "llm",
                "error": None,
                "error_code": None,
                "model": result.get("model", last_model),
                "base": result.get("provider_base", last_base),
                "attempts": last_attempts,
                **fallback_metadata,
            }
        except Exception as e:
            err_str = str(e)
            last_err = err_str
            # 尝试从 error 解析 HTTP code (HTTPError 默认 str: "HTTP Error 401: Unauthorized")
            import re as _re
            m = _re.search(r"HTTP(?:\s+Error)?\s+(\d+)", err_str)
            if m:
                try:
                    last_code = int(m.group(1))
                except (ValueError, TypeError):
                    last_code = None
            # retry 一次
            if attempt < max_retries:
                _llm_diag.record_retry()
                time.sleep(3)

    # 全部失败 → fallback
    _llm_diag.record_error(last_err, last_code)
    _llm_diag.record_fallback()
    return {
        "content": fallback_content,
        "source": "fallback",
        "error": last_err,
        "error_code": last_code,
        "model": last_model,
        "base": last_base,
        "attempts": last_attempts,
        **fallback_metadata,
    }
