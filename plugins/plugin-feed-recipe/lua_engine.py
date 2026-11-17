"""Declarative Lua table parser and sandboxed Lua 5.4 subprocess runtime."""

from __future__ import annotations

import asyncio
import json
import shutil
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextParagraph

if __package__:
    from .cookie_bridge import CookieBridge
    from .html_css import (
        HtmlDocument,
        decode_html_entities,
        extract_all_values,
        extract_readability_html,
        extract_value,
        html_to_paragraphs,
        node_attribute,
        node_inner_html,
        node_inner_text,
        select_nodes,
        strip_html_tags_to_text,
    )
    from .http_client import HttpRequest, HttpTransport
    from .recipe_spec import RecipeSpec, parse_recipe_dict, recipe_to_dict
    from .security import FeedRecipeError, resolve_url
    from .xml_feed import parse_xml_feed, query_xml_xpath
else:
    from cookie_bridge import CookieBridge
    from html_css import (
        HtmlDocument,
        decode_html_entities,
        extract_all_values,
        extract_readability_html,
        extract_value,
        html_to_paragraphs,
        node_attribute,
        node_inner_html,
        node_inner_text,
        select_nodes,
        strip_html_tags_to_text,
    )
    from http_client import HttpRequest, HttpTransport
    from recipe_spec import RecipeSpec, parse_recipe_dict, recipe_to_dict
    from security import FeedRecipeError, resolve_url
    from xml_feed import parse_xml_feed, query_xml_xpath

DEFAULT_INSTRUCTION_LIMIT = 250_000
_MAX_RPC_ROUNDS = 12

# Self-contained pure Lua 5.4 sandbox driver (no os/io/package/dofile/loadfile/debug exposed to recipe)
_LUA_SANDBOX_DRIVER = r"""
local _sethook = debug.sethook
local _load = load
local _orig_env = _G

-- Minimal JSON encoder/decoder in pure Lua 5.4
local json_lib = {}
local function escape_str(s)
  local rep = { ['\\'] = '\\\\', ['"'] = '\\"', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
  return '"' .. s:gsub('[\\"\b\f\n\r\t]', rep):gsub('[%z\1-\31]', function(c)
    return string.format('\\u%04x', string.byte(c))
  end) .. '"'
end
local function is_array(t)
  local max_i, count = 0, 0
  for k, _ in pairs(t) do
    if type(k) ~= 'number' or k < 1 or math.floor(k) ~= k then return false, 0 end
    if k > max_i then max_i = k end
    count = count + 1
  end
  return count > 0 and max_i == count, max_i
end
function json_lib.encode(v)
  local tv = type(v)
  if v == nil then return 'null'
  elseif tv == 'boolean' then return v and 'true' or 'false'
  elseif tv == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    return tostring(v)
  elseif tv == 'string' then return escape_str(v)
  elseif tv == 'table' then
    local arr, n = is_array(v)
    if arr then
      local out = {}
      for i = 1, n do out[i] = json_lib.encode(v[i]) end
      return '[' .. table.concat(out, ',') .. ']'
    else
      local out = {}
      for k, val in pairs(v) do
        if type(val) ~= 'function' and (type(k) == 'string' or type(k) == 'number') then
          out[#out + 1] = escape_str(tostring(k)) .. ':' .. json_lib.encode(val)
        end
      end
      return '{' .. table.concat(out, ',') .. '}'
    end
  end
  return 'null'
end
function json_lib.decode(str)
  if type(str) ~= 'string' then return nil, 'Invalid JSON' end
  local pos, len = 1, #str
  local function skip_ws()
    while pos <= len and str:sub(pos, pos):match('%s') do pos = pos + 1 end
  end
  local parse_val
  local function parse_str()
    pos = pos + 1
    local out = {}
    while pos <= len do
      local c = str:sub(pos, pos)
      if c == '"' then pos = pos + 1; return table.concat(out) end
      if c == '\\' then
        pos = pos + 1
        local esc = str:sub(pos, pos)
        local map = { ['"']='"', ['\\']='\\', ['/']='/', b='\b', f='\f', n='\n', r='\r', t='\t' }
        if map[esc] then out[#out+1] = map[esc]; pos = pos + 1
        elseif esc == 'u' then
          local hex = str:sub(pos+1, pos+4)
          local cp = tonumber(hex, 16)
          if not cp then error('bad unicode') end
          out[#out+1] = utf8.char(cp)
          pos = pos + 5
        else out[#out+1] = esc; pos = pos + 1 end
      else
        out[#out+1] = c; pos = pos + 1
      end
    end
    error('unterminated string')
  end
  local function parse_arr()
    pos = pos + 1; local arr = {}; skip_ws()
    if str:sub(pos, pos) == ']' then pos = pos + 1; return arr end
    while pos <= len do
      arr[#arr+1] = parse_val()
      skip_ws()
      local c = str:sub(pos, pos)
      if c == ']' then pos = pos + 1; return arr end
      if c ~= ',' then error('expected , or ]') end
      pos = pos + 1
    end
    error('unterminated array')
  end
  local function parse_obj()
    pos = pos + 1; local obj = {}; skip_ws()
    if str:sub(pos, pos) == '}' then pos = pos + 1; return obj end
    while pos <= len do
      skip_ws()
      if str:sub(pos, pos) ~= '"' then error('expected string key') end
      local k = parse_str()
      skip_ws()
      if str:sub(pos, pos) ~= ':' then error('expected :') end
      pos = pos + 1
      obj[k] = parse_val()
      skip_ws()
      local c = str:sub(pos, pos)
      if c == '}' then pos = pos + 1; return obj end
      if c ~= ',' then error('expected , or }') end
      pos = pos + 1
    end
    error('unterminated object')
  end
  parse_val = function()
    skip_ws()
    local c = str:sub(pos, pos)
    if c == '"' then return parse_str()
    elseif c == '{' then return parse_obj()
    elseif c == '[' then return parse_arr()
    elseif str:sub(pos, pos+3) == 'true' then pos = pos + 4; return true
    elseif str:sub(pos, pos+4) == 'false' then pos = pos + 5; return false
    elseif str:sub(pos, pos+3) == 'null' then pos = pos + 4; return nil
    else
      local num = str:match('^-?%d+%.?%d*[eE]?[+-]?%d*', pos)
      if not num or num == '' then error('invalid json at ' .. pos) end
      pos = pos + #num
      return tonumber(num)
    end
  end
  local ok, res = pcall(parse_val)
  if not ok then return nil, 'Invalid JSON' end
  return res
end

local raw_in = io.read('*a')
local ok_in, req = pcall(json_lib.decode, raw_in)
if not ok_in or type(req) ~= 'table' then
  io.write(json_lib.encode({ ok = false, error = 'Invalid driver input' }))
  return
end

local cache = req.cache or {}
local seq = 0
local function host_rpc(op, args)
  seq = seq + 1
  local key = tostring(seq) .. ':' .. op .. ':' .. json_lib.encode(args)
  if cache[key] ~= nil then
    return table.unpack(cache[key])
  end
  error('__OMNIDESK_NEED__:' .. json_lib.encode({ key = key, op = op, args = args }))
end

local text_lib = {
  trim = function(s) return tostring(s or ''):gsub('^%s+', ''):gsub('%s+$', '') end,
  url_encode = function(s)
    return (tostring(s or ''):gsub('([^%w%-%_%.%~])', function(c)
      return string.format('%%%02X', string.byte(c))
    end))
  end,
  decode_entities = function(s) return host_rpc('text.decode_entities', { tostring(s or '') }) end,
  resolve_url = function(base, rel) return host_rpc('text.resolve_url', { tostring(base or ''), tostring(rel or '') }) end,
}

local sandbox_env = {
  assert = assert, error = error, ipairs = ipairs, next = next, pairs = pairs,
  pcall = pcall, xpcall = xpcall, select = select, tonumber = tonumber, tostring = tostring,
  type = type, math = math, string = string, table = table, utf8 = utf8,
  json = {
    decode = function(s) return json_lib.decode(s) end,
    encode = function(v) return json_lib.encode(v) end,
  },
  text = text_lib,
  http = {
    get = function(url, opts) return host_rpc('http.get', { url, opts }) end,
    post = function(url, body, opts) return host_rpc('http.post', { url, body, opts }) end,
  },
  cookies = {
    status = function(url) return host_rpc('cookies.status', { url }) end,
  },
  html = {
    select = function(h, s) return host_rpc('html.select', { h, s }) end,
    extract = function(h, s) return host_rpc('html.extract', { h, s }) end,
    extract_all = function(h, s) return host_rpc('html.extract_all', { h, s }) end,
    readability = function(h, p) return host_rpc('html.readability', { h, p or '' }) end,
    paragraphs = function(h, b) return host_rpc('html.paragraphs', { h, b or '' }) end,
    strip = function(h) return host_rpc('html.strip', { h }) end,
  },
  xml = {
    parse_feed = function(x, m) return host_rpc('xml.parse_feed', { x, m or 30 }) end,
    xpath = function(x, xp) return host_rpc('xml.xpath', { x, xp }) end,
  },
}
sandbox_env._G = sandbox_env

local chunk, load_err = _load(req.script or '', req.source_name or 'recipe.lua', 't', sandbox_env)
if not chunk then
  io.write(json_lib.encode({ ok = false, error = tostring(load_err) }))
  return
end

local limit = tonumber(req.instruction_limit) or 250000
local function run_sandboxed()
  _sethook(function() error('Lua recipe exceeded safety instruction limit') end, '', limit)
  local tbl = chunk()
  if type(tbl) ~= 'table' then error('Lua recipe must return a recipe table') end
  local mode = req.mode or 'load'
  if mode == 'load' then
    local meta = {}
    for k, v in pairs(tbl) do
      if type(v) == 'function' then
        if k == 'transform_item' then meta._has_transform_item = true
        elseif k == 'fetch_feed' then meta._has_fetch_feed = true
        elseif k == 'fetch_article' then meta._has_fetch_article = true
        end
      else
        meta[k] = v
      end
    end
    return meta
  elseif mode == 'transform_item' then
    if type(tbl.transform_item) == 'function' then
      local out = tbl.transform_item(req.item)
      if type(out) == 'table' then return out end
    end
    return req.item
  elseif mode == 'fetch_feed' then
    if type(tbl.fetch_feed) ~= 'function' then error('Lua recipe missing fetch_feed function') end
    local res = tbl.fetch_feed(req.spec, req.query or '')
    if type(res) ~= 'table' then error('fetch_feed must return a table array') end
    return res
  elseif mode == 'fetch_article' then
    if type(tbl.fetch_article) ~= 'function' then error('Lua recipe missing fetch_article function') end
    local res = tbl.fetch_article(req.spec, req.item)
    if type(res) ~= 'table' then error('fetch_article must return a table') end
    return res
  end
  error('Unknown mode')
end

local ok_run, out_or_err = pcall(run_sandboxed)
_sethook()
if not ok_run then
  local msg = tostring(out_or_err)
  local marker = msg:match('__OMNIDESK_NEED__:(.+)$')
  if marker then
    io.write(json_lib.encode({ ok = false, need_rpc = json_lib.decode(marker) }))
    return
  end
  io.write(json_lib.encode({ ok = false, error = msg }))
  return
end
io.write(json_lib.encode({ ok = true, result = out_or_err }))
"""


def _skip_lua_ws_and_comments(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        if s[i : i + 2] == "--":
            if s[i + 2 : i + 4] == "[[":
                end = s.find("]]", i + 4)
                i = n if end == -1 else end + 2
            else:
                nl = s.find("\n", i + 2)
                i = n if nl == -1 else nl + 1
            continue
        break
    return i


class _PureLuaTableParser:
    """Fast pure-Python parser for literal declarative `return { ... }` Lua recipes."""

    def __init__(self, src: str) -> None:
        self.s = src
        self.n = len(src)
        self.i = 0

    def parse(self) -> dict[str, Any] | None:
        self.i = _skip_lua_ws_and_comments(self.s, 0)
        if not self.s[self.i :].startswith("return"):
            return None
        after_ret = self.i + 6
        if after_ret < self.n and (self.s[after_ret].isalnum() or self.s[after_ret] == "_"):
            return None
        self.i = _skip_lua_ws_and_comments(self.s, after_ret)
        if self.i >= self.n or self.s[self.i] != "{":
            return None
        val = self._parse_table()
        self.i = _skip_lua_ws_and_comments(self.s, self.i)
        if self.i < self.n and self.s[self.i] == ";":
            self.i = _skip_lua_ws_and_comments(self.s, self.i + 1)
        if self.i != self.n or not isinstance(val, dict):
            return None
        return val

    def _parse_string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f"}
        while self.i < self.n:
            c = self.s[self.i]
            if c == quote:
                self.i += 1
                return "".join(out)
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    raise ValueError("unterminated escape")
                nxt = self.s[self.i]
                out.append(escapes.get(nxt, nxt))
                self.i += 1
            else:
                out.append(c)
                self.i += 1
        raise ValueError("unterminated string")

    def _parse_long_string(self) -> str | None:
        if self.s[self.i] != "[":
            return None
        j = self.i + 1
        while j < self.n and self.s[j] == "=":
            j += 1
        if j >= self.n or self.s[j] != "[":
            return None
        eqs = "=" * (j - self.i - 1)
        close = f"]{eqs}]"
        start = j + 1
        if start < self.n and self.s[start] == "\n":
            start += 1
        end = self.s.find(close, start)
        if end == -1:
            raise ValueError("unterminated long string")
        self.i = end + len(close)
        return self.s[start:end]

    def _skip_function_literal(self) -> str:
        # Must start at `function`
        start = self.i
        self.i += 8
        depth = 1
        block_open = {"function", "if", "for", "while"}
        while self.i < self.n and depth > 0:
            self.i = _skip_lua_ws_and_comments(self.s, self.i)
            if self.i >= self.n:
                break
            c = self.s[self.i]
            if c in {"'", '"'}:
                self._parse_string()
                continue
            if c == "[" and (self.i + 1 < self.n and self.s[self.i + 1] in {"[", "="}):
                if self._parse_long_string() is not None:
                    continue
            if c.isalpha() or c == "_":
                k_start = self.i
                while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
                    self.i += 1
                word = self.s[k_start : self.i]
                if word in block_open:
                    depth += 1
                elif word == "end":
                    depth -= 1
            else:
                self.i += 1
        if depth != 0:
            raise ValueError("unterminated lua function")
        return self.s[start : self.i]

    def _parse_value(self) -> Any:
        self.i = _skip_lua_ws_and_comments(self.s, self.i)
        if self.i >= self.n:
            raise ValueError("unexpected eof")
        c = self.s[self.i]
        if c == "{":
            return self._parse_table()
        if c in {"'", '"'}:
            return self._parse_string()
        if c == "[":
            ls = self._parse_long_string()
            if ls is not None:
                return ls
            raise ValueError("unexpected [")
        if self.s[self.i :].startswith("function") and (
            self.i + 8 >= self.n or not (self.s[self.i + 8].isalnum() or self.s[self.i + 8] == "_")
        ):
            fn_src = self._skip_function_literal()
            return ("__LUA_FN__", fn_src)
        if c.isdigit() or (c == "-" and self.i + 1 < self.n and self.s[self.i + 1].isdigit()):
            start = self.i
            if c == "-":
                self.i += 1
            while self.i < self.n and (self.s[self.i].isdigit() or self.s[self.i] in {".", "e", "E", "+", "-"}):
                if self.s[self.i] in {"+", "-"} and self.s[self.i - 1] not in {"e", "E"}:
                    break
                self.i += 1
            raw_num = self.s[start : self.i]
            return float(raw_num) if ("." in raw_num or "e" in raw_num or "E" in raw_num) else int(raw_num)
        if c.isalpha() or c == "_":
            start = self.i
            while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
                self.i += 1
            ident = self.s[start : self.i]
            if ident == "true":
                return True
            if ident == "false":
                return False
            if ident == "nil":
                return None
            # Unknown expression/call -> fall back to Lua5.4 sandbox
            raise ValueError(f"unsupported identifier {ident}")
        raise ValueError(f"unexpected char {c!r}")

    def _parse_table(self) -> dict[str, Any] | list[Any]:
        self.i += 1  # skip '{'
        arr: list[Any] = []
        obj: dict[str, Any] = {}
        while True:
            self.i = _skip_lua_ws_and_comments(self.s, self.i)
            if self.i >= self.n:
                raise ValueError("unterminated table")
            if self.s[self.i] == "}":
                self.i += 1
                break

            key: str | None = None
            save_i = self.i
            if self.s[self.i] == "[" and (self.i + 1 < self.n and self.s[self.i + 1] in {"'", '"'}):
                self.i += 1
                k = self._parse_string()
                self.i = _skip_lua_ws_and_comments(self.s, self.i)
                if self.i < self.n and self.s[self.i] == "]":
                    self.i = _skip_lua_ws_and_comments(self.s, self.i + 1)
                    if self.i < self.n and self.s[self.i] == "=":
                        self.i += 1
                        key = k
                    else:
                        self.i = save_i
                else:
                    self.i = save_i
            elif self.s[self.i].isalpha() or self.s[self.i] == "_":
                k_start = self.i
                while self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] == "_"):
                    self.i += 1
                ident = self.s[k_start : self.i]
                after = _skip_lua_ws_and_comments(self.s, self.i)
                if after < self.n and self.s[after] == "=" and self.s[after : after + 2] != "==":
                    self.i = after + 1
                    key = ident
                else:
                    self.i = save_i

            val = self._parse_value()
            if key is not None:
                if isinstance(val, tuple) and len(val) == 2 and val[0] == "__LUA_FN__":
                    if key == "transform_item":
                        obj["_has_transform_item"] = True
                    elif key == "fetch_feed":
                        obj["_has_fetch_feed"] = True
                    elif key == "fetch_article":
                        obj["_has_fetch_article"] = True
                else:
                    obj[key] = val
            else:
                arr.append(val)

            self.i = _skip_lua_ws_and_comments(self.s, self.i)
            if self.i < self.n and self.s[self.i] in {",", ";"}:
                self.i += 1

        if obj and not arr:
            return obj
        if arr and not obj:
            return arr
        merged = dict(obj)
        for idx, item in enumerate(arr, start=1):
            merged[str(idx)] = item
        return merged


def try_parse_declarative_lua(
    script: str,
    *,
    source_path: str = "",
    allow_loopback_http: bool = False,
    allowed_loopback_origins: Iterable[str] = (),
) -> RecipeSpec | None:
    """Fast pure-Python declarative Lua table parser for `< 1ms` non-blocking startup."""
    try:
        tbl = _PureLuaTableParser(script).parse()
    except Exception:
        return None
    if tbl is None:
        return None
    spec = parse_recipe_dict(
        tbl,
        source_path=source_path,
        raw_source=script,
        allow_loopback_http=allow_loopback_http,
        allowed_loopback_origins=allowed_loopback_origins,
    )
    spec.is_lua_script = True
    return spec


class LuaEngine:
    """Sandboxed Lua 5.4 recipe evaluator & hook runner."""

    @staticmethod
    def find_lua_binary() -> str | None:
        for cand in ("lua5.4", "lua"):
            found = shutil.which(cand)
            if found:
                return found
        return None

    @staticmethod
    async def _run_lua_subprocess(
        ctx: PluginContext | None,
        payload: dict[str, Any],
        *,
        timeout_sec: float = 5.0,
    ) -> dict[str, Any]:
        lua_bin = LuaEngine.find_lua_binary()
        if not lua_bin:
            raise FeedRecipeError("service_offline", "Lua 5.4 运行时不可用，无法执行脚本型 Lua 配方")
        stdin_str = json.dumps(payload, ensure_ascii=False)
        if ctx is not None:
            res = await ctx.exec_subprocess(
                [lua_bin, "-e", _LUA_SANDBOX_DRIVER],
                stdin_data=stdin_str,
                timeout_sec=timeout_sec,
            )
            rc, stdout, stderr, timed_out = res.returncode, res.stdout, res.stderr, res.timed_out
        else:
            proc = await asyncio.create_subprocess_exec(
                lua_bin,
                "-e",
                _LUA_SANDBOX_DRIVER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(stdin_str.encode("utf-8")),
                    timeout=timeout_sec,
                )
                rc = proc.returncode or 0
                stdout = out_b.decode("utf-8", errors="replace")
                stderr = err_b.decode("utf-8", errors="replace")
                timed_out = False
            except TimeoutError:
                proc.kill()
                await proc.wait()
                rc, stdout, stderr, timed_out = -1, "", "", True

        if timed_out:
            raise FeedRecipeError("timeout", "Lua 配方执行超时")
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            detail = (stderr or stdout or f"rc={rc}").strip()
            raise FeedRecipeError("validation_error", f"Lua 执行失败: {detail}") from None
        if not isinstance(parsed, dict):
            raise FeedRecipeError("validation_error", "Lua 沙箱返回了无效结果")
        return parsed

    @staticmethod
    async def _execute_with_replay(
        ctx: PluginContext | None,
        payload: dict[str, Any],
        *,
        spec: RecipeSpec | None = None,
        http: HttpTransport | None = None,
        cookies: CookieBridge | None = None,
        timeout_sec: float = 15.0,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> Any:
        cache: dict[str, list[Any]] = {}
        for _ in range(_MAX_RPC_ROUNDS):
            req_payload = {**payload, "cache": cache, "instruction_limit": DEFAULT_INSTRUCTION_LIMIT}
            outcome = await LuaEngine._run_lua_subprocess(ctx, req_payload, timeout_sec=max(5.0, float(timeout_sec)))
            if outcome.get("ok"):
                return outcome.get("result")
            rpc = outcome.get("need_rpc")
            if not isinstance(rpc, Mapping):
                err_msg = str(outcome.get("error") or "Lua execution failed")
                raise FeedRecipeError("validation_error", err_msg)

            key = str(rpc.get("key") or "")
            op = str(rpc.get("op") or "")
            args = list(rpc.get("args") or [])
            cache[key] = await LuaEngine._dispatch_host_op(
                op,
                args,
                spec=spec,
                http=http,
                cookies=cookies,
                timeout_sec=timeout_sec,
                allow_loopback_http=allow_loopback_http,
                allowed_loopback_origins=allowed_loopback_origins,
            )
        raise FeedRecipeError("validation_error", "Lua recipe exceeded host RPC call limit")

    @staticmethod
    async def _dispatch_host_op(
        op: str,
        args: list[Any],
        *,
        spec: RecipeSpec | None,
        http: HttpTransport | None,
        cookies: CookieBridge | None,
        timeout_sec: float,
        allow_loopback_http: bool,
        allowed_loopback_origins: Iterable[str],
    ) -> list[Any]:
        if op in {"http.get", "http.post"}:
            if http is None:
                return [None, "HTTP service unavailable in Lua context"]
            url = str(args[0] or "") if args else ""
            body = str(args[1] or "") if (op == "http.post" and len(args) > 1) else ""
            opts = args[2] if (op == "http.post" and len(args) > 2) else (args[1] if len(args) > 1 else {})
            opts_map = opts if isinstance(opts, Mapping) else {}
            use_cookies = bool(opts_map["use_cookies"]) if isinstance(opts_map.get("use_cookies"), bool) else (
                spec.use_chrome_cookies if spec else False
            )
            headers = {
                str(k): str(v)
                for k, v in (opts_map.get("headers") or {}).items()
                if isinstance(opts_map.get("headers"), Mapping)
            }
            cookie_hdr = ""
            secrets: list[str] = []
            if use_cookies and cookies is not None:
                cres = await cookies.resolve_for_url(
                    url, spec.id if spec else "", True, spec.cookie_domain if spec else ""
                )
                cookie_hdr = cres.header
                if cookie_hdr:
                    secrets.append(cookie_hdr)
            res = await http.request(
                HttpRequest(
                    url=url,
                    method="POST" if op == "http.post" else "GET",
                    headers=headers,
                    cookie_header=cookie_hdr,
                    body=body,
                    timeout_sec=timeout_sec,
                ),
                extra_secrets=secrets,
                allow_loopback_http=allow_loopback_http,
                allowed_loopback_origins=allowed_loopback_origins,
            )
            return [
                {
                    "ok": res.ok,
                    "status": res.status_code,
                    "body": res.body,
                    "effective_url": res.effective_url,
                    "error": res.error,
                }
            ]

        if op == "cookies.status":
            url = str(args[0] or "") if args else ""
            if cookies is None:
                return [{"available": False, "source": "none"}]
            cres = await cookies.resolve_for_url(
                url, spec.id if spec else "", True, spec.cookie_domain if spec else ""
            )
            return [{"available": bool(cres.header), "source": cres.source_used}]

        if op == "html.select":
            doc = HtmlDocument(str(args[0] or ""))
            nodes = select_nodes(doc.root, str(args[1] or ""))
            return [
                [
                    {
                        "text": node_inner_text(n),
                        "html": node_inner_html(n),
                        "href": node_attribute(n, "href"),
                        "src": node_attribute(n, "src"),
                    }
                    for n in nodes
                ]
            ]
        if op == "html.extract":
            doc = HtmlDocument(str(args[0] or ""))
            return [extract_value(doc.root, str(args[1] or ""))]
        if op == "html.extract_all":
            doc = HtmlDocument(str(args[0] or ""))
            return [extract_all_values(doc.root, str(args[1] or ""))]
        if op == "html.readability":
            return [extract_readability_html(str(args[0] or ""), str(args[1] or "") if len(args) > 1 else "")]
        if op == "html.paragraphs":
            paras = html_to_paragraphs(str(args[0] or ""), str(args[1] or "") if len(args) > 1 else "")
            return [[{"id": p.id, "kind": p.kind, "original": p.original} for p in paras]]
        if op == "html.strip":
            return [strip_html_tags_to_text(str(args[0] or ""))]
        if op == "xml.parse_feed":
            max_items = int(args[1]) if len(args) > 1 and isinstance(args[1], (int, float)) else 30
            entries = parse_xml_feed(str(args[0] or ""), max_items=max_items)
            return [
                [
                    {
                        "id": e.id,
                        "title": e.title,
                        "url": e.url,
                        "author": e.author,
                        "published_at": e.published_at,
                        "excerpt": e.summary,
                        "content_html": e.content_html,
                        "tags": e.categories,
                    }
                    for e in entries
                ]
            ]
        if op == "xml.xpath":
            return [query_xml_xpath(str(args[0] or ""), str(args[1] or ""))]
        if op == "text.decode_entities":
            return [decode_html_entities(str(args[0] or ""))]
        if op == "text.resolve_url":
            base = str(args[0] or "") if len(args) > 0 else ""
            rel = str(args[1] or "") if len(args) > 1 else ""
            return [resolve_url(base, rel)]
        return [None]

    @staticmethod
    async def load_recipe_from_script(
        script: str,
        *,
        ctx: PluginContext | None = None,
        source_path: str = "",
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> RecipeSpec:
        """Load and validate a Lua recipe via pure-Python table fast path or sandboxed Lua 5.4."""
        fast = try_parse_declarative_lua(
            script,
            source_path=source_path,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        if fast is not None:
            return fast

        raw_tbl = await LuaEngine._execute_with_replay(
            ctx,
            {"mode": "load", "script": script, "source_name": source_path or "recipe.lua"},
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        spec = parse_recipe_dict(
            raw_tbl,
            source_path=source_path,
            raw_source=script,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        spec.is_lua_script = True
        return spec

    @staticmethod
    async def transform_feed_item(
        spec: RecipeSpec,
        item_dict: dict[str, Any],
        *,
        ctx: PluginContext | None = None,
    ) -> dict[str, Any]:
        """Run optional Lua `transform_item(item)` hook."""
        if not spec.is_lua_script or not spec.has_lua_transform_item or not spec.raw_source:
            return item_dict
        if LuaEngine.find_lua_binary() is None:
            # Safe graceful fallback for simple title trimming when lua5.4 binary is absent
            out = dict(item_dict)
            if isinstance(out.get("title"), str):
                out["title"] = out["title"].strip()
            return out
        res = await LuaEngine._execute_with_replay(
            ctx,
            {
                "mode": "transform_item",
                "script": spec.raw_source,
                "source_name": spec.source_path or "recipe.lua",
                "item": item_dict,
            },
            spec=spec,
        )
        return dict(res) if isinstance(res, Mapping) else item_dict

    @staticmethod
    async def execute_fetch_feed(
        spec: RecipeSpec,
        query: str,
        *,
        ctx: PluginContext | None,
        http: HttpTransport,
        cookies: CookieBridge,
        timeout_sec: float = 15.0,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Execute Lua `fetch_feed(spec, query)` hook inside sandboxed Lua 5.4."""
        if not spec.is_lua_script or not spec.has_lua_fetch_feed or not spec.raw_source:
            raise FeedRecipeError("validation_error", "Recipe does not define Lua fetch_feed hook")
        res = await LuaEngine._execute_with_replay(
            ctx,
            {
                "mode": "fetch_feed",
                "script": spec.raw_source,
                "source_name": spec.source_path or "recipe.lua",
                "spec": recipe_to_dict(spec),
                "query": query,
            },
            spec=spec,
            http=http,
            cookies=cookies,
            timeout_sec=timeout_sec,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        if not isinstance(res, list):
            raise FeedRecipeError("validation_error", "fetch_feed must return a table array")
        return [dict(item) for item in res if isinstance(item, Mapping)]

    @staticmethod
    async def execute_fetch_article(
        spec: RecipeSpec,
        item_ctx: Mapping[str, Any],
        *,
        ctx: PluginContext | None,
        http: HttpTransport,
        cookies: CookieBridge,
        timeout_sec: float = 15.0,
        allow_loopback_http: bool = False,
        allowed_loopback_origins: Iterable[str] = (),
    ) -> TextArticle:
        """Execute Lua `fetch_article(spec, item)` hook inside sandboxed Lua 5.4."""
        if not spec.is_lua_script or not spec.has_lua_fetch_article or not spec.raw_source:
            raise FeedRecipeError("validation_error", "Recipe does not define Lua fetch_article hook")
        res = await LuaEngine._execute_with_replay(
            ctx,
            {
                "mode": "fetch_article",
                "script": spec.raw_source,
                "source_name": spec.source_path or "recipe.lua",
                "spec": recipe_to_dict(spec),
                "item": dict(item_ctx),
            },
            spec=spec,
            http=http,
            cookies=cookies,
            timeout_sec=timeout_sec,
            allow_loopback_http=allow_loopback_http,
            allowed_loopback_origins=allowed_loopback_origins,
        )
        if not isinstance(res, Mapping):
            raise FeedRecipeError("validation_error", "fetch_article must return a table")

        title = str(res.get("title") or item_ctx.get("title") or "").strip() or spec.name
        authors_parts: list[str] = []
        if isinstance(res.get("author"), str) and res["author"].strip():
            authors_parts.append(res["author"].strip())
        if isinstance(res.get("authors"), list):
            for a in res["authors"]:
                if isinstance(a, str) and a.strip():
                    authors_parts.append(a.strip())
        authors = ", ".join(authors_parts)

        paragraphs: list[TextParagraph] = []
        raw_paras = res.get("paragraphs")
        if isinstance(raw_paras, list):
            for idx, entry in enumerate(raw_paras, start=1):
                if isinstance(entry, Mapping):
                    orig = str(entry.get("original") or "")
                    if orig or entry.get("image_url") or entry.get("imageUrl"):
                        kind = str(entry.get("kind") or "paragraph")
                        if kind not in {"heading", "paragraph", "quote", "code", "formula", "image"}:
                            kind = "paragraph"
                        paragraphs.append(
                            TextParagraph(
                                id=str(entry.get("id") or f"p_{idx}"),
                                kind=kind,  # type: ignore[arg-type]
                                original=orig,
                                image_url=str(entry.get("image_url") or entry.get("imageUrl") or ""),
                                alt=str(entry.get("alt") or ""),
                                caption=str(entry.get("caption") or ""),
                                translated=str(entry.get("translated") or ""),
                            )
                        )
                elif isinstance(entry, str) and entry:
                    paragraphs.append(TextParagraph(id=f"p_{idx}", kind="paragraph", original=entry))
        if not paragraphs and isinstance(res.get("body_html"), str) and res["body_html"].strip():
            paragraphs = html_to_paragraphs(res["body_html"], str(res.get("url") or item_ctx.get("url") or ""))

        def _map_comment(raw_c: Mapping[str, Any], depth: int = 0) -> CommentNode:
            replies: list[CommentNode] = []
            if depth < 16 and isinstance(raw_c.get("replies"), list):
                replies = [_map_comment(r, depth + 1) for r in raw_c["replies"] if isinstance(r, Mapping)]
            return CommentNode(
                id=str(raw_c.get("id") or ""),
                author=str(raw_c.get("author") or "anonymous") or "anonymous",
                role_badge=str(raw_c.get("role_badge") or ""),
                body=str(raw_c.get("body") or ""),
                translated_body=str(raw_c.get("translated_body") or ""),
                created_at=str(raw_c.get("created_at") or ""),
                score=int(raw_c["score"]) if isinstance(raw_c.get("score"), (int, float)) else 0,
                replies=replies,
            )

        comments = [
            _map_comment(c)
            for c in (res.get("comments") if isinstance(res.get("comments"), list) else [])
            if isinstance(c, Mapping)
        ]

        return TextArticle(
            id=str(item_ctx.get("id") or "lua"),
            source_id=spec.id,
            source_name=spec.name,
            category=spec.category,
            channel_id=spec.id,
            title=title,
            subtitle=str(res.get("subtitle") or ""),
            authors=authors,
            author=authors,
            published_at=str(res.get("published_at") or item_ctx.get("published_at") or ""),
            url=str(res.get("url") or item_ctx.get("url") or ""),
            paragraphs=paragraphs,
            comments=comments,
        )


__all__ = ["DEFAULT_INSTRUCTION_LIMIT", "LuaEngine", "try_parse_declarative_lua"]
